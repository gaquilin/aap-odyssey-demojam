"""Start read-only scans (AI Pipelines runs) from AAP Odyssey.

AAP Odyssey can start three pipelines, and only these, with fixed safe parameters:

  readiness  aap-migration-during with mode=plan: export from AAP 2.4, pre-migration
             report, Dependency Analyzer and migration plan. Never writes to the target AAP.
  train      aap-migration-train: retrain the risk model on the datasets of all migrations.
  predict    aap-migration-predict: AI readiness report from the trained risk model.

Migrations (mode=execute) are deliberately NOT available here: they are started
from the OpenShift AI dashboard.

The calls go to the REST API (KFP v2beta1) of the project's pipeline server, through
its kube-rbac-proxy on port 8443, authenticated with AAP Odyssey's ServiceAccount
token. The proxy authorizes the request on the subresource
``datasciencepipelinesapplications/api`` of the DSPA (GET -> get, POST -> create):
see cluster/10-copilot/04-rbac-pipelines.yaml.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
RUN_PREFIX = "copilot"
TERMINAL_STATES = {"SUCCEEDED", "FAILED", "SKIPPED", "CANCELED"}
SCAN_KINDS = ("readiness", "train", "predict")


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _default_url() -> str:
    ns_file = SA_DIR / "namespace"
    ns = ns_file.read_text().strip() if ns_file.is_file() else "aap-migration-ai"
    dspa = _env("PIPELINES_DSPA_NAME", "dspa")
    return f"https://ds-pipeline-{dspa}.{ns}.svc.cluster.local:8443"


@dataclass
class ScanSettings:
    api_url: str = field(default_factory=lambda: _env("PIPELINES_API_URL") or _default_url())
    token_file: str = field(default_factory=lambda: _env("PIPELINES_API_TOKEN_FILE", str(SA_DIR / "token")))
    ca_file: str = field(default_factory=lambda: _env("PIPELINES_API_CA_FILE", str(SA_DIR / "service-ca.crt")))
    readiness_pipeline: str = field(default_factory=lambda: _env("PIPELINE_READINESS_NAME", "AAP migration - during migration (plan, run, collect)"))
    predict_pipeline: str = field(default_factory=lambda: _env("PIPELINE_PREDICT_NAME", "AAP migration - predict next migration"))
    train_pipeline: str = field(default_factory=lambda: _env("PIPELINE_TRAIN_NAME", "AAP migration - train risk model"))
    enabled: bool = field(default_factory=lambda: _env("COPILOT_SCANS_ENABLED", "true").lower() in ("1", "true", "yes"))


def _norm(name: str) -> str:
    """'AAP migration — during migration (plan, run, collect)' and 'aap-migration-during-migration-plan-run-collect'
    compare equal: case, spaces and punctuation are ignored."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


