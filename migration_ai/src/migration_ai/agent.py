"""Migration agent: runs (or plans) the AAP 2.4 to 2.5 / 2.6 / 2.7 migration with the toolkit.

Modes
-----
plan     read-only. Analyses the pre-migration data and writes a migration plan
         with a runbook the human can execute ("ask the human" mode).
execute  runs the toolkit runbook against the target AAP. The LLM decides when to run
         each step and when to stop; the code enforces the
         order, the allowed commands and the abort threshold.
collect  no action: the human already migrated (e.g. from the CLI menu); the
         agent only checks that the state database has import results.

Guardrails (always enforced in code, whatever the model says)
-------------------------------------------------------------
* only the steps of :data:`RUNBOOK` can run, never arbitrary commands
* steps run in order, a step runs only after its prerequisites succeeded
* destructive toolkit commands (cleanup, state reset) are not in the runbook
* no retry: every object is migrated once, so the reports describe that single migration (a
  second import would add "skipped" results for objects that were migrated the first time)
* after an import step, if the failed share exceeds ``abort_failure_rate``
  the migration stops (status ``aborted``)
* with no model (or the model unreachable) a deterministic policy is used
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from migration_ai import toolkit
from migration_ai.config import Settings
from migration_ai.llm import LLM, Tool

log = logging.getLogger(__name__)
TOOLKIT_ERROR_RE = re.compile(r"IntegrityError|Database session rolled back|OperationalError|"
                              r"Traceback \(most recent call last\)")

PHASE3_TYPES = ["notification_templates", "job_templates", "workflow_job_templates", "schedules",
                "applications"]


@dataclass
class Step:
    id: str
    title: str
    commands: list[list[str]]
    requires: list[str] = field(default_factory=list)
    writes_target: bool = False
    optional: bool = False
    import_step: bool = False
    timeout: int = 6 * 3600
    note: str = ""


def build_runbook(include_credentials: bool = False, include_iam: bool = False,
                  dry_run: bool = False, refresh_export: bool = False, verify_ssl: bool = True,
                  include_settings: bool = False) -> list[Step]:
    """The toolkit sequence (same as migrate-complete.sh, plus reports)."""
    steps = [
        Step("prep", "Discover endpoints and API schemas of AAP 2.4 and the target AAP",
             [["prep", "--force"]], note="Needed by transform to map AAP 2.4 fields to the target AAP."),
    ]
    if refresh_export:
        steps.append(Step("export", "Export all data from AAP 2.4", [["export", "--yes", "--force"]]))
    steps += [
        Step("premigration_report", "Pre-migration report (rules RC-001..RC-011)",
             [["premigration-report", "--format", "all"]], requires=[]),
        Step("transform", "Transform the data for the target AAP",
             [["transform", "--yes", "--force", "--disable-progress"]], requires=["prep"]),
    ]
    if dry_run:
        steps.append(Step("import_check", "Check import dependencies (no writes)",
                          [["import", "--check-dependencies", "--yes"]], requires=["transform"]))
        steps.append(Step("import_dry_run", "Simulated import (dry run, no writes)",
                          [["import", "--dry-run", "--yes", "--phase", "phase1"]], requires=["transform"]))
        return steps
    steps.append(Step("import_phase1", "Import phase 1: organizations, users, teams, credential types, credentials, "
                      "execution environments, inventories, hosts, instance groups, projects",
                      [["import", "--yes", "--phase", "phase1"]], requires=["transform"], writes_target=True,
                      import_step=True))
    if include_credentials:
        steps.append(Step("credentials", "Credentials: compare and create the missing ones (secrets excluded)",
                          [["credentials", "migrate"]], requires=["import_phase1"], writes_target=True,
                          optional=True))
    steps += [
        Step("patch_projects", "Phase 2: add SCM details to projects",
             [["patch-projects"]], requires=["import_phase1"], writes_target=True, optional=True),
        Step("import_phase3", "Import phase 3: notifications, job templates, workflows, schedules, applications",
             [["import", "--yes", *sum((["--resource-type", t] for t in PHASE3_TYPES), [])]],
             requires=["import_phase1"], writes_target=True, import_step=True),
    ]
    if include_settings:
        steps.append(Step("settings", "Global settings (overwrites the target AAP configuration: review first)",
                          [["import", "--yes", "--resource-type", "settings"]], requires=["import_phase3"],
                          writes_target=True, optional=True))
    if include_iam:
        iam_common = ["--scan-strategy", "principal", "--workers", "10", *([] if verify_ssl else ["--no-verify-ssl"])]
        steps.append(Step("iam_audit", "Audit permissions on AAP 2.4 (read-only report)",
                          [["iam", "audit", *iam_common, "--output-dir", "./reports/local_audit/",
                            "--checkpoint-dir", "./reports/local_audit/"]],
                          requires=["import_phase3"], optional=True))
        steps.append(Step("iam", "Migrate permissions (RBAC role assignments), after all resources exist",
                          [["iam", "migrate", *iam_common, "--output-dir", "./reports/local_migrate/",
                            "--checkpoint-dir", "./reports/local_migrate/"]],
                          requires=["import_phase3"], writes_target=True, optional=True))
    steps += [
        Step("enhanced_report", "Post-migration enhanced report",
             [["enhanced-report", "--format", "html"], ["enhanced-report", "--format", "csv"]],
             requires=["import_phase3"]),
        Step("validate", "Post-migration validation against the target AAP",
             [["validate", "--live"]], requires=["import_phase3"], optional=True),
    ]
    return steps


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


class MigrationAgent:
    def __init__(self, settings: Settings, work_dir: Path, mode: str = "plan", *,
                 migration_id: str = "", include_credentials: bool = False, include_iam: bool = False,
                 include_settings: bool = False,
                 dry_run: bool = False, refresh_export: bool = False, max_retries: int = 1,
                 abort_failure_rate: float = 0.5, use_llm: bool = True, language: str = "en"):
        if mode not in ("plan", "execute", "collect"):
            raise ValueError(f"unknown mode {mode}")
        self.settings = settings
        self.work = work_dir
        self.mode = mode
        self.migration_id = migration_id
        self.dry_run = dry_run
        self.max_retries = max_retries
        self.abort_failure_rate = abort_failure_rate
        self.language = language
        env = toolkit.load_env_file(settings.toolkit_env_file)
        verify = all(env.get(k, "true").strip().lower() not in ("false", "0", "no")
                     for k in ("SOURCE__VERIFY_SSL", "TARGET__VERIFY_SSL"))
        self.runbook = build_runbook(include_credentials, include_iam, dry_run, refresh_export, verify_ssl=verify,
                                     include_settings=include_settings)
        self.state: dict[str, str] = {s.id: "pending" for s in self.runbook}
        self.retries = 0
        self.status = "running"
        self.stop_reason = ""
        self.out_dir = work_dir / "reports" / "agent"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.out_dir / "agent-log.jsonl"
        self.db = toolkit.StateDB(work_dir / toolkit.DB_RELATIVE)
        self.llm = LLM(settings.llm) if use_llm else None
        self.llm_ok = bool(self.llm and self.llm.available())
        self.step_results: list[dict] = []

    def event(self, kind: str, **data: Any) -> None:
        rec = {"ts": _now(), "type": kind, **data}
        with open(self.events_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
        log.info("%s %s", kind, {k: v for k, v in data.items() if k != "tail"})

    def _read_json(self, rel: str) -> dict | None:
        p = self.work / rel
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return None
        return None

    def premigration(self) -> dict | None:
        return self._read_json("reports/premigration-report.json")

    def dependencies(self) -> dict | None:
        return self._read_json("reports/dependencies.json")

    def readiness_summary(self) -> dict:
        rep = self.premigration()
        if not rep:
            return {"available": False}
        s = rep["summary"]
        cat = rep.get("rc_catalog", {})
        blocking = [f for f in rep.get("findings", []) if f["severity"] == "critical" and not f.get("cascade_from")]
        return {
            "available": True,
            "predicted_success_pct": s["predicted_success_pct"],
            "objects": s["total_resources"],
            "organizations": s["organizations"],
            "outcomes": s["outcomes"],
            "root_causes": [
                {"rc": rc, "title": cat.get(rc, {}).get("title"), "objects": n,
                 "fix": cat.get(rc, {}).get("remediation")}
                for rc, n in sorted(s.get("resources_by_rc", {}).items(), key=lambda kv: -kv[1])
            ],
            "blocking_examples": [
                {"rc": f["rc"], "type": f["resource_type"], "name": f["resource_name"], "org": f["organization"],
                 "problem": f["title"]} for f in blocking[:20]
            ],
        }

    def dependency_summary(self) -> dict:
        dep = self.dependencies()
        if not dep:
            return {"available": False}
        return {
            "available": True,
            "waves": [{"wave": p["phase"], "organizations": p["orgs"]} for p in dep.get("migration_phases", [])],
            "cycles": dep.get("cycles", []),
            "independent": len(dep.get("independent_orgs", [])),
            "dependent": len(dep.get("dependent_orgs", [])),
        }

    def ai_risk_summary(self) -> dict:
        """Predictions of the risk model trained on previous migrations, if one exists."""
        try:
            from migration_ai.predict import quick_summary

            return quick_summary(self.settings, self.work)
        except Exception as exc:
            return {"available": False, "reason": str(exc)}

    def db_summary(self) -> dict:
        totals = self.db.totals()
        total = sum(totals.values())
        return {
            "import_rows": total,
            "by_status": totals,
            "failed_share": round(totals.get("failed", 0) / total, 3) if total else 0.0,
            "top_errors": self.db.top_errors(8),
        }

    def step(self, step_id: str) -> Step | None:
        return next((s for s in self.runbook if s.id == step_id), None)

    def next_allowed(self) -> list[str]:
        if self.status != "running":
            return []
        out = []
        for s in self.runbook:
            st = self.state[s.id]
            if st != "pending":
                continue
            if all(self.state[r] in ("succeeded",) for r in s.requires):
                out.append(s.id)
        return out

    def runbook_view(self) -> list[dict]:
        allowed = set(self.next_allowed())
        return [{"id": s.id, "title": s.title, "status": self.state[s.id], "optional": s.optional,
                 "writes_aap26": s.writes_target, "can_run_now": s.id in allowed,
                 "commands": [" ".join(["aap-bridge", *c]) for c in s.commands]} for s in self.runbook]

    def run_step(self, step_id: str, reason: str = "") -> dict:
        s = self.step(step_id)
        if s is None:
            return {"ok": False, "error": f"unknown step '{step_id}'", "runbook": [x.id for x in self.runbook]}
        if self.mode != "execute":
            return {"ok": False, "error": f"mode '{self.mode}' does not run steps"}
        if step_id not in self.next_allowed():
            return {"ok": False, "error": f"step '{step_id}' cannot run now",
                    "allowed_now": self.next_allowed()}
        before = self.db.totals()
        self.event("step_start", step=step_id, reason=reason)
        rcs, tails, secs = [], [], 0.0
        for i, args in enumerate(s.commands):
            res = toolkit.run(self.settings, self.work, args, f"{len(self.step_results) + 1:02d}-{step_id}-{i}",
                              timeout=s.timeout)
            rcs.append(res.returncode)
            tails.append(res.output_tail[-1500:])
            secs += res.seconds
        after = self.db.totals()
        ok = all(rc == 0 for rc in rcs)
        toolkit_error = next((ln.strip() for ln in "\n".join(tails).splitlines()
                              if TOOLKIT_ERROR_RE.search(ln)), "")
        if toolkit_error:
            ok = False
        failed_share = 0.0
        if s.import_step:
            total = sum(after.values())
            failed_share = after.get("failed", 0) / total if total else 0.0
            if not ok and not toolkit_error and total > sum(before.values()):
                ok = True
        self.state[step_id] = "succeeded" if ok else "failed"
        result = {
            "ok": ok, "step": step_id, "returncodes": rcs, "seconds": round(secs, 1),
            "import_totals_before": before, "import_totals_after": after,
            "failed_share": round(failed_share, 3), "output_tail": "\n".join(tails)[-3000:],
        }
        if toolkit_error:
            result["toolkit_error"] = toolkit_error[:400]
        if not ok and s.optional:
            result["note"] = ("optional step failed: nothing was changed by it; skip it and continue with the "
                              "next steps (the imported objects are not affected)")
        self._last_tail = result["output_tail"]
        self.step_results.append({k: v for k, v in result.items() if k != "output_tail"})
        self.event("step_end", **{k: v for k, v in result.items() if k != "output_tail"},
                   tail=result["output_tail"][-800:])
        if s.import_step and failed_share > self.abort_failure_rate:
            self.status = "aborted"
            self.stop_reason = (f"failure share {failed_share:.0%} after '{step_id}' exceeds the "
                                f"abort threshold {self.abort_failure_rate:.0%}")
            self.event("guardrail_abort", reason=self.stop_reason)
            result["guardrail"] = self.stop_reason
        elif not ok and not s.optional:
            self.status = "aborted"
            self.stop_reason = f"required step '{step_id}' failed (exit codes {rcs})"
            self.event("guardrail_abort", reason=self.stop_reason)
            result["guardrail"] = self.stop_reason
        return result

    def stop_refused(self) -> str:
        """Why the LLM may NOT stop the migration now ("" = it may). Stopping is for systematic problems: a
        required step failed, too many objects failing, authentication or connectivity errors. A failed
        optional step (e.g. validation) is skipped, not a reason to leave the runbook unfinished."""
        required_failed = [x.id for x in self.runbook if self.state.get(x.id) == "failed" and not x.optional]
        if required_failed:
            return ""
        last = self.step_results[-1] if self.step_results else {}
        totals = self.db.totals()
        total = sum(totals.values())
        share = totals.get("failed", 0) / total if total else 0.0
        if share >= self.abort_failure_rate / 2:
            return ""
        if re.search(r"401|403|unauthori[sz]ed|forbidden|connection refused|could not connect|name or service|"
                     r"timed out|certificate verify", str(self.event_tail()), re.I):
            return ""
        failed_optional = [x.id for x in self.runbook if self.state.get(x.id) == "failed" and x.optional]
        return (f"not stopped: no required step failed and only {share:.1%} of the objects failed"
                + (f"; the failed step(s) {', '.join(failed_optional)} are optional" if failed_optional else "")
                + ". Skip what failed with skip_step and run the remaining steps (enhanced report, validation), "
                  "then finish and report the problem to the human." + (f" Last step: {last.get('step')}." if last
                                                                        else ""))

    def event_tail(self) -> str:
        """Output of the last step (for the connectivity / authentication check)."""
        return getattr(self, "_last_tail", "")

    def skip_step(self, step_id: str, reason: str = "") -> dict:
        s = self.step(step_id)
        if s is None or not s.optional:
            return {"ok": False, "error": "only optional steps can be skipped"}
        if self.state[step_id] != "pending":
            return {"ok": False, "error": f"step is {self.state[step_id]}"}
        self.state[step_id] = "skipped"
        self.event("step_skipped", step=step_id, reason=reason)
        return {"ok": True}

    def failures(self, resource_type: str = "", limit: int = 10) -> list[dict]:
        from aap_migration.cli.commands.migration_report_v2 import _HUMAN_READABLE_ERRORS

        out = []
        for e in self.db.top_errors(50):
            if resource_type and e["resource_type"] != resource_type:
                continue
            tmpl = _HUMAN_READABLE_ERRORS.get(e["error_key"], "")
            e["explanation"] = tmpl.split(".")[0] if tmpl else ""
            out.append(e)
        return out[:limit]

    def run(self) -> dict:
        self.event("agent_start", mode=self.mode, dry_run=self.dry_run, llm=self.llm_ok,
                   model=self.settings.llm.model if self.llm_ok else None)
        if self.mode == "plan":
            self._plan()
            self.status = "planned"
        elif self.mode == "collect":
            self._collect()
        else:
            if self.llm_ok:
                try:
                    self._execute_llm()
                except Exception as exc:
                    self.event("llm_error", error=str(exc))
                    log.exception("LLM agent failed, continuing deterministically")
            self._execute_deterministic()
            if self.status == "running":
                totals = self.db.totals()
                self.status = "completed_with_failures" if totals.get("failed") else "completed"
                if self.dry_run:
                    self.status = "dry_run_completed"
        summary = self._write_summary()
        self.event("agent_end", status=self.status, reason=self.stop_reason)
        return summary

    def _collect(self) -> None:
        totals = self.db.totals()
        if not self.db.exists or not totals:
            self.status = "no_migration_data"
            self.stop_reason = ("The state database has no import results. Run the migration "
                                "(CLI menu or mode=execute) and push the workspace first.")
        else:
            self.status = "completed_with_failures" if totals.get("failed") else "completed"

    def _execute_deterministic(self) -> None:
        """Run whatever is still pending, in order (also finishes after the LLM)."""
        for _ in range(len(self.runbook) + self.max_retries + 2):
            if self.status != "running":
                return
            allowed = self.next_allowed()
            if not allowed:
                return
            sid = allowed[0]
            reason = "deterministic policy" if not self.llm_ok else "completing runbook after the agent"
            self.run_step(sid, reason)

    def _execute_llm(self) -> None:
        system = (
            "You are the migration agent for Red Hat Ansible Automation Platform 2.4 to a newer AAP (2.5, 2.6, 2.7). "
            "You drive the 'aap-bridge' toolkit ONLY through the provided tools. Rules: "
            "1) call get_runbook first; 2) run the steps in order with run_step, one at a time; "
            "3) after each import step call get_failures and decide: continue, or stop_migration if something "
            "is systematically wrong (authentication, connectivity, most objects failing); there is no retry: "
            "each object is migrated once and failures are reported for the human to fix on AAP 2.4; "
            "4) you may skip only optional steps, explaining why; if an OPTIONAL step fails (e.g. "
            "a toolkit database error), skip it and continue: it is never a reason to stop, the imported objects "
            "are not affected; stop_migration is refused unless a required step failed, many objects fail or "
            "authentication/connectivity is broken; "
            "5) when nothing is left to run call finish with a short summary in "
            "English: what ran, what failed and why, "
            "what the human must fix. Never invent results: use only tool outputs."
        )
        context = {
            "migration_id": self.migration_id, "dry_run": self.dry_run,
            "readiness": self.readiness_summary(), "dependencies": self.dependency_summary(),
            "abort_failure_rate": self.abort_failure_rate, "max_retries": self.max_retries,
        }
        finished: dict[str, str] = {}

        def finish(summary: str) -> dict:
            finished["summary"] = summary
            return {"ok": True}

        def stop_migration(reason: str) -> dict:
            refused = self.stop_refused()
            if refused:
                self.event("stop_refused", reason=reason, why=refused)
                return {"ok": False, "error": refused}
            self.status = "aborted"
            self.stop_reason = f"stopped by the agent: {reason}"
            self.event("agent_stop", reason=reason)
            return {"ok": True, "status": self.status}

        tools = [
            Tool("get_runbook", "List the runbook steps, their status and which can run now.",
                 {"type": "object", "properties": {}}, lambda: self.runbook_view()),
            Tool("run_step", "Run one runbook step with the toolkit. Returns exit codes, import totals, "
                 "failure share and the output tail.",
                 {"type": "object", "properties": {
                     "step_id": {"type": "string"},
                     "reason": {"type": "string", "description": "why you run it now"}},
                  "required": ["step_id"]}, self.run_step),
            Tool("get_failures", "Most frequent import errors from the state database, with explanations.",
                 {"type": "object", "properties": {
                     "resource_type": {"type": "string"}, "limit": {"type": "integer"}}},
                 self.failures),
            Tool("get_status", "Import totals by status and the failed share.",
                 {"type": "object", "properties": {}}, lambda: self.db_summary()),
            Tool("skip_step", "Skip an optional step.",
                 {"type": "object", "properties": {"step_id": {"type": "string"}, "reason": {"type": "string"}},
                  "required": ["step_id", "reason"]}, self.skip_step),
            Tool("stop_migration", "Stop the migration now (no further steps).",
                 {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
                 stop_migration),
            Tool("finish", "Finish and give the final summary.",
                 {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]},
                 finish),
        ]

        def on_event(ev: dict) -> None:
            self.event("llm_" + ev.pop("type"), **ev)

        answer, _ = self.llm.run_tools(system, "Context:\n" + json.dumps(context, indent=1, default=str)
                                       + "\nStart the migration.", tools,
                                       max_turns=4 * len(self.runbook) + 6, on_event=on_event)
        self.llm_summary = finished.get("summary") or answer

    def _plan(self) -> None:
        readiness = self.readiness_summary()
        deps = self.dependency_summary()
        ai = self.ai_risk_summary()
        narrative = ""
        if self.llm_ok:
            system = (
                "You are a Red Hat Ansible Automation Platform migration architect. Using ONLY the data "
                "provided, write in English, in Markdown, without top-level headings: 1) a go/no-go "
                "recommendation with one-sentence rationale; 2) the fixes to do on AAP 2.4 before "
                "migrating, ordered by number of objects unblocked, citing RC codes; 3) the main risks during "
                "the import and how to recognise them; 4) the suggested migration order by waves. "
                "Max 350 words."
            )
            try:
                narrative = self.llm.complete(system, json.dumps(
                    {"readiness": readiness, "dependencies": deps, "ai_model": ai}, default=str), 1200)
            except Exception as exc:
                self.event("llm_error", error=str(exc))
        md = render_plan_markdown(self.migration_id, readiness, deps, ai, self.runbook, narrative,
                                  self.settings, self.dry_run)
        (self.out_dir / "migration-plan.md").write_text(md, encoding="utf-8")
        (self.out_dir / "migration-plan.json").write_text(json.dumps({
            "migration_id": self.migration_id, "generated": _now(), "readiness": readiness,
            "dependencies": deps, "ai_model": ai, "agent_narrative": narrative,
            "runbook": [{"id": s.id, "title": s.title, "commands": s.commands, "writes_aap26": s.writes_target,
                         "optional": s.optional} for s in self.runbook]}, indent=1, default=str), encoding="utf-8")
        self.event("plan_written", path=str(self.out_dir / "migration-plan.md"), llm=bool(narrative))

    def _write_summary(self) -> dict:
        summary = {
            "migration_id": self.migration_id, "mode": self.mode, "status": self.status,
            "stop_reason": self.stop_reason, "dry_run": self.dry_run, "finished": _now(),
            "llm_used": self.llm_ok, "model": self.settings.llm.model if self.llm_ok else None,
            "steps": self.runbook_view(), "step_results": self.step_results,
            "database": self.db_summary(),
            "agent_summary": getattr(self, "llm_summary", ""),
        }
        if self.mode == "collect" and self._has_run_summary():
            self.event("collect_kept_run_summary", status=self.status)
            return summary
        (self.out_dir / "agent-summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
        (self.out_dir / "agent-summary.md").write_text(render_summary_markdown(summary), encoding="utf-8")
        return summary

    def _has_run_summary(self) -> bool:
        try:
            prev = json.loads((self.out_dir / "agent-summary.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return prev.get("mode") == "execute" and not prev.get("dry_run")

    def rebuild_run_summary(self) -> dict | None:
        """Rebuild the summary of the last real migration run from agent-log.jsonl (e.g. after an older
        collect run replaced it). None when the log has no such run."""
        try:
            events = [json.loads(x) for x in self.events_path.read_text(encoding="utf-8").splitlines() if x.strip()]
        except (OSError, ValueError):
            return None
        starts = [i for i, e in enumerate(events) if e.get("type") == "agent_start" and e.get("mode") == "execute"
                  and not e.get("dry_run")]
        if not starts:
            return None
        run = events[starts[-1]:]
        end = next((e for e in run if e.get("type") == "agent_end"), {})
        ran = {e.get("step") for e in run if e.get("type") == "step_start"}
        self.runbook = build_runbook(include_credentials="credentials" in ran,
                                     include_iam=bool(ran & {"iam", "iam_audit"}), dry_run=False)
        self.state = {x.id: "pending" for x in self.runbook}
        self.step_results = []
        for e in run:
            if e.get("type") == "step_start" and e.get("step") in self.state:
                self.state[e["step"]] = "running"
            elif e.get("type") == "step_end" and e.get("step") in self.state:
                self.state[e["step"]] = "succeeded" if e.get("ok") else "failed"
                self.step_results.append({k: v for k, v in e.items() if k not in ("ts", "type", "tail")})
            elif e.get("type") == "step_skipped" and e.get("step") in self.state:
                self.state[e["step"]] = "skipped"
        self.mode, self.dry_run = "execute", False
        self.status = end.get("status") or "completed"
        self.stop_reason = end.get("reason") or ""
        self.llm_ok = bool(run[0].get("llm"))
        self.llm_summary = next((e.get("content") for e in reversed(run) if e.get("type") == "llm_answer"), "")
        summary = self._write_summary()
        summary["finished"] = end.get("ts") or summary["finished"]
        (self.out_dir / "agent-summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
        return summary


def render_plan_markdown(migration_id: str, readiness: dict, deps: dict, ai: dict, runbook: list[Step],
                         narrative: str, settings: Settings, dry_run: bool) -> str:
    env = toolkit.load_env_file(settings.toolkit_env_file)
    lines = [f"# Migration plan `{migration_id}`", "",
             f"Generated on {_now()} by the migration agent.",
             f"Source: {env.get('SOURCE__URL', 'n/a')} → Target: {env.get('TARGET__URL', 'n/a')}", ""]
    if readiness.get("available"):
        o = readiness["outcomes"]
        lines += ["## Readiness (pre-migration report)", "",
                  f"**Predicted first-attempt success: {readiness['predicted_success_pct']}%** of "
                  f"{readiness['objects']} objects in {readiness['organizations']} organizations.", "",
                  "| Outcome | Objects |", "|---|---:|",
                  f"| Will fail | {o.get('will_fail', 0)} |", f"| At risk | {o.get('at_risk', 0)} |",
                  f"| Need action | {o.get('needs_action', 0)} |",
                  f"| Stale | {o.get('stale', 0)} |", f"| Ready | {o.get('ready', 0)} |", "",
                  "### Main root causes", "", "| Code | Cause | Objects | Fix before migrating |", "|---|---|---:|---|"]
        for rc in readiness["root_causes"][:12]:
            lines.append(f"| {rc['rc']} | {rc['title']} | {rc['objects']} | {rc['fix'] or ''} |")
        if readiness["blocking_examples"]:
            lines += ["", "### Blocking objects (first 20)", "", "| Code | Type | Object | Organization | Problem |",
                      "|---|---|---|---|---|"]
            for b in readiness["blocking_examples"]:
                lines.append(f"| {b['rc']} | {b['type']} | {b['name']} | {b['org']} | {b['problem']} |")
    else:
        lines += ["## Readiness", "", "Pre-migration report not available."]
    if ai.get("available"):
        lines += ["", "## Risk model prediction (trained on previous migrations)", "",
                  f"Model `{ai.get('model_version')}`: {ai.get('predicted_fail')} objects with failure "
                  f"probability ≥ 50%, of which {ai.get('not_flagged_by_rules')} not flagged by the rules.", ""]
        for cls, n in ai.get("top_predicted_errors", []):
            lines.append(f"* {cls}: {n}")
    if deps.get("available"):
        lines += ["", "## Migration order (Dependency Analyzer)", ""]
        for w in deps["waves"]:
            lines.append(f"* Wave {w['wave']}: {', '.join(w['organizations'])}")
        if deps["cycles"]:
            lines.append(f"* Circular dependencies: {deps['cycles']}")
    if narrative:
        lines += ["", "## Agent analysis", "", narrative]
    lines += ["", "## Runbook", "",
              "To run the migration manually, open a terminal in the toolkit console "
              "(`oc rsh deploy/aap-migration-console bash`) and run:", "", "```bash",
              "set -a; . /etc/aap-bridge/.env; set +a",
              f"mkdir -p /work/{migration_id} && cd /work/{migration_id}",
              f"migration-ai --work-dir /work/{migration_id} ws pull --migration-id {migration_id}"]
    for s in runbook:
        lines.append(f"# {s.title}{' (writes to the target AAP)' if s.writes_target else ''}{' (optional)' if s.optional else ''}")
        for c in s.commands:
            lines.append("aap-bridge " + " ".join(c))
    lines += [f"migration-ai --work-dir /work/{migration_id} ws push --migration-id {migration_id}", "```", "",
              "Then start the *During migration* pipeline with `mode=collect` to collect the data and train the model.",
              "" if not dry_run else "\nNote: plan generated in dry-run mode."]
    return "\n".join(lines) + "\n"


def render_summary_markdown(summary: dict) -> str:
    db = summary["database"]
    if summary["mode"] == "collect":
        title = "Migration data collection"
    elif summary["dry_run"]:
        title = "Dry-run summary"
    else:
        title = "Migration run summary"
    lines = [f"# {title} — `{summary['migration_id']}`", "",
             f"* Mode: **{summary['mode']}**{' (dry-run)' if summary['dry_run'] else ''}",
             f"* Status: **{summary['status']}**",
             f"* LLM: {summary['model'] or 'none (deterministic policy)'}"]
    if summary["stop_reason"]:
        lines.append(f"* Reason: {summary['stop_reason']}")
    if summary["mode"] == "collect":
        lines += ["", "The migration was run by hand (toolkit console): this summary collects its import results."]
    else:
        lines += ["", "## Steps", "", "| Step | Status | Writes to the target AAP |", "|---|---|---|"]
        for s in summary["steps"]:
            lines.append(f"| {s['title']} | {s['status']} | {'yes' if s['writes_aap26'] else 'no'} |")
    lines += ["", "## Import results (state database)", "",
              f"Objects: {db['import_rows']} — " + ", ".join(f"{k}: {v}" for k, v in sorted(db['by_status'].items())), ""]
    if db["top_errors"]:
        lines += ["| Type | Error | Objects | Example |", "|---|---|---:|---|"]
        for e in db["top_errors"]:
            lines.append(f"| {e['resource_type']} | {e['error_key']} | {e['count']} | {e['example']} |")
    if summary.get("agent_summary"):
        lines += ["", "## Agent summary", "", summary["agent_summary"]]
    return "\n".join(lines) + "\n"


def wait_seconds(seconds: float) -> None:
    time.sleep(seconds)
