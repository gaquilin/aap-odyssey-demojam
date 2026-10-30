"""AAP Odyssey web UI (standard library HTTP server).

Runs behind the OpenShift oauth-proxy sidecar (only users allowed in the
namespace reach it). Endpoints:

  GET  /                                      chat page (PatternFly)
  GET  /static/...                            vendored PatternFly CSS and fonts
  GET  /api/migrations                        migrations in the S3 bucket, newest first
  GET  /api/progress?migration_id=...         where the migration is in the journey (stages, next step)
  POST /api/chat                              {"migration_id", "message", "history"} -> answer, agent activity,
                                              view, progress (orchestrator + specialist agents, see agents.py)
  POST /api/smoke-results?migration_id=...    upload reports/smoke-results.json of the smoke test suite
  GET  /api/agents                            the specialist agents (role, tools, what they may write)
  GET  /api/overview                          every migration with its journey position
  GET  /api/reports?migration_id=...          reports grouped by phase + generated CasC and test suites
  GET  /report?migration_id=...&file=...      open one report (served through the OAuth proxy, so it
                                              works even when the browser cannot reach the S3 storage)
  GET  /api/download?file=...                 download a generated CasC or test suite archive
  GET  /healthz
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from migration_ai import agents as agents_mod
from migration_ai import progress as progress_mod
from migration_ai.assistant import Copilot, _quoted
from migration_ai.config import load_settings

log = logging.getLogger(__name__)
EXAMPLE_MIGRATION_ID = "test-migration-01"
GLOBAL_INTENTS = {("migration", "start_request"), ("planning", "installed"), ("planning", "patches"), ("reporting", "all_reports"),
                  ("training", "model"), ("training", "train"), ("testing", "smoke_suite"),
                  ("testing", "smoke_info"), ("copilot", "journey"), ("copilot", "intro")}
NO_MIGRATION_ERROR = ("This question needs a migration: choose one in the Migration menu, or start one with a name in "
                      'quotes, e.g. Scan AAP 2.4 for migration readiness as "' + EXAMPLE_MIGRATION_ID + '". '
                      "Without a migration you can ask: the patch releases of the installed AAP, the installed "
                      "AAP version, the reports of all migrations, the risk model status, the smoke test suite, "
                      "what's next.")
_LOCK = threading.Lock()
REFRESH_SECONDS = 30


class State:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.data_dir = Path(os.environ.get("COPILOT_DATA_DIR", "/data"))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.default_migration = os.environ.get("COPILOT_MIGRATION_ID", "")
        self.pulled: dict[str, float] = {}
        self.model_path: Path | None = None
        self.latest_model: dict | None = None
        self.model_checked = 0.0
        self.runs_cache: dict[str, tuple[float, list[dict] | None]] = {}
        from migration_ai.scans import PipelinesAPI, ScanSettings

        scan_settings = ScanSettings()
        self.scans = PipelinesAPI(scan_settings) if scan_settings.enabled else None

    def store(self):
        from migration_ai.storage import Store

        return Store(self.settings.s3) if self.settings.s3.configured else None

    def migrations(self) -> list[dict]:
        """[{id, updated}] newest first; 'updated' = last agent or report activity in S3."""
        st = self.store()
        out: list[dict] = []
        if st:
            try:
                base = self.settings.migrations_prefix
                for mid in st.list_children(base):
                    dates = [st.last_modified(f"{base}/{mid}/{k}") for k in (
                        "agent/agent-summary.json", "workspace/reports/premigration-report.json",
                        "ai/ai-readiness-report.json")]
                    dates = [d for d in dates if d]
                    out.append({"id": mid, "updated": max(dates).isoformat() if dates else None})
            except Exception as exc:
                log.warning("cannot list migrations: %s", exc)
        else:
            for p in self.data_dir.iterdir():
                if p.is_dir() and not p.name.startswith("_"):
                    out.append({"id": p.name, "updated": datetime.fromtimestamp(
                        p.stat().st_mtime, tz=timezone.utc).isoformat()})
        out.sort(key=lambda m: (m["updated"] or "", m["id"]), reverse=True)
        return out

    def workspace(self, migration_id: str, refresh: bool = False) -> Path:
        from migration_ai.storage import pull_workspace, workspace_prefix

        work = self.data_dir / migration_id
        st = self.store()
        with _LOCK:
            stale = time.time() - self.pulled.get(migration_id, 0) > REFRESH_SECONDS
            if st and (refresh or stale):
                base = f"{self.settings.migrations_prefix}/{migration_id}"
                pull_workspace(st, workspace_prefix(self.settings.migrations_prefix, migration_id), work, sync=True)
                st.download_dir(f"{base}/agent", work / "reports" / "agent", sync=True)
                st.download_dir(f"{base}/ai", work / "ai", sync=True)
                st.download_dir(f"{base}/casc", work / "casc")
                st.download_dir(f"{base}/planning", work / "planning", sync=True)
                st.download_dir(f"{base}/smoke", work / "smoke")
                st.download_dir(f"{base}/journey", work / "journey")
                self.pulled[migration_id] = time.time()
            if st and (self.model_path is None or refresh or time.time() - self.model_checked > 60):
                from migration_ai.predict import fetch_latest_model

                try:
                    got = fetch_latest_model(self.settings, self.data_dir / "_models")
                    self.model_path, self.latest_model = got if got else (None, None)
                except Exception as exc:
                    log.warning("cannot read the latest model: %s", exc)
                self.model_checked = time.time()
            card = self.model_path.parent / "model-card.md" if self.model_path else None
            if card and card.is_file():
                (work / "ai").mkdir(parents=True, exist_ok=True)
                shutil.copy2(card, work / "ai" / "model-card.md")
        return work

    def runs(self, migration_id: str) -> list[dict] | None:
        """Pipeline runs of the migration (cached 15 s); None when the pipeline API is not reachable."""
        if not self.scans:
            return None
        at, runs = self.runs_cache.get(migration_id, (0.0, None))
        if time.time() - at > 15:
            try:
                runs = self.scans.runs_for_migration(migration_id)
            except Exception as exc:
                log.info("pipeline runs not available: %s", exc)
                runs = None
            self.runs_cache[migration_id] = (time.time(), runs)
        return runs

    def progress(self, migration_id: str, work: Path) -> dict:
        return progress_mod.compute(work, migration_id, self.runs(migration_id), self.latest_model)

    def journey_template(self) -> dict:
        """The full journey before any migration exists (all stages pending)."""
        res = progress_mod.compute(self.data_dir / "_no_migration", EXAMPLE_MIGRATION_ID, [], None)
        res["template"] = True
        res["migration_id"] = ""
        res["example_migration_id"] = EXAMPLE_MIGRATION_ID
        return res

    def publish(self, migration_id: str, local_dir: Path, sub: str, replace: bool = False) -> None:
        """Copy what an agent generated (CasC, test suite, patch plan, results) to the bucket.

        replace=True: the folder in the bucket is emptied first (the patch plan keeps one file per version)."""
        st = self.store()
        if not st or not migration_id or not local_dir.is_dir():
            return
        try:
            if replace:
                st.delete_prefix(f"{self.settings.migrations_prefix}/{migration_id}/{sub}/")
            st.upload_dir(local_dir, f"{self.settings.migrations_prefix}/{migration_id}/{sub}")
        except Exception as exc:
            log.warning("cannot copy %s to the bucket: %s", local_dir, exc)

    def model_info(self) -> dict:
        return dict(self.latest_model or {})

    def overview(self) -> list[dict]:
        out = []
        for m in self.migrations():
            item = dict(m)
            try:
                work = self.workspace(m["id"])
                p = self.progress(m["id"], work)
                item.update({"position": p["position"], "total": p["total"], "phase": p["current_phase"],
                             "next": p["next"]["title"], "human": bool(p["next"].get("human")),
                             "reports": len(list_reports(work, m["id"])), "casc": len(list_casc(work)),
                             "smoke": len(list_smoke(work))})
            except Exception as exc:
                item["error"] = str(exc)[:200]
            out.append(item)
        return out

    def reports(self, migration_id: str) -> dict:
        work = self.workspace(migration_id).resolve()
        return {"phases": PHASES, "reports": list_reports(work, migration_id), "casc": list_casc(work),
                "smoke": list_smoke(work)}

    def copilot(self, migration_id: str, work: Path, user: str = "") -> Copilot:
        return Copilot(self.settings, work, migration_id=migration_id, model_path=self.model_path,
                       scans=self.scans, refresh=lambda m: self.workspace(m, refresh=True), user=user,
                       progress_fn=lambda m, w: self.progress(m, w), reports_fn=self.reports,
                       migrations_fn=self.overview, publish_fn=self.publish, model_info_fn=self.model_info,
                       data_dir=self.data_dir)


STATE: State | None = None

PHASES = ["Pre-migration", "Migration", "Post-migration", "Training", "Prediction", "Other"]
REPORT_GLOBS = ("reports/*.html", "reports/validate/**/*.html", "reports/iam/*.html", "reports/local_audit/*.html", "reports/local_migrate/*.html", "reports/agent/*.md",
                "ai/*.html", "ai/model-card.md", "planning/patch-plan-*.md", "smoke/results/*.json")
REPORT_HIDDEN = ("migration_report_",)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _agent_summary(work: Path) -> dict:
    p = work / "reports" / "agent" / "agent-summary.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except ValueError:
        return {}


REPORT_AGENT = {"planning": "planning", "smoke": "testing", "agent": "migration", "model-card.md": "training",
                "ai-readiness-report.html": "training"}


def describe_report(work: Path, rel: str) -> dict:
    """Phase, title, source (Toolkit / AI agent / test suite), agent and short file name of a report."""
    d = _describe(work, rel)
    parts, name = rel.split("/"), rel.rsplit("/", 1)[-1]
    agent = REPORT_AGENT.get(parts[0]) or REPORT_AGENT.get(parts[1] if len(parts) > 2 else "") or REPORT_AGENT.get(name)
    d["agent"] = agent if d["source"] != "Toolkit" else None
    d["agent_name"] = agents_mod.AGENT_NAMES.get(agent) if d["agent"] else None
    return d


def _describe(work: Path, rel: str) -> dict:
    parts, name = rel.split("/"), rel.rsplit("/", 1)[-1]
    if parts[0] == "planning" and name.startswith("patch-plan-"):
        m = re.match(r"patch-plan-(\d+\.\d+)(?:-(\d{8})-\d{6})?\.md$", name)
        ver = m.group(1) if m else ""
        return {"phase": "Pre-migration", "title": f"Patch plan — installed AAP {ver}", "source": "AI agent",
                "group": "patches", "name": f"pre-migration-patch-plan-aap-{ver.replace('.', '')}"}
    if parts[:2] == ["smoke", "results"]:
        m = re.match(r"(\d{8})-(\d{6})(?:-\d+)?-(pre|post|patch)\.json$", name)
        phase = m.group(3) if m else "post"
        label = {"pre": "before the migration", "post": "after the migration", "patch": "after a patch"}[phase]
        return {"phase": "Pre-migration" if phase == "pre" else "Post-migration",
                "title": f"Platform smoke test results — {label}", "source": "Test suite",
                "name": f"{'pre' if phase == 'pre' else 'post'}-migration-smoke-test-results-{phase}-"
                        f"{m.group(1) if m else ''}".rstrip("-")}
    if name == "premigration-report.html":
        return {"phase": "Pre-migration", "title": "Pre-migration readiness report", "source": "Toolkit",
                "name": "pre-migration-readiness-report"}
    if name.startswith("dependenc") and name.endswith(".html"):
        return {"phase": "Pre-migration", "title": "Pre-migration dependency analysis", "source": "Toolkit",
                "name": "pre-migration-dependency-analysis"}
    if name == "migration-plan.md":
        return {"phase": "Pre-migration", "title": "Pre-migration plan", "source": "AI agent",
                "name": "pre-migration-plan"}
    if name == "agent-summary.md":
        s = _agent_summary(work)
        mode, dry = s.get("mode"), s.get("dry_run")
        if mode == "plan":
            return {"phase": "Pre-migration", "title": "Pre-migration analysis summary", "source": "AI agent",
                    "name": "pre-migration-analysis-summary"}
        if mode == "collect":
            return {"phase": "Migration", "title": "Migration data collection summary", "source": "AI agent",
                    "name": "migration-collect-summary"}
        if dry:
            return {"phase": "Migration", "title": "Dry-run summary", "source": "AI agent",
                    "name": "migration-dry-run-summary"}
        return {"phase": "Migration", "title": "Migration run summary", "source": "AI agent",
                "name": "migration-run-summary"}
    if parts[:2] in (["reports", "iam"], ["reports", "local_audit"], ["reports", "local_migrate"]):
        t = {"iam_audit.html": ("Permissions audit (AAP 2.4)", "migration-permissions-audit"),
             "iam_migration.html": ("Permissions migration", "migration-permissions-report"),
             "iam_dry_run.html": ("Permissions migration (dry run)", "migration-permissions-dry-run")}.get(
            name, (f"Permissions report ({name})", "migration-" + _slug(name.rsplit(".", 1)[0])))
        return {"phase": "Migration", "title": t[0], "source": "Toolkit", "name": t[1]}
    if name.startswith("org-") and name.endswith("-enhanced.html"):
        org = name[len("org-"):-len("-enhanced.html")]
        suffix = "" if org == "failures" else f" — organization {org}"
        return {"phase": "Post-migration", "title": "Post-migration enhanced report" + suffix, "source": "Toolkit",
                "name": "post-migration-enhanced-report" + ("" if org == "failures" else f"-{_slug(org)}")}
    if parts[:2] == ["reports", "validate"]:
        scope = parts[2:-1]
        return {"phase": "Post-migration", "title": "Post-migration validation report — " + " · ".join(scope),
                "source": "Toolkit", "name": "post-migration-validation-report-" + _slug("-".join(scope))}
    if name == "model-card.md":
        return {"phase": "Training", "title": "Risk model card (latest model)", "source": "AI agent",
                "name": "training-risk-model-card"}
    if name == "ai-readiness-report.html":
        return {"phase": "Prediction", "title": "Pre-migration agent assessment (AI prediction)",
                "source": "AI agent", "name": "prediction-agent-assessment"}
    base = name.rsplit(".", 1)[0]
    return {"phase": "Other", "title": base.replace("_", " ").replace("-", " ").capitalize(), "source": "Toolkit",
            "name": _slug(base)}


def list_reports(work: Path, migration_id: str = "") -> list[dict]:
    mid = migration_id or work.name
    out = []
    for pattern in REPORT_GLOBS:
        for p in sorted(work.glob(pattern)):
            if not p.is_file() or p.name.startswith(REPORT_HIDDEN):
                continue
            rel = p.relative_to(work).as_posix()
            d = describe_report(work, rel)
            ext = ".html" if p.suffix in (".html", ".md") else p.suffix
            out.append({"file": rel, "migration_id": mid, **d, "filename": f"{mid}__{d['name']}{ext}",
                        "generated": datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat(
                            timespec="minutes"), "size": p.stat().st_size})
    out.sort(key=lambda r: (PHASES.index(r["phase"]), r["title"]))
    return out


CASC_FILES = ("README.md", "AI-REVIEW.md", "casc-summary.json", "configure.yml", "requirements.yml",
              "env_variables.yml")


def list_casc(work: Path) -> list[dict]:
    """Generated Configuration as Code (pipeline 'AAP migration - Configuration as Code' or AAP Odyssey), newest first."""
    out = []
    root = work / "casc"
    if not root.is_dir():
        return out
    for d in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
        files = [d / f for f in CASC_FILES if (d / f).is_file()] + sorted((d / "orgs").rglob("*.yml"))
        zip_path = d.with_suffix(".zip")
        summary = {}
        if (d / "casc-summary.json").is_file():
            try:
                summary = json.loads((d / "casc-summary.json").read_text(encoding="utf-8"))
            except ValueError:
                summary = {}
        out.append({"stamp": d.name, "phase": "Configuration as Code", "source": "AI agent",
                    "title": f"Configuration as Code — {d.name}", "source_type": summary.get("source"),
                    "errors": len((summary.get("validation") or {}).get("errors", [])),
                    "zip": str(zip_path.resolve()) if zip_path.is_file() else None,
                    "files": [f.relative_to(work).as_posix() for f in files]})
    return out


def list_smoke(work: Path) -> list[dict]:
    """Smoke test suites generated by the test agent, newest first."""
    out = []
    root = work / "smoke"
    if not root.is_dir():
        return out
    for d in sorted((p for p in root.iterdir() if p.is_dir() and p.name != "results"), reverse=True):
        summary = {}
        if (d / "suite-summary.json").is_file():
            try:
                summary = json.loads((d / "suite-summary.json").read_text(encoding="utf-8"))
            except ValueError:
                summary = {}
        zip_path = d.with_suffix(".zip")
        files = [d / f for f in ("README.md", "AI-REVIEW.md") if (d / f).is_file()] + \
            sorted((d / "playbooks").glob("*.yml")) + sorted((d / "objects").glob("*.yml")) + \
            sorted((d / "vars").glob("*.yml"))
        out.append({"stamp": d.name, "phase": "Post-migration", "source": "AI agent", "agent": "testing",
                    "title": f"Platform smoke test suite — {d.name}",
                    "tests": len(summary.get("tests") or []),
                    "errors": len((summary.get("validation") or {}).get("errors", [])),
                    "zip": str(zip_path.resolve()) if zip_path.is_file() else None,
                    "files": [f.relative_to(work).as_posix() for f in files]})
    return out


def _md_page(text: str, title: str) -> bytes:
    import html

    return (f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
            "<style>body{font:15px/1.5 'Red Hat Text',system-ui,sans-serif;max-width:1000px;margin:24px auto;"
            "padding:0 16px}pre{white-space:pre-wrap}</style></head><body><pre>"
            f"{html.escape(text)}</pre></body></html>").encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "migration-copilot/0.3"

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def _json(self, code: int, data) -> None:
        body = json.dumps(data, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _mid(self, q: dict) -> str | None:
        from migration_ai.scans import valid_migration_id

        mid = (q.get("migration_id", [""])[0] or "").strip()
        return mid if valid_migration_id(mid) else None

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path in ("/healthz", "/readyz"):
            return self._json(200, {"status": "ok"})
        if url.path == "/":
            html = resources.files("migration_ai").joinpath("web/index.html").read_bytes()
            return self._bytes(html, "text/html; charset=utf-8", {"Cache-Control": "no-store"})
        if url.path.startswith("/static/"):
            root = Path(str(resources.files("migration_ai").joinpath("web/static"))).resolve()
            target = (root / url.path[len("/static/"):]).resolve()
            if root not in target.parents or not target.is_file():
                return self._json(404, {"error": "not found"})
            ctype = mimetypes.guess_type(target.name)[0] or ("font/woff2" if target.suffix == ".woff2"
                                                             else "application/octet-stream")
            return self._bytes(target.read_bytes(), ctype, {"Cache-Control": "public, max-age=86400"})
        if url.path == "/api/migrations":
            migs = STATE.migrations()
            return self._json(200, {"migrations": [m["id"] for m in migs], "items": migs,
                                    "default": STATE.default_migration,
                                    "llm": STATE.settings.llm.model if STATE.settings.llm.configured else None})
        if url.path == "/api/agents":
            return self._json(200, {"agents": agents_mod.catalog(),
                                    "llm": STATE.settings.llm.model if STATE.settings.llm.configured else None})
        if url.path == "/api/overview":
            return self._json(200, {"migrations": STATE.overview()})
        if url.path == "/api/progress":
            if not (q.get("migration_id", [""])[0] or "").strip():
                return self._json(200, STATE.journey_template())
            mid = self._mid(q)
            if not mid:
                return self._json(400, {"error": "invalid migration_id"})
            work = STATE.workspace(mid, refresh=q.get("refresh", ["0"])[0] == "1")
            return self._json(200, STATE.progress(mid, work))
        if url.path in ("/api/reports", "/report"):
            mid = self._mid(q)
            if not mid:
                return self._json(400, {"error": "invalid migration_id"})
            work = STATE.workspace(mid, refresh=q.get("refresh", ["0"])[0] == "1").resolve()
            reports = list_reports(work, mid)
            if url.path == "/api/reports":
                return self._json(200, {"migration_id": mid, "phases": PHASES, "reports": reports,
                                        "casc": list_casc(work), "smoke": list_smoke(work)})
            rel = q.get("file", [""])[0]
            names = {r["file"]: r["filename"] for r in reports}
            allowed = set(names) | {f for c in list_casc(work) + list_smoke(work) for f in c["files"]}
            target = (work / rel).resolve()
            if rel not in allowed or work not in target.parents or not target.is_file():
                return self._json(404, {"error": "report not found"})
            if target.suffix == ".md":
                body, ctype = _md_page(target.read_text(encoding="utf-8"), names.get(rel, target.name)), \
                    "text/html; charset=utf-8"
            elif target.suffix in (".yml", ".yaml", ".json"):
                body, ctype = target.read_bytes(), "text/plain; charset=utf-8"
            else:
                body, ctype = target.read_bytes(), "text/html; charset=utf-8"
            fname = names.get(rel) or f"{mid}__{rel.replace('/', '__')}"
            return self._bytes(body, ctype, {"Content-Disposition": f'inline; filename="{fname}"'})
        if url.path == "/api/download":
            target = Path(q.get("file", [""])[0]).resolve()
            root = STATE.data_dir.resolve()
            if root not in target.parents or not target.is_file() or target.suffix != ".zip":
                return self._json(404, {"error": "not found"})
            rel_parts = target.relative_to(root).parts
            mid = rel_parts[0]
            kind = "smoke-tests" if "smoke" in rel_parts else "casc"
            name = f"{mid}__{kind}__{target.stem}.zip"
            return self._bytes(target.read_bytes(), "application/zip",
                               {"Content-Disposition": f'attachment; filename="{name}"'})
        return self._json(404, {"error": "not found"})

    def _body(self, limit: int) -> tuple[dict | None, str | None]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            return None, "too large"
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return None, "invalid JSON"
        return (data, None) if isinstance(data, dict) else (None, "a JSON object is required")

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/api/smoke-results":
            return self._smoke_results(parse_qs(url.query))
        if url.path != "/api/chat":
            return self._json(404, {"error": "not found"})
        req, err = self._body(256 * 1024)
        if err:
            return self._json(413 if err == "too large" else 400, {"error": err})
        msg = (req.get("message") or "").strip()
        mid = (req.get("migration_id") or STATE.default_migration or "").strip()
        if not mid:
            from migration_ai.scans import valid_migration_id

            quoted = (_quoted(msg) or "").strip()
            mid = quoted if valid_migration_id(quoted) else ""
        if not msg:
            return self._json(400, {"error": "message is required"})
        history = [{"role": h["role"], "content": str(h["content"])[:4000]} for h in (req.get("history") or [])[-8:]
                   if isinstance(h, dict) and h.get("role") in ("user", "assistant")]
        user = self.headers.get("X-Forwarded-Preferred-Username") or self.headers.get("X-Forwarded-User") or ""
        if not mid:
            if agents_mod.route(msg) not in GLOBAL_INTENTS:
                return self._json(400, {"error": NO_MIGRATION_ERROR})
            try:
                work = STATE.data_dir / "_global"
                work.mkdir(parents=True, exist_ok=True)
                copilot = STATE.copilot("", work, user)
                template = STATE.journey_template()
                res = agents_mod.Orchestrator(copilot, progress_text=lambda: progress_mod.as_text(template)).ask(
                    msg, history)
                res["migration_id"] = ""
                res["progress"] = template
            except Exception as exc:
                log.exception("chat failed")
                return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            return self._chat_reply(res)
        try:
            work = STATE.workspace(mid, refresh=bool(req.get("refresh")))
            copilot = STATE.copilot(mid, work, user)
            orch = agents_mod.Orchestrator(
                copilot, progress_text=lambda: progress_mod.as_text(STATE.progress(copilot.migration_id, copilot.work)))
            res = orch.ask(msg, history)
            mid = copilot.migration_id
            STATE.runs_cache.pop(mid, None)
            res["migration_id"] = mid
            res["progress"] = STATE.progress(mid, STATE.workspace(mid))
        except Exception as exc:
            log.exception("chat failed")
            return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
        return self._chat_reply(res)

    def _chat_reply(self, res: dict):
        keep = ("type", "tool", "args", "agent", "task", "summary", "via", "intent")
        res["events"] = [{k: v for k, v in e.items() if k in keep} for e in res["events"]]
        res["agent_name"] = agents_mod.AGENT_NAMES.get(res.get("agent") or "", "")
        return self._json(200, res)

    def _smoke_results(self, q: dict):
        from migration_ai import smoke

        mid = self._mid(q)
        if not mid:
            return self._json(400, {"error": "invalid migration_id"})
        data, err = self._body(2 * 1024 * 1024)
        if err:
            return self._json(413 if err == "too large" else 400, {"error": err})
        work = STATE.workspace(mid)
        try:
            path = smoke.save_results(work, data)
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        STATE.publish(mid, work / "smoke" / "results", "smoke/results")
        prog = STATE.progress(mid, work)
        return self._json(200, {"saved": path.name, "progress": prog})


def serve(port: int = 8080) -> None:
    global STATE
    STATE = State()
    log.info("AAP Odyssey on :%s (LLM: %s)", port, STATE.settings.llm.model or "none")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