class PipelinesAPI:
    def __init__(self, settings: ScanSettings | None = None):
        self.s = settings or ScanSettings()

    def _client(self):
        import httpx

        token = Path(self.s.token_file).read_text().strip() if Path(self.s.token_file).is_file() else ""
        verify: bool | str = self.s.ca_file if Path(self.s.ca_file).is_file() else True
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return httpx.Client(base_url=self.s.api_url.rstrip("/"), headers=headers, verify=verify, timeout=30)

    def _call(self, method: str, path: str, **kw) -> dict:
        with self._client() as c:
            r = c.request(method, path, **kw)
        if r.status_code in (401, 403):
            raise PermissionError(f"pipeline server refused AAP Odyssey ({r.status_code}): apply "
                                  "cluster/10-copilot/04-rbac-pipelines.yaml")
        r.raise_for_status()
        return r.json() if r.content else {}

    def find_pipeline(self, name: str) -> dict:
        want, token, seen = _norm(name), "", []
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if token:
                params["page_token"] = token
            data = self._call("GET", "/apis/v2beta1/pipelines", params=params)
            for p in data.get("pipelines") or []:
                names = (p.get("display_name", ""), p.get("name", ""))
                seen.append(names[0] or names[1])
                if want in (_norm(n) for n in names if n):
                    return p
            token = data.get("next_page_token") or ""
            if not token:
                break
        raise LookupError(f"pipeline '{name}' is not imported in the pipeline server (found: {seen})")

    def latest_version(self, pipeline_id: str) -> str:
        data = self._call("GET", f"/apis/v2beta1/pipelines/{pipeline_id}/versions",
                          params={"page_size": 1, "sort_by": "created_at desc"})
        versions = data.get("pipeline_versions") or []
        if not versions:
            raise LookupError(f"pipeline {pipeline_id} has no versions")
        return versions[0]["pipeline_version_id"]

    def create_run(self, pipeline_name: str, display_name: str, description: str, params: dict) -> dict:
        p = self.find_pipeline(pipeline_name)
        body = {
            "display_name": display_name,
            "description": description,
            "pipeline_version_reference": {"pipeline_id": p["pipeline_id"],
                                           "pipeline_version_id": self.latest_version(p["pipeline_id"])},
            "runtime_config": {"parameters": params},
        }
        return self._call("POST", "/apis/v2beta1/runs", json=body)

    def get_run(self, run_id: str) -> dict:
        return self._call("GET", f"/apis/v2beta1/runs/{run_id}")

    def recent_runs(self, contains: str = "", limit: int = 50) -> list[dict]:
        data = self._call("GET", "/apis/v2beta1/runs", params={"page_size": limit, "sort_by": "created_at desc"})
        runs = [r for r in data.get("runs") or [] if (r.get("display_name") or "").startswith(RUN_PREFIX + "-")]
        return [r for r in runs if not contains or contains in (r.get("display_name") or "")]

    def pipeline_for(self, kind: str) -> str:
        return {"readiness": self.s.readiness_pipeline, "train": self.s.train_pipeline,
                "predict": self.s.predict_pipeline}[kind]

    def runs_for_migration(self, migration_id: str, limit: int = 100) -> list[dict]:
        """Every run (dashboard, schedule or Copilot) whose migration_id parameter is this migration."""
        data = self._call("GET", "/apis/v2beta1/runs", params={"page_size": limit, "sort_by": "created_at desc"})
        return [r for r in data.get("runs") or []
                if ((r.get("runtime_config") or {}).get("parameters") or {}).get("migration_id") == migration_id]


def task_states(run: dict) -> list[dict]:
    """Pipeline steps of a run with their state (from run_details.task_details)."""
    out = []
    for t in (run.get("run_details") or {}).get("task_details") or []:
        name = t.get("display_name") or ""
        if not re.match(r"^\d\. ", name):
            continue
        out.append({"step": name, "state": t.get("state"), "start": t.get("start_time"), "end": t.get("end_time")})
    seen, uniq = set(), []
    for t in sorted(out, key=lambda x: x["step"]):
        if t["step"] not in seen:
            seen.add(t["step"])
            uniq.append(t)
    return uniq


def scan_parameters(kind: str, migration_id: str, refresh_export: bool, use_llm: bool) -> dict:
    """Fixed, read-only parameters. mode is always 'plan' for readiness (plan never runs import steps)."""
    if kind == "readiness":
        return {"migration_id": migration_id, "mode": "plan", "refresh_export": refresh_export,
                "with_dependencies": True, "dry_run": False, "include_credentials": False, "include_iam": False,
                "use_llm": use_llm, "abort_failure_rate": 0.5, "train_after": False, "casc_review": False}
    if kind == "predict":
        return {"migration_id": migration_id, "refresh_export": refresh_export}
    if kind == "train":
        return {"migrations": "all", "threshold": 0.5}
    raise ValueError(f"unknown scan kind '{kind}' (allowed: {', '.join(SCAN_KINDS)})")


def run_summary(run: dict) -> dict:
    return {"run_id": run.get("run_id"), "name": run.get("display_name"), "state": run.get("state"),
            "created_at": run.get("created_at"), "finished_at": run.get("finished_at"),
            "error": (run.get("error") or {}).get("message") if isinstance(run.get("error"), dict) else run.get("error")}


def run_display_name(kind: str, migration_id: str) -> str:
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{RUN_PREFIX}-{kind}-{migration_id}-{stamp}"


_ID_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def valid_migration_id(mid: str) -> bool:
    return bool(_ID_RE.match(mid or ""))
