"""AAP Odyssey: ask questions about a migration and generate CasC.

The LLM served on OpenShift AI answers through tools that read the real data
of a migration (state database, reports, export, risk model) and generate or
validate Configuration as Code. Without an LLM a keyword router still answers
the most common requests.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from migration_ai import casc as casc_mod
from migration_ai import toolkit
from migration_ai.config import Settings
from migration_ai.llm import LLM, Tool

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are AAP Odyssey, the migration assistant for Red Hat Ansible Automation Platform (AAP) 2.4 to a newer AAP (2.5, 2.6, 2.7).
You answer questions about ONE migration using tools that read its real data: the toolkit state
database (import results), the pre-migration and AI readiness reports, the RAW AAP 2.4 export and
the risk model. You can also generate Configuration as Code (CasC) for the target AAP in the
infra.aap_configuration format.

Rules:
- Use the tools; never invent objects, numbers or error messages.
- For CasC of EXISTING objects call generate_casc (deterministic, correct field names) and show
  the relevant YAML. For NEW objects requested by the user, write YAML using only the fields
  returned by casc_schema, then ALWAYS call validate_casc and fix every error before answering.
- Secrets are never shown: CasC uses {{ vaulted_* }} placeholders.
- Scans: start_scan starts a READ-ONLY pipeline run: kind "readiness" (export from AAP 2.4,
  pre-migration report, dependency analysis, migration plan) or kind "predict" (AI readiness report
  from the risk model). Use scan_status to follow it; when it has SUCCEEDED, call overview or predict
  to report the results. For "what is the status of the migration" call migration_status.
  For "what's next" or "where are we" call journey_status and say the next step and whether a human
  must act. You cannot start or run a migration: if asked, explain that migrations
  are started from the OpenShift AI dashboard (pipeline "AAP migration - during migration (plan, run, collect)", mode plan/execute).
- Answer in {language}, concise, with Markdown. Put YAML in ```yaml blocks.
"""


