"""Where a migration is in the end-to-end journey (shown by AAP Odyssey).

The journey is a fixed list of stages, from the pre-migration analysis to the optional
Configuration as Code. The state of each stage is derived from what exists for
the migration (reports, state database, agent summary, model, CasC, patch plan,
smoke test results) and from its pipeline runs, so it is the same whoever started
them (dashboard, schedule or Copilot). Stages marked ``human`` need a person: they
are the human-in-the-loop points of the process. Every stage names the specialist
agent that owns it.

An optional stage that is still pending when a later stage is already done is shown
as "skipped" and never becomes the current stage.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from migration_ai import toolkit

TERMINAL = {"SUCCEEDED", "FAILED", "SKIPPED", "CANCELED"}


@dataclass
class Stage:
    id: str
    phase: str
    title: str
    description: str
    how: str
    agent: str = "migration"
    human: str = ""
    effect: str = "read-only"
    optional: bool = False
    status: str = "pending"
    detail: str = ""


def _stages(mid: str) -> list[Stage]:
    q = f'"{mid}"'
    P = "'" + (os.environ.get("PIPELINE_READINESS_NAME") or "AAP migration - during migration (plan, run, collect)") + "'"
    return [
        Stage("readiness", "Pre-migration", "Pre-migration analysis",
              "Export from AAP 2.4, pre-migration report, dependency analysis and migration plan.",
              f"Ask: Scan AAP 2.4 for migration readiness as {q} (or pipeline {P}, mode=plan)"),
        Stage("patches", "Pre-migration", "Target patch level",
              "The Smart Planning agent compares the installed target AAP (2.5, 2.6, 2.7...) with its published patch releases "
              "and summarizes what to apply before migrating.",
              "Ask: Show the patch releases of the installed AAP", agent="planning",
              human="Decide and schedule the patching", optional=True),
        Stage("smoke_pre", "Pre-migration", "Platform smoke test (before)",
              "Test suite with brand new objects proves that the empty target AAP works.",
              "Ask: Generate the smoke test suite; run it with smoke_phase=pre; upload the results",
              agent="testing", human="Customize and run the suite, upload the results", effect="test objects",
              optional=True),
        Stage("review", "Pre-migration", "Review the plan and fix AAP 2.4",
              "Read the pre-migration report and the plan, fix the blocking objects on AAP 2.4, scan again.",
              "Reports: pre-migration readiness report and migration plan; ask: What will fail and why?",
              human="Review the plan and decide the fixes"),
        Stage("dryrun", "Migration", "Dry run",
              "The agent runs the runbook up to the dependency check and a simulated import. No writes.",
              f"Dashboard: pipeline {P}, mode=execute, dry_run=true"),
        Stage("approve", "Migration", "Approve and start the migration",
              "Back up the target AAP, then start the migration: the agent runs the toolkit import itself "
              "(stops on guardrails), or a person runs it by hand in the toolkit console.",
              f"Agent: pipeline {P}, mode=execute, dry_run=false. By hand: toolkit console, "
              "then the same pipeline with mode=collect",
              human="Approve the migration and start it"),
        Stage("migration", "Migration", "Migration",
              "Import into the target AAP in the toolkit order: run by the agent, or by hand followed by mode=collect.",
              f"Dashboard: pipeline {P} (execute or collect); ask: What is the status of the migration?",
              effect="writes target AAP"),
        Stage("secrets", "Migration", "Restore credential secrets",
              "Secret values cannot be read from the AAP API: credentials arrive on the target AAP without them.",
              "Console: credential_decrypt/update_credentials.py",
              human="Re-enter the credential secrets", effect="writes target AAP"),
        Stage("verify", "Post-migration", "Post-migration verification",
              "Enhanced report and validation report; spot checks on the target AAP.",
              "Reports: post-migration enhanced report and validation report", agent="reporting",
              human="Review the reports and spot-check the target AAP"),
        Stage("smoke_post", "Post-migration", "Platform smoke test (after)",
              "The same suite after the migration: the platform still works; regression against the baseline.",
              "Ask: Generate the smoke test suite; run it with smoke_phase=post; upload the results",
              agent="testing", human="Run the suite and upload the results", effect="test objects"),
        Stage("train", "Training", "Train the risk model",
              "The risk model learns from the real outcome of every object.",
              "Automatic with train_after=true, or ask: Train the risk model", agent="training", effect="AI"),
        Stage("predict", "Prediction", "Predict the next migration",
              "Pre-migration agent assessment of the NEXT AAP 2.4 to migrate: failure probability of every "
              "object, from the risk model.",
              'Ask: Run an AI risk prediction scan as "<next migration>" (or pipeline predict)', agent="training",
              effect="AI"),
        Stage("casc", "Configuration as Code", "Configuration as Code",
              "Optionally generate the Configuration as Code of some or all target AAP objects.",
              "Dashboard: pipeline CasC, source=live, or ask: Generate the CasC; Reports: Download zip",
              agent="casc", human="Check and apply the code", optional=True),
    ]


@dataclass
class Facts:
    readiness: bool = False
    plan: bool = False
    imported: int = 0
    summary: dict = field(default_factory=dict)
    dryrun_ok: bool = False
    migration_run_ok: bool = False
    enhanced: bool = False
    validated: bool = False
    trained: bool = False
    predicted: bool = False
    casc: bool = False
    patch_plan: bool = False
    smoke: dict = field(default_factory=dict)
    active: dict | None = None


def _run_stage(params: dict) -> str:
    if "source" in params:
        return "casc"
    if "migrations" in params:
        return "train"
    mode = params.get("mode")
    if mode == "plan":
        return "readiness"
    if mode == "execute":
        return "dryrun" if params.get("dry_run") in (True, "true", "True") else "migration"
    if mode == "collect":
        return "migration"
    return "predict"


CONFIRM_FILE = Path("journey") / "confirmations.json"


def confirmations(work: Path) -> dict[str, dict]:
    """Stages a person marked as done in AAP Odyssey: {stage_id: {"done": bool, "by": user, "at": iso time}}."""
    p = work / CONFIRM_FILE
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except ValueError:
        return {}


def confirm(work: Path, stage_id: str, done: bool, user: str = "", note: str = "") -> dict:
    from datetime import datetime, timezone

    data = confirmations(work)
    data[stage_id] = {"done": done, "by": user or "a user", "at": datetime.now(tz=timezone.utc).isoformat(
        timespec="minutes"), **({"note": note} if note else {})}
    (work / CONFIRM_FILE).parent.mkdir(parents=True, exist_ok=True)
    (work / CONFIRM_FILE).write_text(json.dumps(data, indent=1), encoding="utf-8")
    return data[stage_id]


def human_stages(migration_id: str = "") -> list[tuple[str, str]]:
    """(id, title) of the stages a person can mark as done (the ones with a human in the loop)."""
    return [(s.id, s.title) for s in _stages(migration_id or "x") if s.human]


def _smoke(work: Path) -> dict:
    from migration_ai import smoke

    out: dict[str, dict] = {}
    for r in smoke.load_results(work):
        out[r["phase"]] = {"passed": smoke.passed(r), "finished": r.get("finished"),
                           "summary": r.get("summary")}
    return out


def gather(work: Path, migration_id: str, runs: list[dict] | None, latest_model: dict | None) -> Facts:
    f = Facts()
    f.readiness = any((work / "reports" / f"premigration-report.{ext}").is_file() for ext in ("json", "html"))
    f.plan = (work / "reports" / "agent" / "migration-plan.md").is_file()
    try:
        totals = toolkit.StateDB(work / toolkit.DB_RELATIVE).totals()
    except Exception:
        totals = {}
    f.imported = sum(v for k, v in totals.items() if k in ("completed", "failed"))
    p = work / "reports" / "agent" / "agent-summary.json"
    if p.is_file():
        try:
            f.summary = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            f.summary = {}
    s = f.summary
    if s.get("mode") == "execute" and s.get("dry_run") and s.get("status") == "completed":
        f.dryrun_ok = True
    if s.get("mode") in ("execute", "collect") and not s.get("dry_run") and s.get("status") == "completed":
        f.migration_run_ok = True
    for r in runs or []:
        params = (r.get("runtime_config") or {}).get("parameters") or {}
        stage = _run_stage(params)
        if r.get("state") == "SUCCEEDED":
            f.dryrun_ok |= stage == "dryrun"
            f.migration_run_ok |= stage == "migration"
        elif r.get("state") not in TERMINAL and f.active is None:
            f.active = {"stage": stage, "name": r.get("display_name"), "state": r.get("state"),
                        "created_at": r.get("created_at")}
    f.enhanced = any((work / "reports").glob("org-*-enhanced.html"))
    f.validated = any((work / "reports" / "validate").rglob("report.html")) if (work / "reports").is_dir() else False
    f.trained = bool(latest_model and migration_id in (latest_model.get("migrations") or []))
    f.predicted = (work / "ai" / "ai-readiness-report.json").is_file()
    f.casc = (work / "casc").is_dir() and any(d.is_dir() for d in (work / "casc").iterdir())
    f.patch_plan = (work / "planning").is_dir() and any((work / "planning").glob("patch-plan-*.md"))
    f.smoke = _smoke(work)
    return f


def compute(work: Path, migration_id: str, runs: list[dict] | None = None,
            latest_model: dict | None = None) -> dict[str, Any]:
    f = gather(work, migration_id, runs, latest_model)
    st = {s.id: s for s in _stages(migration_id)}
    migrated = f.imported > 0 or f.migration_run_ok
    done = {
        "readiness": f.readiness,
        "patches": f.patch_plan,
        "smoke_pre": bool(f.smoke.get("pre", {}).get("passed")),
        "review": f.dryrun_ok or migrated,
        "dryrun": f.dryrun_ok,
        "approve": migrated,
        "migration": migrated,
        "verify": f.enhanced and f.validated,
        "smoke_post": bool(f.smoke.get("post", {}).get("passed")),
        "train": f.trained,
        "predict": False,
        "casc": f.casc,
    }
    for sid, ok in done.items():
        if ok:
            st[sid].status = "done"
    marked = confirmations(work)
    for phase, sid in (("pre", "smoke_pre"), ("post", "smoke_post")):
        r = f.smoke.get(phase)
        if r:
            sm = r.get("summary") or {}
            st[sid].detail = (f"last run {str(r.get('finished') or '')[:16]}: {sm.get('passed', 0)} passed, "
                              f"{sm.get('failed', 0)} failed")
            if not r["passed"]:
                st[sid].status = "failed"
    if migrated and not f.dryrun_ok:
        st["dryrun"].status = "skipped"
    if migrated:
        st["secrets"].status = "reminder"
        if f.imported:
            st["migration"].detail = f"{f.imported} objects imported (completed or failed)"
    s = f.summary
    if s.get("status") == "aborted":
        sid = "dryrun" if s.get("dry_run") else "migration" if s.get("mode") in ("execute", "collect") else ""
        if sid and st[sid].status != "done":
            st[sid].status = "failed"
            st[sid].detail = s.get("stop_reason") or "the agent stopped"
        elif sid:
            st[sid].detail = ((st[sid].detail + "; ") if st[sid].detail else "") + \
                "the agent stopped before the end of the runbook: " + (s.get("stop_reason") or "see the summary")[:200]
    for sid, m in marked.items():
        if sid in st and m.get("done"):
            st[sid].status = "done"
            st[sid].detail = ((st[sid].detail + "; ") if st[sid].detail else "") + (
                f"{m['note']} by {m.get('by')} on {str(m.get('at'))[:16].replace('T', ' ')}" if m.get("note") else
                f"marked as done by {m.get('by')} on {str(m.get('at'))[:16].replace('T', ' ')}")
        elif sid in st and st[sid].status in ("done", "reminder"):
            st[sid].status = "pending"
            st[sid].detail = f"reopened by {m.get('by')}"
    if f.active:
        a = st[f.active["stage"]]
        a.status = "running"
        a.detail = f"run {f.active['name']} is {f.active['state']}"
    order = [x.id for x in st.values()]
    for i, sid in enumerate(order):
        if st[sid].optional and st[sid].status == "pending" and any(
                st[later].status in ("done", "reminder", "running") for later in order[i + 1:]):
            st[sid].status = "skipped"
    progressed = lambda i: any(st[later].status in ("done", "reminder", "running") for later in order[i + 1:])
    blocking_fail = {sid for i, sid in enumerate(order) if st[sid].status == "failed"
                     and not (st[sid].optional and progressed(i))}
    current = next((st[i] for i in order if st[i].status == "running"), None) or \
        next((st[i] for i in order if i in blocking_fail
              or (st[i].status == "pending" and not st[i].optional and i != "secrets")), None) or \
        next((st[i] for i in order if st[i].status == "pending" and i != "secrets"), None)
    optional_before = [st[i].title for i in order[:order.index(current.id)] if st[i].optional
                       and st[i].status == "pending"] if current else []
    if current and current.status == "pending":
        current.status = "current"
    stages = [asdict(x) for x in st.values()]
    pos = order.index(current.id) + 1 if current else len(order)
    if current is None:
        nxt = {"title": "Journey complete", "how": "Run a new pre-migration analysis for the next migration.",
               "human": "", "stage": None, "agent": None}
    else:
        nxt = {"title": current.title, "how": current.how, "human": current.human, "stage": current.id,
               "status": current.status, "detail": current.detail, "agent": current.agent,
               "optional": current.optional, "optional_before": optional_before}
    reminders = [x.title for x in st.values() if x.status == "reminder"]
    return {"migration_id": migration_id, "stages": stages, "position": pos, "total": len(order),
            "current": current.id if current else None,
            "current_phase": current.phase if current else "Complete",
            "next": nxt, "human_required": bool(current and current.human), "reminders": reminders}


def as_text(p: dict) -> str:
    """Markdown summary of where the migration is (used by AAP Odyssey answers)."""
    from migration_ai.agents import AGENT_NAMES

    n = p["next"]
    lines = [f"**Where we are:** stage {p['position']} of {p['total']} — {p['current_phase']}."]
    if n.get("stage"):
        state = {"running": "in progress", "failed": "failed", "current": "next"}.get(n.get("status", ""), "next")
        lines.append(f"**{state.capitalize()}:** {n['title']}" + (" (optional)" if n.get("optional") else "")
                     + (f" ({n['detail']})" if n.get("detail") else "") + f". How: {n['how']}.")
        if n.get("agent"):
            lines.append(f"**Agent:** {AGENT_NAMES.get(n['agent'], n['agent'])}.")
        lines.append("**Human in the loop:** " + (n["human"] + "." if n.get("human") else "no, it is automated."))
        if n.get("optional_before"):
            lines.append("**Optional before this step:** " + "; ".join(n["optional_before"]) + ".")
    else:
        lines.append(f"**{n['title']}.** {n['how']}")
    if p.get("reminders"):
        lines.append("**Reminder:** " + "; ".join(p["reminders"]) + " (human step).")
    return "\n".join(lines)