class Copilot:
    def __init__(self, settings: Settings, work_dir: Path, migration_id: str = "", model_path: Path | None = None,
                 use_llm: bool = True, language: str = "English", scans=None,
                 refresh: Callable[[str], Path] | None = None, user: str = "",
                 progress_fn: Callable[[str, Path], dict] | None = None,
                 reports_fn: Callable[[str], dict] | None = None,
                 migrations_fn: Callable[[], list[dict]] | None = None,
                 publish_fn: Callable[[str, Path, str], None] | None = None,
                 model_info_fn: Callable[[], dict] | None = None,
                 data_dir: Path | None = None):
        self.settings = settings
        self.work = work_dir
        self.migration_id = migration_id
        self.model_path = model_path
        self.language = language
        self.db = toolkit.StateDB(work_dir / toolkit.DB_RELATIVE)
        self.llm = LLM(settings.llm) if use_llm else None
        self._bundle = None
        self._export_index = None
        self.files: list[str] = []
        self.scans = scans
        self.refresh = refresh
        self.user = user
        self.progress_fn = progress_fn
        self.reports_fn = reports_fn
        self.migrations_fn = migrations_fn
        self.publish_fn = publish_fn
        self.model_info_fn = model_info_fn
        self.data_dir = data_dir or work_dir.parent
        self.view: dict | None = None

    def _json(self, rel: str) -> dict | None:
        p = self.work / rel
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None

    def bundle(self):
        if self._bundle is None and self.model_path and self.model_path.is_file():
            from migration_ai.predict import load_bundle

            self._bundle = load_bundle(self.model_path)
        return self._bundle

    def export_index(self) -> dict[tuple[str, int], dict]:
        if self._export_index is None:
            from aap_migration.analysis.premigration import load_inventory_from_export

            self._export_index = {}
            if (self.work / "exports").is_dir():
                for t, items in load_inventory_from_export(self.work / "exports").items():
                    for o in items:
                        rid = o.get("_source_id", o.get("id"))
                        if isinstance(rid, int):
                            self._export_index[(t, rid)] = o
        return self._export_index

    def overview(self) -> dict:
        agent = self._json("reports/agent/agent-summary.json") or {}
        pre = (self._json("reports/premigration-report.json") or {}).get("summary", {})
        ai = (self._json("ai/ai-readiness-report.json") or {}).get("ai", {})
        return {
            "migration_id": self.migration_id,
            "agent": {k: agent.get(k) for k in ("mode", "status", "stop_reason", "finished", "model")},
            "import_results": self.db.totals(),
            "top_errors": self.db.top_errors(10),
            "premigration": {k: pre.get(k) for k in ("predicted_success_pct", "outcomes", "resources_by_rc")},
            "ai_model": ai or ({"version": self.bundle()["version"]} if self.bundle() else None),
        }

    def search(self, text: str, status: str = "", resource_type: str = "", limit: int = 20) -> list[dict]:
        rows = self.db.find(text, limit=200)
        rows = [r for r in rows if r.get("phase") == "import"
                and (not status or r["status"] == status) and (not resource_type or r["resource_type"] == resource_type)]
        return rows[:limit]

    def explain(self, resource_type: str, name_or_id: str) -> dict:
        from aap_migration.cli.commands.migration_report_v2 import (
            _HUMAN_READABLE_ERRORS,
            extract_export_metadata,
            normalize_error_key,
        )

        rtypes = {resource_type, resource_type + "s", resource_type.rstrip("s"),
                  resource_type.replace(" ", "_"), resource_type.replace(" ", "_") + "s"}
        matches = [r for r in self.db.find(name_or_id, 50) if r["resource_type"] in rtypes
                   and r.get("phase") == "import"]
        if not matches:
            return self._explain_not_imported(rtypes, name_or_id)
        r = matches[0]
        obj = self.export_index().get((resource_type, int(r["source_id"])), {})
        meta = extract_export_metadata(resource_type, obj) if obj else {}
        key = normalize_error_key(r.get("error_message") or "")
        explanation = _HUMAN_READABLE_ERRORS.get(key) or next(
            (v for k, v in _HUMAN_READABLE_ERRORS.items() if key and key.startswith(k)), "")
        pre = self._json("reports/premigration-report.json") or {}
        findings = [f for f in pre.get("findings", []) if f["key"] == f"{resource_type}:{r['source_id']}"]
        ai = (self._json("ai/ai-readiness-report.json") or {}).get("resources", {}).get(
            f"{resource_type}:{r['source_id']}", {})
        log_lines = self._grep_logs(str(r["source_name"]), 8)
        return {
            "resource": {k: r[k] for k in ("resource_type", "source_id", "source_name", "target_id", "status")},
            "error_message": (r.get("error_message") or "")[:800],
            "error_class": key, "explanation": _fill(explanation, {"source_name": r["source_name"], **meta}),
            "aap24_metadata": {k: v for k, v in meta.items() if k in (
                "organization_name", "project_name", "inventory_name", "playbook", "modified", "modified_by",
                "last_job_run", "last_job_failed", "sync_status", "credential_names")},
            "premigration_findings": [{k: f[k] for k in ("rc", "severity", "title", "detail", "remediation")}
                                      for f in findings],
            "ai_prediction": {k: ai.get(k) for k in ("p_fail", "predicted_error")} if ai else None,
            "log_lines": log_lines,
        }

    def _explain_not_imported(self, rtypes: set[str], name_or_id: str) -> dict:
        """An object with no import result (skipped by the toolkit, blocked or stale): what AAP 2.4 and the
        pre-migration analysis say about it."""
        from aap_migration.cli.commands.migration_report_v2 import extract_export_metadata

        pre = self._json("reports/premigration-report.json") or {}
        findings = [f for f in pre.get("findings", []) if str(f.get("resource_name")) == name_or_id
                    and f.get("resource_type") in rtypes] or \
            [f for f in pre.get("findings", []) if str(f.get("resource_name")) == name_or_id]
        hits = [(k, o) for k, o in self.export_index().items()
                if k[0] in rtypes and (str(o.get("name")) == name_or_id or str(k[1]) == name_or_id)]
        if not findings and not hits:
            return {"error": f"'{name_or_id}' is neither in the import results nor in the AAP 2.4 export: "
                             "check the name (exact, case sensitive) and the object type"}
        rtype, rid = (hits[0][0] if hits else (findings[0]["resource_type"], findings[0].get("resource_id")))
        meta = extract_export_metadata(rtype, hits[0][1]) if hits else {}
        key = f"{rtype}:{rid}"
        ai = (self._json("ai/ai-readiness-report.json") or {}).get("resources", {}).get(key, {})
        other = [r for r in self.db.find(name_or_id, 20) if r["resource_type"] == rtype]
        return {
            "resource": {"resource_type": rtype, "source_id": rid, "source_name": name_or_id,
                         "status": (other[0].get("status") if other else None) or "not imported"},
            "explanation": "The object exists on AAP 2.4 but has no import result: the toolkit did not import it "
                           "(skipped, or blocked by the pre-migration findings below). Fix it on AAP 2.4, or "
                           "decide to leave it behind if it is stale.",
            "premigration_outcome": (pre.get("resources") or {}).get(key),
            "premigration_findings": [{k: f.get(k) for k in ("rc", "severity", "title", "detail", "remediation")}
                                      for f in findings[:5]],
            "aap24_metadata": {k: v for k, v in meta.items() if k in (
                "organization_name", "project_name", "inventory_name", "playbook", "modified", "modified_by",
                "last_job_run", "last_job_failed", "credential_names")},
            "ai_prediction": {k: ai.get(k) for k in ("p_fail", "predicted_error")} if ai else None,
            "log_lines": self._grep_logs(name_or_id, 8),
        }

    def _grep_logs(self, needle: str, limit: int) -> list[str]:
        out: list[str] = []
        logs = self.work / "logs"
        if not logs.is_dir() or not needle:
            return out
        for f in sorted(logs.rglob("*.log")):
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if needle in line and ("error" in line.lower() or "fail" in line.lower()):
                            out.append(f"{f.name}: {line.strip()[:300]}")
                            if len(out) >= limit:
                                return out
            except OSError:
                continue
        return out

    def predict(self, organization: str = "", resource_type: str = "", top: int = 15) -> dict:
        rep = self._json("ai/ai-readiness-report.json")
        if not rep:
            if not self.bundle():
                return {"error": "no AI readiness report and no trained model for this migration"}
            from migration_ai.predict import build_ai_report

            rep = build_ai_report(self.work, self.bundle())
        res = [r for r in rep["resources"].values() if r.get("p_fail") is not None
               and (not organization or r["organization"].lower() == organization.lower())
               and (not resource_type or r["resource_type"] == resource_type)]
        res.sort(key=lambda r: -r["p_fail"])
        return {"model": rep.get("ai", {}).get("model_version"), "summary": rep.get("ai"),
                "highest_risk": [{k: r.get(k) for k in ("resource_type", "name", "organization", "p_fail",
                                                        "predicted_error", "rcs")} for r in res[:top]]}

    def generate_casc(self, organization: str = "", resource_types: list[str] | None = None,
                      source: str = "migration", include_hosts: bool = False) -> dict:
        types = resource_types or casc_mod.CASC_TYPES
        bad = [t for t in types if t not in casc_mod.CASC_TYPES]
        if bad:
            return {"error": f"unknown resource types {bad}", "allowed": casc_mod.CASC_TYPES}
        if source == "live":
            env = toolkit.load_env_file(self.settings.toolkit_env_file)
            objs = casc_mod.objects_from_live(env.get("TARGET__URL", ""), env.get("TARGET__TOKEN", ""),
                                              verify=env.get("TARGET__VERIFY_SSL", "true").lower() != "false",
                                              include_hosts=include_hosts, organization=organization or None)
        else:
            objs = casc_mod.objects_from_migration(self.work)
        scopes, secrets, notes = casc_mod.convert(objs, organization or None, types, include_hosts)
        if not scopes:
            return {"error": f"no objects found for organization '{organization}'"}
        out = self.work / "casc" / time.strftime("%Y%m%d-%H%M%S")
        summary = casc_mod.write_casc(scopes, secrets, notes, out, source)
        archive = shutil.make_archive(str(out), "zip", out)
        self.files.append(archive)
        if self.publish_fn:
            self.publish_fn(self.migration_id, out.parent, "casc")
        preview = {}
        ordered = sorted(summary["files"], key=lambda f: (0 if "job_templates" in f else 1 if "projects" in f else 2, f))
        for f in ordered[:4]:
            text = (out / f).read_text(encoding="utf-8")
            preview[f] = text[:2500] + ("\n# ... truncated" if len(text) > 2500 else "")
        return {"output_dir": str(out), "zip": archive, "scopes": summary["scopes"], "notes": summary["notes"][:15],
                "validation": summary["validation"], "secrets_placeholders": summary["secrets"], "preview": preview}

    def validate_casc(self, yaml_text: str) -> dict:
        return casc_mod.validate_text(yaml_text)

    def start_scan(self, kind: str = "readiness", migration_id: str = "", refresh_export: bool = True) -> dict:
        from migration_ai import scans

        if not self.scans:
            return {"error": "scans are disabled in AAP Odyssey (COPILOT_SCANS_ENABLED=false)"}
        mid = (migration_id or self.migration_id).strip()
        if not scans.valid_migration_id(mid):
            return {"error": f"invalid migration id '{mid}': use lowercase letters, digits and '-'"}
        if kind not in scans.SCAN_KINDS:
            return {"error": f"unknown scan kind '{kind}'", "allowed": list(scans.SCAN_KINDS)}
        active = [r for r in self.scans.recent_runs(f"-{kind}-{mid}-", 20)
                  if r.get("state") not in scans.TERMINAL_STATES]
        if active:
            return {"status": "already_running", "run": scans.run_summary(active[0]),
                    "message": "a scan of the same kind is already running for this migration"}
        pipeline = self.scans.pipeline_for(kind)
        params = scans.scan_parameters(kind, mid, bool(refresh_export), use_llm=bool(self.llm and self.llm.configured))
        run = self.scans.create_run(pipeline, scans.run_display_name(kind, mid),
                                    f"Started from AAP Odyssey by {self.user or 'unknown user'}", params)
        log.info("scan started by %s: %s %s run=%s", self.user or "?", kind, mid, run.get("run_id"))
        return {"status": "started", "kind": kind, "migration_id": mid, "pipeline": pipeline,
                "parameters": params, "run": scans.run_summary(run),
                "next": "ask for the scan status in a few minutes; the run is also visible in the dashboard"}

    def journey_status(self) -> dict:
        from migration_ai import progress

        if self.progress_fn:
            return self.progress_fn(self.migration_id, self.work)
        return progress.compute(self.work, self.migration_id)

    def migration_status(self, migration_id: str = "") -> dict:
        """Current state of a migration: latest pipeline runs (any origin) + last saved agent result."""
        from migration_ai import scans

        mid = migration_id or self.migration_id
        out: dict[str, Any] = {"migration_id": mid}
        if self.scans:
            try:
                runs = self.scans.runs_for_migration(mid)
                out["runs"] = []
                for r in runs[:5]:
                    p = (r.get("runtime_config") or {}).get("parameters") or {}
                    item = scans.run_summary(r)
                    item.update({"mode": p.get("mode") or "-", "dry_run": p.get("dry_run")})
                    out["runs"].append(item)
                active = next((r for r in runs if r.get("state") not in scans.TERMINAL_STATES), None)
                if active:
                    detail = self.scans.get_run(active["run_id"])
                    out["active_run"] = scans.run_summary(detail)
                    out["active_steps"] = scans.task_states(detail)
            except Exception as exc:
                out["runs_error"] = str(exc)
        agent = self._json("reports/agent/agent-summary.json") or {}
        out["last_agent_result"] = {k: agent.get(k) for k in ("mode", "dry_run", "status", "stop_reason", "finished")}
        out["last_agent_steps"] = [{"step": s.get("title"), "status": s.get("status")} for s in agent.get("steps", [])]
        out["import_results"] = self.db.totals()
        return out

    def scan_status(self, run_id: str = "", migration_id: str = "") -> dict:
        from migration_ai import scans

        if not self.scans:
            return {"error": "scans are disabled in AAP Odyssey"}
        if run_id:
            run = self.scans.get_run(run_id)
        else:
            target = migration_id or self.migration_id
            runs = self.scans.recent_runs(f"-{target}-", 20)
            if not runs:
                return {"error": f"no scan started from AAP Odyssey for migration '{target}'"}
            run = runs[0]
        out = {"run": scans.run_summary(run)}
        if run.get("state") == "SUCCEEDED":
            mid = self.migration_id
            m = re.match(rf"{scans.RUN_PREFIX}-(readiness|train|predict)-(.+)-\d{{8}}-\d{{6}}$", run.get("display_name") or "")
            if m:
                out["kind"], mid = m.group(1), m.group(2)
            if self.refresh and mid:
                self.work = self.refresh(mid)
                self.migration_id = mid
                self.db = toolkit.StateDB(self.work / toolkit.DB_RELATIVE)
                self._export_index = None
            pre = (self._json("reports/premigration-report.json") or {}).get("summary", {})
            ai = (self._json("ai/ai-readiness-report.json") or {}).get("ai", {})
            out["results"] = {"migration_id": mid,
                              "premigration": {k: pre.get(k) for k in ("predicted_success_pct", "outcomes",
                                                                        "resources_by_rc")} if pre else None,
                              "ai_readiness": ai or None,
                              "plan_available": (self.work / "reports/agent/migration-plan.md").is_file()}
        return out

    def start_readiness_scan(self, migration_id: str = "", refresh_export: bool = True) -> dict:
        return self.start_scan("readiness", migration_id, refresh_export)

    def start_prediction(self, migration_id: str = "", refresh_export: bool = True) -> dict:
        res = self.start_scan("predict", migration_id, refresh_export)
        if res.get("status") in ("started", "already_running") and self.migration_id and migration_id \
                and migration_id != self.migration_id:
            from migration_ai import progress

            progress.confirm(self.work, "predict", True, self.user,
                             note=f"prediction of the next migration `{migration_id}` started")
            if self.publish_fn:
                self.publish_fn(self.migration_id, self.work / "journey", "journey")
        return res

    def start_training(self) -> dict:
        """Retrain the risk model on every migration (pipeline aap-migration-train, read-only for AAP)."""
        return self.start_scan("train", self.migration_id, False)

    def model_status(self) -> dict:
        info = self.model_info_fn() if self.model_info_fn else {}
        card = self.work / "ai" / "model-card.md"
        out = {"latest_model": info or None, "model_card_available": card.is_file(),
               "this_migration_in_training_data": bool(info and self.migration_id in (info.get("migrations") or []))}
        if not info:
            out["note"] = "no trained model yet: train after at least one finished migration"
        return out

    def list_reports(self, migration_id: str = "") -> dict:
        mid = migration_id or self.migration_id
        if not self.reports_fn:
            return {"error": "reports are not available here"}
        data = self.reports_fn(mid)
        self.view = {"type": "reports", "migration_id": mid, **data}
        return {"migration_id": mid, "phases": data.get("phases"),
                "reports": [{k: r.get(k) for k in ("phase", "title", "source", "filename", "generated")}
                            for r in data.get("reports", [])],
                "casc": [{k: c.get(k) for k in ("title", "errors", "stamp")} for c in data.get("casc", [])],
                "smoke": [{k: c.get(k) for k in ("title", "stamp")} for c in data.get("smoke", [])]}

    def migrations_overview(self) -> dict:
        if not self.migrations_fn:
            return {"error": "the migration list is not available here"}
        items = self.migrations_fn()
        self.view = {"type": "migrations", "items": items}
        return {"migrations": items}

    def installed_version(self) -> dict:
        from migration_ai import releases

        env = toolkit.load_env_file(self.settings.toolkit_env_file)
        verify = env.get("TARGET__VERIFY_SSL", "true").lower() != "false"
        res = releases.installed_version(env.get("TARGET__URL", ""), verify)
        if res.get("controller_version"):
            fetcher = releases.fetcher_for(self.data_dir / "_cache" / "releases", self.settings.s3)
            res["aap_version"] = releases.aap_version_for(res["controller_version"], fetcher) or None
        return res

    def patch_releases(self, version: str = "", compare_installed: bool = True, question: str = "") -> dict:
        """Smart Planning agent: patch releases of an AAP version from docs.redhat.com.

        version "" = the version of the installed target AAP (2.5, 2.6, 2.7...), detected from the
        controller version of the anonymous ping. The installed release is compared only when the
        requested version is the installed one."""
        from migration_ai import releases

        version = (version or "").strip()
        fetcher = releases.fetcher_for(self.data_dir / "_cache" / "releases", self.settings.s3)
        inst = self.installed_version() if (compare_installed or not version) else {}
        cv = inst.get("controller_version") or ""
        detected = inst.get("aap_version") or ""
        note = ""
        if not version:
            if detected:
                version = detected
            else:
                version = max(releases.urls(), key=lambda v: [int(x) for x in re.findall(r"\d+", v)])
                note = ("installed AAP version not detected"
                        + (f" ({inst['error']})" if inst.get("error") else f" (controller {cv})" if cv else "")
                        + f": showing AAP {version}")
        compare = bool(cv) and detected in ("", version)
        plan = releases.build_plan(version, fetcher, controller_version=cv if compare else "")
        plan["installed_check"] = inst or None
        plan["note"] = note or None
        text = releases.plan_markdown(plan)
        ids = releases.ID_RE.findall(question or "")
        found = releases.find_ids(version, fetcher, ids) if ids else None
        direct = releases.direct_answer(plan, question, found)
        summary, rejected = ("", []) if ids else self._planning_summary(plan, question, direct)
        saved = None
        if plan.get("installed") and self.migration_id:
            out_dir = self.work / "planning"
            out_dir.mkdir(parents=True, exist_ok=True)
            for old in out_dir.glob("patch-plan-*"):
                old.unlink()
            md = out_dir / f"patch-plan-{version}.md"
            md.write_text("\n\n".join(x for x in (direct, summary) if x) + ("\n\n---\n\n" if direct or summary else "")
                          + text + "\n", encoding="utf-8")
            (out_dir / f"patch-plan-{version}.json").write_text(json.dumps(plan, indent=1, default=str),
                                                                encoding="utf-8")
            saved = md.relative_to(self.work).as_posix()
            if self.publish_fn:
                try:
                    self.publish_fn(self.migration_id, out_dir, "planning", replace=True)
                except TypeError:
                    self.publish_fn(self.migration_id, out_dir, "planning")
        self.view = {"type": "releases", "plan": plan, "summary": summary, "file": saved}
        return {"plan_markdown": text, "direct_answer": direct or None, "llm_summary": summary or None,
                "llm_rejected": rejected or None, "saved": saved,
                "installed": plan.get("installed"), "rule": plan["rule"]}

    def _planning_summary(self, plan: dict, question: str, direct: str = "") -> tuple[str, list[str]]:
        """Short note of the LLM from compact facts, checked against them; cached; skipped after
        PLANNING_SUMMARY_TIMEOUT. Returns (note, rejected facts): a note citing ids or releases that are not
        in the release notes, or contradicting the computed state, is dropped."""
        from migration_ai import releases

        if not (self.llm and self.llm.configured):
            return "", []
        facts = releases.compact_facts(plan, question)
        prompt = releases.SUMMARY_PROMPT + (releases.QUESTION_PROMPT.format(question=question[:500],
                                                                             direct=direct or "(none)")
                                            if question else "")
        payload = json.dumps(facts, default=str)
        key = hashlib.sha256((self.llm.settings.model + prompt + payload).encode()).hexdigest()[:24]
        cache = self.data_dir / "_cache" / "planning-answers" / f"{key}.md"
        if cache.is_file():
            return cache.read_text(encoding="utf-8"), []
        timeout = float(os.environ.get("PLANNING_SUMMARY_TIMEOUT") or 90)
        try:
            summary = self.llm.complete(prompt, payload, max_tokens=600, timeout=timeout)
        except Exception as exc:
            log.warning("planning summary without LLM: %s", exc)
            return "", []
        summary = releases.drop_repeats(summary, direct)
        bad = releases.check_summary(summary, payload + direct + json.dumps(plan, default=str), plan)
        if bad:
            log.warning("planning note of the LLM rejected: %s", bad)
            return "", bad
        if summary:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(summary, encoding="utf-8")
        return summary, []

    def smoke_suites(self) -> list[dict]:
        """Suites already generated for this migration (newest first)."""
        from migration_ai.webapp import list_smoke

        return list_smoke(self.work)

    def _existing_suite(self) -> dict | None:
        for d in sorted((p for p in (self.work / "smoke").glob("*") if p.is_dir() and p.name != "results"),
                        reverse=True):
            zip_path = d.with_suffix(".zip")
            if (d / "suite-summary.json").is_file() and zip_path.is_file():
                try:
                    res = json.loads((d / "suite-summary.json").read_text(encoding="utf-8"))
                except ValueError:
                    continue
                return res | {"output_dir": str(d), "zip": str(zip_path)}
        return None

    def generate_smoke_suite(self, force: bool = False) -> dict:
        """Smoke & regression test agent: generate the platform test suite (the human runs it).

        A suite that already exists is reused unless force=True: every generation is a new zip in Reports."""
        from migration_ai import smoke

        existing = None if force else self._existing_suite()
        if existing:
            review_file = Path(existing["output_dir"]) / "AI-REVIEW.md"
            review = review_file.read_text(encoding="utf-8").strip() if review_file.is_file() else ""
            self.files.append(existing["zip"])
            self.view = {"type": "smoke", "suite": {k: existing[k] for k in ("stamp", "tests", "customization",
                                                                            "features", "validation")},
                         "zip": existing["zip"], "review": review, "results": self._smoke_results_view()}
            return {k: existing[k] for k in ("stamp", "tests", "customization", "features", "validation")} | {
                "review": review or None, "download": Path(existing["zip"]).name, "reused": True}
        res = smoke.generate_suite(self.work, self.migration_id)
        review = ""
        if self.llm and self.llm.configured:
            try:
                objs = (Path(res["output_dir"]) / "objects" / "smoke-objects.yml").read_text(encoding="utf-8")
                review = self.llm.complete(
                    "You review a platform smoke test suite of the target AAP before a human runs it. In at most 6 English "
                    "bullets list what the person must customize or check (file and variable), and any risk. Use only "
                    "the facts given; never invent variables.",
                    json.dumps({"tests": res["tests"], "customization": res["customization"],
                                "features": res["features"], "validation": res["validation"]})[:12000]
                    + "\n\nobjects/smoke-objects.yml:\n" + objs[:6000],
                    max_tokens=500, timeout=float(os.environ.get("PLANNING_SUMMARY_TIMEOUT") or 90))
            except Exception as exc:
                log.warning("suite review without LLM: %s", exc)
        if review:
            (Path(res["output_dir"]) / "AI-REVIEW.md").write_text(review + "\n", encoding="utf-8")
        self.files.append(res["zip"])
        if self.publish_fn:
            self.publish_fn(self.migration_id, Path(res["output_dir"]).parent, "smoke")
        self.view = {"type": "smoke", "suite": {k: res[k] for k in ("stamp", "tests", "customization", "features",
                                                                    "validation")},
                     "zip": res["zip"], "review": review, "results": self._smoke_results_view()}
        return {k: res[k] for k in ("stamp", "tests", "customization", "features", "validation")} | {
            "review": review or None, "download": Path(res["zip"]).name}

    def _smoke_results_view(self) -> dict:
        from migration_ai import smoke

        runs = smoke.load_results(self.work)
        if not runs:
            return {"runs": []}
        cur = runs[-1]
        base = next((r for r in reversed(runs[:-1]) if smoke.passed(r)), None)
        return {"runs": [{"phase": r["phase"], "finished": r.get("finished"), "summary": r.get("summary"),
                          "passed": smoke.passed(r), "file": r["_file"]} for r in runs],
                "latest": cur, "baseline": base and {"phase": base["phase"], "finished": base.get("finished")},
                "regressions": smoke.compare(base, cur)}

    def smoke_results(self) -> dict:
        res = self._smoke_results_view()
        self.view = {"type": "smoke", "results": res}
        if not res["runs"]:
            return {"runs": [], "note": "no results uploaded yet: run the suite and upload reports/smoke-results.json"}
        return res

    def casc_schema(self, variable: str = "") -> dict:
        return casc_mod.schema_reference(variable or None)

    def tools(self) -> list[Tool]:
        """Every tool; each specialist agent (agents.py) gets only its own subset."""
        s = {"type": "string"}
        obj: dict = {"type": "object", "properties": {}}
        mid = {"type": "string", "description": "migration folder name, default: current migration"}
        out = [
            Tool("overview", "Summary of the migration: agent status, import results, top errors, readiness, AI model.",
                 obj, self.overview),
            Tool("search", "Find objects in the import results by name or id.",
                 {"type": "object", "properties": {"text": s, "status": {"type": "string", "enum": [
                     "", "completed", "failed", "skipped", "pending"]}, "resource_type": s,
                     "limit": {"type": "integer"}}, "required": ["text"]}, self.search),
            Tool("explain", "Why an object failed (or not): error, explanation, AAP 2.4 metadata, rules, AI, logs.",
                 {"type": "object", "properties": {"resource_type": s, "name_or_id": s},
                  "required": ["resource_type", "name_or_id"]}, self.explain),
            Tool("predict", "Risk model predictions (probability of import failure) for an AAP 2.4 to migrate.",
                 {"type": "object", "properties": {"organization": s, "resource_type": s, "top": {"type": "integer"}}},
                 self.predict),
            Tool("generate_casc", "Generate infra.aap_configuration CasC for existing objects "
                 "(source 'migration' = imported objects, 'live' = current target AAP). With an organization, the "
                 "_global scope keeps only the shared objects that organization uses.",
                 {"type": "object", "properties": {
                     "organization": s, "resource_types": {"type": "array", "items": s,
                                                           "description": f"subset of {casc_mod.CASC_TYPES}"},
                     "source": {"type": "string", "enum": ["migration", "live"]},
                     "include_hosts": {"type": "boolean"}}}, self.generate_casc),
            Tool("casc_schema", "Allowed fields of a CasC variable (e.g. controller_templates); no argument lists "
                 "all variables.", {"type": "object", "properties": {"variable": s}}, self.casc_schema),
            Tool("validate_casc", "Validate CasC YAML (a mapping of dispatch variables).",
                 {"type": "object", "properties": {"yaml_text": s}, "required": ["yaml_text"]}, self.validate_casc),
            Tool("journey_status", "Where the migration is in the end-to-end journey: stages done, the current "
                 "stage, the next step, the agent that owns it and whether a human must act.", obj,
                 self.journey_status),
            Tool("migration_status", "Current state of the migration: running/finished pipeline runs (from the "
                 "dashboard too) with their steps, last agent result and import totals.",
                 {"type": "object", "properties": {"migration_id": mid}}, self.migration_status),
            Tool("migration_plan", "The migration plan written by the migration agent (runbook in toolkit order).",
                 obj, lambda: self._fallback_plan([])),
            Tool("readiness", "Pre-migration readiness: predicted success, root causes with their fix, objects that "
                 "will fail (optionally of one organization).", {"type": "object", "properties": {"organization": s}},
                 lambda organization="": self._fallback_readiness(f'"{organization}"' if organization else "", [],
                                                                  detailed=True)),
            Tool("list_reports", "Reports of a migration grouped by phase (pre-migration, migration, post-migration, "
                 "training, prediction), each with its source (toolkit or AI agent), plus CasC and test suites.",
                 {"type": "object", "properties": {"migration_id": mid}}, self.list_reports),
            Tool("migrations_overview", "Every migration with its last activity, journey stage and number of reports.",
                 obj, self.migrations_overview),
            Tool("model_status", "The latest trained risk model: version, migrations used, metrics.", obj,
                 self.model_status),
            Tool("installed_version", "Controller version of the target AAP (anonymous ping) and the matching AAP "
                 "version (2.5, 2.6, 2.7...).", obj,
                 self.installed_version),
            Tool("patch_releases", "Patch releases of an AAP version from the Red Hat documentation: releases "
                 "after today or else the last three, releases newer than the installed target AAP, items relevant "
                 "to the migration, and a patch-scheduling summary. Omit version to use the installed AAP version.",
                 {"type": "object", "properties": {"version": {"type": "string", "enum": ["2.5", "2.6", "2.7"]},
                                                   "compare_installed": {"type": "boolean"}}}, self.patch_releases),
            Tool("generate_smoke_suite", "Generate the platform smoke test suite of the target AAP (brand new objects only, "
                 "never migrated job templates). A human customizes and runs it. An existing suite is reused; force=true "
                 "only when the user explicitly asks for a new one.",
                 {"type": "object", "properties": {"force": {"type": "boolean"}}}, self.generate_smoke_suite),
            Tool("smoke_results", "Uploaded smoke test results (pre / post / patch) and regressions against the "
                 "baseline.", obj, self.smoke_results),
        ]
        if self.scans:
            out += [
                Tool("start_readiness_scan", "Start the READ-ONLY pre-migration analysis (pipeline 'AAP migration - during migration (plan, run, collect)', mode=plan): "
                     "export AAP 2.4, pre-migration report, dependency analysis, migration plan.",
                     {"type": "object", "properties": {"migration_id": mid, "refresh_export": {"type": "boolean"}}},
                     self.start_readiness_scan),
                Tool("start_prediction", "Start the AI readiness report pipeline (predict) for a migration.",
                     {"type": "object", "properties": {"migration_id": mid, "refresh_export": {"type": "boolean"}}},
                     self.start_prediction),
                Tool("start_training", "Start the training pipeline: retrain the risk model on every migration.",
                     obj, self.start_training),
                Tool("scan_status", "State of a pipeline run started from AAP Odyssey (PENDING, RUNNING, SUCCEEDED, "
                     "FAILED); without run_id the latest of the migration.",
                     {"type": "object", "properties": {"run_id": s, "migration_id": s}}, self.scan_status),
            ]
        return out

    def ask(self, question: str, history: list[dict] | None = None) -> dict:
        events: list[dict] = []
        self.files = []
        if self.llm and self.llm.configured:
            try:
                answer, _ = self.llm.run_tools(SYSTEM_PROMPT.replace("{language}", self.language), question,
                                               self.tools(), max_turns=12, on_event=events.append,
                                               history=history)
                return {"answer": answer, "events": events, "files": self.files, "llm": True}
            except Exception as exc:
                log.warning("LLM unavailable, using the keyword router: %s", exc)
                events.append({"type": "llm_error", "error": str(exc)})
        return {"answer": self._fallback(question, events), "events": events, "files": self.files, "llm": False}

    def _fallback(self, q: str, events: list[dict]) -> str:
        ql = q.lower()
        if self.scans and re.search(r"\bscan", ql):
            return self._fallback_scan(q, ql, events)
        if re.search(r"\b(status|progress|where are we|running)\b", ql):
            return self._fallback_status(q, events)
        if "casc" in ql or "configuration as code" in ql:
            org = _quoted(q) or ""
            res = self.generate_casc(organization=org)
            events.append({"type": "tool_call", "tool": "generate_casc", "args": {"organization": org}})
            if "error" in res:
                return res["error"]
            first = next(((k, v) for k, v in res["preview"].items() if "job_templates" in k),
                         next(iter(res["preview"].items()), ("", "")))
            return (f"CasC generated in `{res['output_dir']}` (scope: {', '.join(res['scopes'])}). "
                    f"Validation errors: {len(res['validation']['errors'])}.\n\n**{first[0]}**\n```yaml\n"
                    f"{first[1]}\n```")
        name = _quoted(q)
        if re.search(r"\b(plan|runbook)\b", ql):
            return self._fallback_plan(events)
        about_object = bool(name) and re.search(r"\b(why|explain|details?|status of|what about)\b", ql)
        if not about_object and re.search(r"\b(risk|risky|predict|prediction|likely to fail)\b", ql):
            return self._fallback_risk(q, events)
        if not about_object and re.search(r"\b(will fail|blocking|root causes?|readiness|fix first|stale)\b", ql):
            return self._fallback_readiness(q, events, detailed=True)
        if name:
            for r in self.db.find(name, 5):
                if r.get("phase") == "import":
                    d = self.explain(r["resource_type"], name)
                    events.append({"type": "tool_call", "tool": "explain", "args": {"name_or_id": name}})
                    lines = [f"**{d['resource']['source_name']}** ({d['resource']['resource_type']}): import status "
                             f"`{d['resource']['status']}`."]
                    if d["error_message"]:
                        lines += ["", f"* **Error:** {d['error_class']}", f"* **Why:** {d['explanation'] or 'n/a'}",
                                  f"* **Raw message:** `{d['error_message'][:300]}`"]
                    lines += [f"* **Pre-migration {f['rc']}** ({f['severity']}): {f['title']}. **Fix:** {f['remediation']}"
                              for f in d["premigration_findings"]]
                    return "\n".join(lines)
            found = self._object_findings(name)
            if found:
                events.append({"type": "tool_call", "tool": "premigration_findings", "args": {"name": name}})
                return found
        return self._fallback_summary(events)

    _HINTS = ("Try: *summarize the result*, *show the migration plan*, *what will fail and why?*, "
              "*why will \"Name\" fail?*, *which objects have the highest risk?*, "
              "*generate the casc of \"Organization\"*, *scan status*.")

    def _premig(self) -> dict:
        return self._json("reports/premigration-report.json") or {}

    def _readiness_lines(self, top: int = 8) -> list[str]:
        rep = self._premig()
        s = rep.get("summary") or {}
        if not s:
            return ["No pre-migration report for this migration yet: start a scan "
                    "(*scan AAP 2.4 for migration readiness*)."]
        labels = rep.get("outcome_labels") or {}
        cat = rep.get("rc_catalog") or {}
        lines = [f"**Pre-migration readiness:** predicted first-attempt success **{s.get('predicted_success_pct')}%** "
                 f"of {s.get('total_resources')} objects in {s.get('organizations')} organizations.", "",
                 "| Outcome | Objects |", "|---|---:|"]
        lines += [f"| {labels.get(k, k)} | {v} |" for k, v in (s.get("outcomes") or {}).items()]
        rcs = sorted(((k, v) for k, v in (s.get("resources_by_rc") or {}).items() if v), key=lambda x: -x[1])[:top]
        if rcs:
            lines += ["", "| Code | Root cause | Objects | Fix before migrating |", "|---|---|---:|---|"]
            for rc, n in rcs:
                c = cat.get(rc, {})
                lines.append(f"| {rc} | {c.get('title', rc)} | {n} | {(c.get('remediation') or '')[:160]} |")
        return lines

    def _fallback_summary(self, events: list[dict]) -> str:
        events.append({"type": "tool_call", "tool": "overview", "args": {}})
        o = self.overview()
        lines = [f"**Migration {self.migration_id}** — agent: `{o['agent'].get('mode') or 'n/a'}` / "
                 f"`{o['agent'].get('status') or 'n/a'}`.", ""]
        lines += self._readiness_lines()
        if o["import_results"]:
            totals = ", ".join(f"{k}: {v}" for k, v in sorted(o["import_results"].items()))
            lines += ["", f"**Import results:** {totals}.", "", "| Type | Error | Objects | Example |",
                      "|---|---|---:|---|"]
            lines += [f"| {e['resource_type']} | {e['error_key']} | {e['count']} | {e['example']} |"
                      for e in o["top_errors"]]
        else:
            lines += ["", "**Import results:** none yet — this migration has not been executed "
                      "(start it from the dashboard: pipeline *AAP migration - during migration (plan, run, collect)*)."]
        lines += ["", "ℹ️ Automatic answer built from the migration data (no language model connected). "
                  + self._HINTS]
        return "\n".join(lines)

    def _fallback_readiness(self, q: str, events: list[dict], detailed: bool = False) -> str:
        events.append({"type": "tool_call", "tool": "premigration_report", "args": {}})
        lines = self._readiness_lines(top=12)
        rep = self._premig()
        crit = [f for f in rep.get("findings", []) if f.get("severity") == "critical"]
        org = _quoted(q)
        if org:
            crit = [f for f in crit if (f.get("organization") or "").lower() == org.lower()]
        if detailed and crit:
            lines += ["", f"**Objects that will fail** ({len(crit)}{', organization ' + org if org else ''}; first 20):",
                      "", "| Code | Type | Object | Organization | Problem |", "|---|---|---|---|---|"]
            lines += [f"| {f['rc']} | {f['resource_type']} | {f['resource_name']} | {f.get('organization') or ''} | "
                      f"{f['title']} |" for f in crit[:20]]
        return "\n".join(lines)

    def _fallback_plan(self, events: list[dict]) -> str:
        events.append({"type": "tool_call", "tool": "migration_plan", "args": {}})
        plan = self.work / "reports/agent/migration-plan.md"
        if not plan.is_file():
            return "No migration plan yet: start a pre-migration analysis (*scan AAP 2.4 for migration readiness*)."
        text = plan.read_text(encoding="utf-8")
        return text if len(text) < 7000 else text[:7000] + "\n\n… (truncated: full plan in the run artifacts)"

    def _fallback_risk(self, q: str, events: list[dict]) -> str:
        org = _quoted(q) or ""
        events.append({"type": "tool_call", "tool": "predict", "args": {"organization": org}})
        res = self.predict(organization=org)
        if "error" in res:
            return (res["error"] + ". The risk model needs at least one finished migration "
                    "(pipeline *AAP migration - train risk model*); meanwhile ask *what will fail and why?* "
                    "for the rule-based pre-migration assessment.")
        rows = res["highest_risk"]
        if not rows:
            return f"No objects with a risk prediction{' in ' + org if org else ''}."
        lines = [f"**Highest predicted risk** (model `{res.get('model')}`{', organization ' + org if org else ''}):", "",
                 "| Type | Object | Organization | P(fail) | Likely error | Rules |", "|---|---|---|---:|---|---|"]
        lines += [f"| {r['resource_type']} | {r['name']} | {r['organization']} | {r['p_fail']:.0%} | "
                  f"{r.get('predicted_error') or ''} | {', '.join(r.get('rcs') or [])} |" for r in rows]
        return "\n".join(lines)

    def _object_findings(self, name: str) -> str:
        rep = self._premig()
        res = [r for r in (rep.get("resources") or {}).values() if (r.get("name") or "").lower() == name.lower()]
        if not res:
            return ""
        out = []
        labels = rep.get("outcome_labels") or {}
        for r in res[:3]:
            fs = [f for f in rep.get("findings", []) if f.get("key") == r["key"]]
            out.append(f"**{r['name']}** ({r['resource_type']}, organization {r.get('organization') or 'n/a'}): "
                       f"pre-migration outcome **{labels.get(r['outcome'], r['outcome'])}**"
                       + (", stale" if r.get("stale") else "") + ".")
            for f in fs:
                out.append(f"* {f['rc']} ({f['severity']}): {f['title']}. {f['detail']} "
                           f"**Fix:** {f['remediation']}")
            if not fs:
                out.append("* No pre-migration finding: expected to migrate cleanly.")
        return "\n".join(out)

    def _fallback_status(self, q: str, events: list[dict]) -> str:
        events.append({"type": "tool_call", "tool": "migration_status", "args": {}})
        st = self.migration_status(_quoted(q) or "")
        lines = [f"**Migration {st['migration_id']}: current status**", ""]
        if st.get("active_run"):
            r = st["active_run"]
            lines += [f"**In progress:** run `{r['name']}` — **{r['state']}** (started {r.get('created_at') or 'n/a'})."]
            if st.get("active_steps"):
                lines += ["", "| Step | State |", "|---|---|"]
                lines += [f"| {t['step']} | {t['state']} |" for t in st["active_steps"]]
            lines += ["", "Per-object import progress is saved at the end of the agent step: follow it live "
                      "in the dashboard (run → step *2. Migration agent* → Logs)."]
        elif st.get("runs") is not None:
            lines.append("No pipeline run in progress for this migration.")
        if st.get("runs"):
            lines += ["", "**Latest runs:**", "", "| Run | Mode | Dry run | State | Started |", "|---|---|---|---|---|"]
            lines += [f"| {r['name']} | {r.get('mode') or ''} | {'' if r.get('dry_run') is None else r['dry_run']} | "
                      f"{r['state']} | {r.get('created_at') or ''} |" for r in st["runs"]]
        if st.get("runs_error"):
            lines += ["", f"(pipeline server not reachable: {st['runs_error'][:200]})"]
        a = st["last_agent_result"]
        if a.get("status"):
            lines += ["", f"**Last agent result:** mode `{a.get('mode')}`{' (dry run)' if a.get('dry_run') else ''}, "
                      f"status **{a.get('status')}**" + (f" — {a['stop_reason']}" if a.get("stop_reason") else "")
                      + (f", finished {a['finished']}" if a.get("finished") else "") + "."]
            if st["last_agent_steps"]:
                lines += ["", "| Step | Status |", "|---|---|"]
                lines += [f"| {x['step']} | {x['status']} |" for x in st["last_agent_steps"]]
        totals = ", ".join(f"{k}: {v}" for k, v in sorted(st["import_results"].items()))
        lines += ["", f"**Import results:** {totals or 'none yet'}."]
        return "\n".join(lines)

    def _fallback_scan(self, q: str, ql: str, events: list[dict]) -> str:
        if re.search(r"\b(status|progress)\b", ql) and not re.search(r"\b(start|run|launch)\b", ql):
            res = self.scan_status(migration_id=_quoted(q) or "")
            events.append({"type": "tool_call", "tool": "scan_status", "args": {}})
            if "error" in res:
                return res["error"]
            r = res["run"]
            text = f"Scan `{r['name']}`: **{r['state']}**."
            if r.get("error"):
                text += f" Error: {r['error']}"
            results = res.get("results")
            if results and results.get("premigration"):
                p = results["premigration"]
                text += (f"\n\nPredicted first-attempt success: **{p.get('predicted_success_pct')}%** "
                         f"({_fmt_outcomes(p.get('outcomes'))}). Open the pre-migration report and the "
                         "migration plan with the **Reports** button at the top, or ask *summarize the result*, "
                         "*what will fail and why?*, *show the migration plan*.")
            if results and results.get("ai_readiness"):
                text += "\n\nAI readiness report ready: ask *which objects have the highest risk?*"
            return text
        kind = "predict" if re.search(r"\b(predict|prediction|risk|ai readiness)\b", ql) else "readiness"
        mid = _quoted(q) or self.migration_id
        refresh = not re.search(r"\b(existing|without export|no export)\b", ql)
        res = self.start_scan(kind=kind, migration_id=mid, refresh_export=refresh)
        events.append({"type": "tool_call", "tool": "start_scan", "args": {"kind": kind, "migration_id": mid}})
        if "error" in res:
            return res["error"]
        r = res["run"]
        if res["status"] == "already_running":
            return f"A {kind} scan of `{mid}` is already running: `{r['name']}` ({r['state']}). Ask *scan status*."
        what = ("export from AAP 2.4, pre-migration report, dependency analysis and migration plan"
                if kind == "readiness" else "AI readiness report from the trained risk model")
        return (f"Started a read-only **{kind}** scan of `{mid}` ({what}).\n\n"
                f"* Run: `{r['name']}` — {r['state'] or 'PENDING'}\n"
                f"* Nothing is written to the target AAP.\n\n"
                "Ask *scan status* to follow it; the run is also visible in the OpenShift AI dashboard.")


def _fill(text: str, values: dict) -> str:
    """Fill the {placeholders} of the toolkit's error explanations with what we know."""
    def sub(m):
        v = values.get(m.group(1))
        return str(v) if v not in (None, "") else m.group(1).replace("_", " ")
    return re.sub(r"\{(\w+)\}", sub, text or "")


def _fmt_outcomes(o: dict | None) -> str:
    return ", ".join(f"{k}: {v}" for k, v in (o or {}).items()) or "n/a"


def _quoted(text: str) -> str | None:
    m = re.search(r"['\"“”‘’]([^'\"“”‘’]+)['\"“”‘’]", text)
    return m.group(1) if m else None


def answer_once(copilot: Copilot, question: str) -> str:
    return copilot.ask(question)["answer"]


def tool_names(copilot: Copilot) -> list[str]:
    return [t.name for t in copilot.tools()]


def as_json(data: Any) -> str:
    return json.dumps(data, indent=1, default=str)
