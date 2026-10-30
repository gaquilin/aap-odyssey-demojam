"""Multi-agent AAP Odyssey: an orchestrator and six specialist agents.

AAP Odyssey page talks to the ORCHESTRATOR. It understands the request, hands it to
the specialist that owns it and returns the specialist's result together with the
trace of the interaction (shown in the UI as "Agent activity").

Each specialist has its own role, its own tools and its own permissions. The tool
subset is enforced in code, not in the prompt: the Reporting agent cannot start a
pipeline, the Smart Planning agent cannot read the migration database, and no agent
can write to AAP (migrations start from the dashboard; smoke tests are run by a person).

  planning   Smart Planning agent         docs.redhat.com patch releases vs the installed target AAP version
  migration  Migration agent              pre-migration analysis, plan, runs, failures (the toolkit agent)
  reporting  Reporting agent              reports of one or all migrations
  training   Training & Prediction agent  risk model: status, training run, AI readiness report
  casc       Configuration as Code agent  infra.aap_configuration code of some or all target AAP objects
  testing    Smoke & regression test agent platform test suite (brand new objects) and its results

Routing: a deterministic intent router handles the known requests (fast, works without
an LLM). With an LLM, the specialist answers with its own tool loop; requests the
router does not recognise go to the LLM orchestrator, which delegates through one tool
per specialist. Without an LLM every specialist answers deterministically.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

from migration_ai.llm import Tool

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Agent:
    id: str
    name: str
    role: str
    tools: tuple[str, ...]
    writes: str
    phase: str
    prompt: str


BASE_RULES = """Rules: use your tools; never invent objects, numbers, versions, dates or error messages.
Answer in English, concise, with Markdown. Say clearly when a human must act."""

AGENTS: dict[str, Agent] = {a.id: a for a in [
    Agent("planning", "Smart Planning agent",
          "Detects the version of the installed target AAP (2.5, 2.6, 2.7...), reads its patch releases in the "
          "Red Hat documentation, compares them with the installed release and summarizes what to patch and when.",
          ("patch_releases", "installed_version"),
          "nothing (reads docs.redhat.com and the anonymous ping of the target AAP)", "Pre-migration",
          "You are the Smart Planning agent. For patch questions call patch_releases with the AAP version the user "
          "names; without a version omit it: the tool uses the version of the installed target AAP. Facts come only from the tool. Cite AAP-xxxxx and CVE ids exactly. CVE severity is "
          "not in the release notes: say it must be checked on the Red Hat CVE pages. Patching is decided and "
          "scheduled by a human."),
    Agent("migration", "Migration agent",
          "Owns the migration lifecycle: pre-migration analysis, plan, dry run and migration runs, failures and "
          "their causes.",
          ("start_readiness_scan", "scan_status", "migration_status", "overview", "readiness", "migration_plan",
           "search", "explain", "journey_status"),
          "the target AAP, only through the pipeline 'AAP migration - during migration (plan, run, collect)', started by a human from the dashboard", "Migration",
          "You are the Migration agent. You can start only the READ-ONLY pre-migration analysis; migrations are "
          "started by a human from the OpenShift AI dashboard (pipeline 'AAP migration - during migration (plan, run, collect)', mode=execute). Explain failures "
          "with explain/readiness and give the fix."),
    Agent("reporting", "Reporting agent",
          "Finds and presents the reports of one migration or of all migrations, grouped by phase, with their "
          "source (toolkit or AI agent).",
          ("list_reports", "migrations_overview", "overview"),
          "nothing", "Every phase",
          "You are the Reporting agent. Use list_reports for one migration and migrations_overview for all of them. "
          "Group by phase and say for each report whether the toolkit or an AI agent generated it."),
    Agent("training", "Training & Prediction agent",
          "Trains the risk model on the finished migrations and predicts which objects of the next AAP 2.4 will "
          "fail.",
          ("model_status", "start_training", "start_prediction", "predict", "scan_status"),
          "the risk model in the bucket (training pipeline)", "Training / Prediction",
          "You are the Training & Prediction agent. Report model metrics as given (recall on failures, comparison "
          "with the rules). Training and prediction run as pipelines: start them only when asked."),
    Agent("casc", "Configuration as Code agent",
          "Generates and validates the infra.aap_configuration code of some or all target AAP objects, per "
          "organization or for the whole migration.",
          ("generate_casc", "casc_schema", "validate_casc"),
          "files only (zip in the bucket); a human checks and applies them", "Configuration as Code",
          "You are the Configuration as Code agent. For existing objects call generate_casc. Objects without an "
          "organization (credential types, execution environments, instance groups, credentials without "
          "organization) go to the _global scope, applied first; with an organization filter only the shared "
          "objects that organization uses are kept. For new objects use casc_schema and always validate_casc."),
    Agent("testing", "Smoke & regression test agent",
          "Generates the platform smoke test suite of the target AAP (brand new objects, never migrated job "
          "templates), "
          "reviews its customization points and compares uploaded results with the baseline.",
          ("generate_smoke_suite", "smoke_results"),
          "files only; the suite is customized and run by a human", "Pre / post-migration",
          "You are the Smoke & regression test agent. The suite creates brand new objects in a dedicated "
          "organization and deletes them; it never launches migrated job templates. A human customizes and runs "
          "it, then uploads reports/smoke-results.json. List the customization points and any regression."),
]}
AGENT_NAMES = {a.id: a.name for a in AGENTS.values()} | {"copilot": "AAP Odyssey (orchestrator)"}

INTRO = ("**I am AAP Odyssey**, the AI assistant for the migration from Ansible Automation Platform 2.4 to 2.5, 2.6 or 2.7 "
         "on Red Hat OpenShift AI.\n\n"
         "Six specialist agents follow your migration through 13 stages: they predict what will fail, explain the "
         "results and verify the platform. A person decides and starts every change.\n\n"
         "Ask *What's next?* to begin.")

NEXT_RE = re.compile(r"\b(what'?s next|what is next|next step|where are we|journey|which stage|what now)\b", re.I)

ROUTES: list[tuple[str, str, re.Pattern]] = [
    ("testing", "smoke_suite", re.compile(r"\b(generate|create|build|make|prepare|write|regenerate|new)\b.*"
                                          r"\b(smoke|regression|test suite)\b", re.I)),
    ("testing", "smoke_results", re.compile(r"\b(smoke|regressions?)\b.*\b(results?|baseline|compare|uploaded|pass(ed)?|"
                                            r"fail(ed|s)?|green|ok)\b|\b(results?)\b.*\b(smoke|regression)\b|"
                                            r"\bany regressions?\b|\bregressions?\b.*\b(found|any|after|since)\b", re.I)),
    ("testing", "smoke_info", re.compile(r"\b(smoke|regressions?)\b|\btest suite\b", re.I)),
    ("planning", "installed", re.compile(r"\binstalled (aap )?version\b|\bwhich (aap )?version\b.*\binstalled\b|"
                                         r"\bversion of (the )?(installed|target) aap\b", re.I)),
    ("planning", "patches", re.compile(r"\b(patch|patches|patching|errata|async updates?|release notes|"
                                       r"patch releases?|hotfix(es)?|up to date|cves?|security (fixes|updates))\b|"
                                       r"\b(newer|latest|last|recent) (aap )?(releases?|versions?)\b|\bnewer than\b", re.I)),
    ("reporting", "all_reports", re.compile(r"\b(all|every)\b.*\bmigrations\b|\bmigrations overview\b|"
                                            r"\blist (the )?migrations\b", re.I)),
    ("reporting", "reports", re.compile(r"\breports?\b", re.I)),
    ("casc", "casc", re.compile(r"\bcasc\b|configuration as code", re.I)),
    ("training", "train", re.compile(r"\b(train|retrain)\b", re.I)),
    ("training", "model", re.compile(r"\b(model card|risk model|model status|which model)\b", re.I)),
    ("training", "predict_scan", re.compile(r"\b(scan|run|start)\b.*\b(predict|prediction|ai readiness)\b", re.I)),
    ("training", "risk", re.compile(r"\b(risk|risky|predict|prediction|likely to fail|highest)\b", re.I)),
    ("migration", "scan", re.compile(r"\bscan\b", re.I)),
    ("migration", "status", re.compile(r"\b(status|progress|running)\b", re.I)),
    ("migration", "plan", re.compile(r"\b(plan|runbook)\b", re.I)),
    ("migration", "readiness", re.compile(r"\b(will fail|blocking|root causes?|readiness|fix first|stale|why)\b",
                                          re.I)),
    ("migration", "summary", re.compile(r"\b(summari[sz]e|summary|result|overview)\b", re.I)),
]


START_RE = re.compile(r"\b(start|run|launch|execute|begin|kick off|trigger|do)\b.*\b(dry[ -]?run|migration|import)\b|"
                      r"^\s*(dry[ -]?run)\b", re.I)
ANALYSIS_RE = re.compile(r"pre-?migration|analysis|readiness|\bscan\b|predict|risk|smoke|casc|train", re.I)


MARK_RE = re.compile(r"\b(mark|set|flag)\b.*\b(as )?(done|completed?|not done|undone|open|pending)\b|"
                     r"\b(i|we) (have )?(reviewed|restored|re-entered|verified|checked|fixed|approved|applied|decided)\b|"
                     r"\breopen\b", re.I)
STAGE_WORDS = [("smoke_pre", r"smoke.*\b(before|pre)\b"), ("smoke_post", r"smoke.*\b(after|post)\b"),
               ("secrets", r"secret|credential"), ("verify", r"verif|validation|spot.?check|post-?migration"),
               ("review", r"review|plan|fix"), ("approve", r"approv"), ("patches", r"patch"),
               ("casc", r"casc|configuration as code")]


def stage_in(message: str) -> str:
    for sid, pat in STAGE_WORDS:
        if re.search(pat, message, re.I):
            return sid
    return ""


INTRO_RE = re.compile(r"\bwho are you\b|\bwhat are you\b|\bintroduce yourself\b|\bwhat can you do\b|"
                      r"\bwhat is (aap )?odyssey\b|^\W*(hey|hi|hello|ehi|ciao|ok)?\W*odyssey\W*$", re.I)


def route(message: str) -> tuple[str, str] | None:
    if MARK_RE.search(message) and stage_in(message):
        return "copilot", "mark"
    if INTRO_RE.search(message):
        return "copilot", "intro"
    if START_RE.search(message) and not ANALYSIS_RE.search(message):
        return "migration", "start_request"
    if NEXT_RE.search(message) and not re.search(r"\bscan\b", message, re.I):
        return "copilot", "journey"
    for agent, intent, pat in ROUTES:
        if pat.search(message):
            return agent, intent
    return None


def _plain(text: str) -> str:
    """First line of an answer without Markdown (for the activity trace)."""
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return re.sub(r"[*_`#|]+", "", text).strip()


def _version(message: str) -> str:
    m = re.search(r"\b2\.(5|6|7)\b", message)
    return f"2.{m.group(1)}" if m else ""


class Orchestrator:
    """Routes a chat message to a specialist agent and records the interaction."""

    def __init__(self, copilot, progress_text: Callable[[], str] | None = None):
        self.cp = copilot
        self.progress_text = progress_text
        self.events: list[dict] = []

    def agent_tools(self, agent_id: str) -> list[Tool]:
        allowed = set(AGENTS[agent_id].tools)
        tools = [t for t in self.cp.tools() if t.name in allowed]
        return [self._traced(agent_id, t) for t in tools]

    def _traced(self, agent_id: str, t: Tool) -> Tool:
        def fn(**kw):
            from migration_ai.assistant import _quoted

            msg = getattr(self, "_message", "")
            mid = kw.get("migration_id")
            if mid and mid != self.cp.migration_id and mid != (_quoted(msg) or ""):
                kw["migration_id"] = self.cp.migration_id
            self.events.append({"type": "tool_call", "agent": agent_id, "tool": t.name, "args": kw})
            if t.name.startswith("start_") and not re.search(
                    r"\b(scan|analys[ie]s|readiness|train|retrain|predict|prediction)\b", msg, re.I):
                return {"error": "not started: the user did not ask for this pipeline. Dry run and migration are "
                                 "started by a person from the OpenShift AI dashboard."}
            return t.fn(**kw)
        return Tool(t.name, t.description, t.parameters, fn)

    def ask(self, message: str, history: list[dict] | None = None) -> dict:
        self._message = message
        self.events = []
        self.cp.files = []
        self.cp.view = None
        r = route(message)
        llm = self.cp.llm if (self.cp.llm and self.cp.llm.configured) else None
        if r == ("copilot", "mark"):
            return self._result(self._mark(message), "copilot", llm_used=False)
        if r == ("copilot", "intro"):
            self.events.append({"type": "handoff", "agent": "copilot", "task": "introduction", "via": "router"})
            return self._result(INTRO, "copilot", llm_used=False)
        if r and r[0] == "copilot":
            self.events.append({"type": "handoff", "agent": "copilot", "task": "journey status", "via": "router"})
            if self.progress_text:
                answer = self.progress_text()
            else:
                from migration_ai.progress import as_text

                answer = as_text(self.cp.journey_status())
            return self._result(answer, "copilot", llm_used=False)
        if r:
            agent_id, intent = r
            self.events.append({"type": "handoff", "agent": agent_id, "task": message[:200], "via": "router",
                                "intent": intent})
            answer, used = self._run_specialist(agent_id, intent, message, history, llm)
            return self._result(answer, agent_id, llm_used=used)
        if llm:
            try:
                return self._orchestrate_llm(message, history, llm)
            except Exception as exc:
                log.warning("LLM orchestrator unavailable: %s", exc)
                self.events.append({"type": "llm_error", "error": str(exc)[:200]})
        self.events.append({"type": "handoff", "agent": "migration", "task": message[:200], "via": "router",
                            "intent": "summary"})
        answer, used = self._run_specialist("migration", "summary", message, history, None)
        return self._result(answer, "migration", llm_used=used)

    def _result(self, answer: str, agent_id: str, llm_used: bool) -> dict:
        first = _plain(next((ln for ln in (answer or "").splitlines() if ln.strip()), ""))[:160]
        self.events.append({"type": "agent_result", "agent": agent_id, "summary": first})
        return {"answer": answer, "events": self.events, "files": self.cp.files, "llm": llm_used,
                "agent": agent_id, "view": self.cp.view}

    def _run_specialist(self, agent_id: str, intent: str, message: str, history, llm) -> tuple[str, bool]:
        deterministic_first = intent in ("start_request", "status", "reports", "all_reports", "patches", "installed",
                                         "smoke_suite",
                                         "smoke_results", "smoke_info", "casc", "train", "predict_scan", "scan", "model")
        if llm and not deterministic_first:
            a = AGENTS[agent_id]
            try:
                def on_event(e):
                    if e.get("type") == "tool_call":
                        return
                answer, _ = llm.run_tools(f"{a.prompt}\n{BASE_RULES}", message, self.agent_tools(agent_id),
                                          max_turns=8, on_event=on_event, history=history)
                return answer, True
            except Exception as exc:
                log.warning("%s without LLM: %s", a.name, exc)
                self.events.append({"type": "llm_error", "agent": agent_id, "error": str(exc)[:200]})
        self._llm_used = False
        answer = self._deterministic(agent_id, intent, message)
        used = self._llm_used if intent == "patches" else bool(llm) and intent == "smoke_suite"
        return answer, used

    def _smoke_info(self, message: str) -> str:
        """Any other smoke / regression question: what exists and how a person runs it. No new suite."""
        res = self._call("testing", "smoke_results")
        suites = self.cp.smoke_suites()
        lines = []
        if suites:
            lines.append(f"**Smoke test suite**: generated {suites[0]['stamp']} ({suites[0]['tests']} tests); "
                         "download it from *Reports* → *Smoke & regression test suites*.")
        else:
            lines.append("**No smoke test suite yet.** Ask *Generate the smoke test suite* to create one.")
        if res.get("runs"):
            cur = res["latest"]
            sm = cur.get("summary") or {}
            lines.append(f"**Latest results ({cur['phase']})**: {sm.get('passed')} passed, {sm.get('failed')} failed"
                         + ("; regressions against the baseline: " + ", ".join(x["name"] for x in res["regressions"])
                            if res.get("regressions") else
                            "; no regression against the baseline." if res.get("baseline") else "."))
        else:
            lines.append("No results uploaded yet.")
        lines.append("A person runs the suite (AAP Odyssey never writes to AAP): set the customization points, run "
                     "`ansible-playbook playbooks/smoke.yml -e smoke_phase=pre|post|patch`, then upload "
                     "`reports/smoke-results.json` with *Upload results*.")
        return "\n\n".join(lines)

    def _call(self, agent_id: str, tool: str, **kw) -> Any:
        self.events.append({"type": "tool_call", "agent": agent_id, "tool": tool, "args": kw})
        return getattr(self.cp, tool)(**kw)

    def _deterministic(self, agent_id: str, intent: str, message: str) -> str:
        from migration_ai.assistant import _quoted

        cp = self.cp
        if agent_id == "planning":
            if intent == "installed":
                res = self._call("planning", "installed_version")
                if res.get("error"):
                    return f"Cannot read the version of the target AAP: {res['error']}"
                aap = f"AAP **{res['aap_version']}**" if res.get("aap_version") else "Target AAP (version not mapped)"
                return (f"{aap} at `{res['url']}`: automation controller **{res['controller_version']}**. "
                        "Ask *show the patch releases of the installed AAP* to see what is newer.")
            version = _version(message)
            try:
                res = self._call("planning", "patch_releases", version=version, compare_installed=True,
                                 question=message)
            except Exception as exc:
                return (f"The Smart Planning agent could not read the patch releases of "
                        f"{'AAP ' + version if version else 'the installed AAP'} from "
                        f"docs.redhat.com: {type(exc).__name__}: {exc}"
                        + ("" if isinstance(exc, (PermissionError, FileNotFoundError)) else
                           ". AAP Odyssey pod needs HTTPS egress to docs.redhat.com (guide, step 10).")
                        + ("\n\nCopy the pages from a workstation: `bash cluster/10-copilot/06-copy-release-notes.sh` "
                           "(guide, step 10.6)." if isinstance(exc, FileNotFoundError) else
                           "\n\nCheck from the pod: `bash cluster/10-copilot/05-check-egress.sh`."))
            parts = [res.get("direct_answer") or ""]
            if res.get("llm_summary"):
                parts.append(res["llm_summary"])
            elif res.get("llm_rejected"):
                parts.append("*The note of the language model was not shown: it cited facts that are not in "
                             "the release notes (" + "; ".join(res["llm_rejected"][:4]) + ").*")
            self._llm_used = bool(res.get("llm_summary"))
            head = "\n\n".join(x for x in parts if x)
            return (head + "\n\n---\n\n" if head else "") + res["plan_markdown"]
        if agent_id == "reporting":
            if intent == "all_reports":
                res = self._call("reporting", "migrations_overview")
                n = len(res.get("migrations") or [])
                return f"**{n} migrations** in the bucket, newest first. Pick one to open its reports."
            mid = _quoted(message) or cp.migration_id
            res = self._call("reporting", "list_reports", migration_id=mid)
            if res.get("error"):
                return res["error"]
            n = len(res["reports"])
            if not n and not res["casc"] and not res["smoke"]:
                return f"No reports yet for **{mid}**: run the pre-migration analysis or the pipeline first."
            by_phase: dict[str, int] = {}
            for r in res["reports"]:
                by_phase[r["phase"]] = by_phase.get(r["phase"], 0) + 1
            parts = ", ".join(f"{k}: {v}" for k, v in by_phase.items())
            return (f"**Reports of {mid}**, grouped by phase ({parts or 'none'}"
                    + (f"; {len(res['casc'])} CasC" if res["casc"] else "")
                    + (f"; {len(res['smoke'])} test suites" if res["smoke"] else "") + "). Open them in a new tab.")
        if agent_id == "testing":
            if intent == "smoke_results":
                res = self._call("testing", "smoke_results")
                if not res.get("runs"):
                    return res.get("note", "No results yet.")
                cur = res["latest"]
                sm = cur.get("summary") or {}
                lines = [f"**Latest smoke test ({cur['phase']})**: {sm.get('passed')} passed, {sm.get('failed')} failed, "
                         f"{sm.get('skipped')} skipped."]
                if res.get("regressions"):
                    lines += ["", "**Changes against the baseline:**", ""]
                    lines += [f"* {x['name']}: {x['before']} → **{x['now']}** ({x.get('details') or ''})"
                              for x in res["regressions"]]
                elif res.get("baseline"):
                    lines.append("No regression against the baseline.")
                return "\n".join(lines)
            if intent == "smoke_info":
                return self._smoke_info(message)
            force = bool(re.search(r"\b(regenerate|new|again|another|fresh)\b", message, re.I))
            res = self._call("testing", "generate_smoke_suite", force=force)
            tests = ", ".join(t["name"] for t in res["tests"])
            if res.get("reused"):
                return (f"**A smoke test suite already exists** (generated {res['stamp']}): download it below. "
                        f"Tests: {tests}.\n\nAsk *Regenerate the smoke test suite* only if the target AAP or the "
                        "migration changed and you need a new one.")
            return ("**Platform smoke test suite generated** (brand new objects only; migrated job templates are "
                    f"never launched). Tests: {tests}.\n\n"
                    "**Human in the loop:** download the zip, set the customization points below, run it with "
                    "`ansible-playbook playbooks/smoke.yml -e smoke_phase=pre|post|patch`, then upload "
                    "`reports/smoke-results.json` here."
                    + (f"\n\nObject definitions validated: {len(res['validation']['errors'])} errors."
                       if res.get("validation") else ""))
        if agent_id == "training":
            if intent == "train":
                res = self._call("training", "start_training")
                return self._started(res, "training of the risk model on every migration")
            if intent == "predict_scan":
                mid = _quoted(message) or cp.migration_id
                refresh = not re.search(r"\b(existing|without export|no export)\b", message, re.I)
                res = self._call("training", "start_prediction", migration_id=mid, refresh_export=refresh)
                return self._started(res, f"AI readiness report of `{mid}`")
            if intent == "model":
                res = self._call("training", "model_status")
                m = res.get("latest_model")
                if not m:
                    return res.get("note", "No trained model yet.")
                metrics = m.get("metrics") or {}
                return (f"**Risk model {m.get('version')}** trained on {len(m.get('migrations') or [])} migration(s)"
                        + (f"; recall on failures {metrics.get('recall_failed')}" if metrics.get("recall_failed")
                           is not None else "") + ". "
                        + ("This migration is part of its training data." if res["this_migration_in_training_data"]
                           else "This migration is not in its training data yet.")
                        + " Open the model card from **Reports** (Training phase).")
            self.events.append({"type": "tool_call", "agent": "training", "tool": "predict", "args": {}})
            return cp._fallback_risk(message, [])
        if agent_id == "casc":
            self.events.append({"type": "tool_call", "agent": "casc", "tool": "generate_casc",
                                "args": {"organization": _quoted(message) or ""}})
            return cp._fallback(message, [])
        if intent == "scan":
            self.events.append({"type": "tool_call", "agent": "migration", "tool": "start_readiness_scan", "args": {}})
            return cp._fallback_scan(message, message.lower(), [])
        if intent == "start_request":
            import os

            pipeline = os.environ.get("PIPELINE_READINESS_NAME") or "AAP migration - during migration (plan, run, collect)"
            dry = bool(re.search(r"dry[ -]?run", message, re.I))
            return ("AAP Odyssey **does not start** the " + ("dry run" if dry else "migration") + ": a person starts "
                    "it from the OpenShift AI dashboard, after reviewing the plan (human in the loop). AAP Odyssey "
                    "starts only the read-only pre-migration analysis, the training and the prediction.\n\n"
                    f"Dashboard → project → *Pipelines* → **{pipeline}** → *Create run*:\n\n"
                    f"* `migration_id`: `{cp.migration_id or 'test-migration-01'}`\n* `mode`: `execute`\n"
                    f"* `dry_run`: `{'true' if dry else 'false'}`" + ("" if dry else
                    "\n\nBack up the target AAP first. Then ask *What is the status of the migration?*"))
        if intent == "status":
            self.events.append({"type": "tool_call", "agent": "migration", "tool": "migration_status", "args": {}})
            return cp._fallback_status(message, [])
        if intent == "plan":
            self.events.append({"type": "tool_call", "agent": "migration", "tool": "migration_plan", "args": {}})
            return cp._fallback_plan([])
        if intent == "readiness" and not (_quoted(message) and re.search(r"\bwhy\b", message, re.I)):
            self.events.append({"type": "tool_call", "agent": "migration", "tool": "readiness", "args": {}})
            return cp._fallback_readiness(message, [], detailed=True)
        evs: list[dict] = []
        answer = cp._fallback(message, evs)
        for e in evs:
            self.events.append({**e, "agent": "migration"})
        return answer

    def _mark(self, message: str) -> str:
        """A person confirms (or reopens) a stage with a human in the loop."""
        from migration_ai import progress

        cp = self.cp
        sid = stage_in(message)
        titles = dict(progress.human_stages(cp.migration_id))
        self.events.append({"type": "handoff", "agent": "copilot", "task": "mark stage", "via": "router"})
        if not cp.migration_id:
            return "Select a migration first: the confirmation is saved with the migration."
        if sid not in titles:
            return ("Only the stages with a human in the loop can be marked: "
                    + ", ".join(f"*{t}*" for t in titles.values()) + ".")
        done = not re.search(r"\b(not done|undone|reopen|open|pending)\b", message, re.I)
        m = progress.confirm(cp.work, sid, done, getattr(cp, "user", "") or "")
        self.events.append({"type": "tool_call", "agent": "copilot", "tool": "mark_stage",
                            "args": {"stage": sid, "done": done}})
        if cp.publish_fn:
            cp.publish_fn(cp.migration_id, cp.work / "journey", "journey")
        nxt = ""
        if self.progress_text:
            nxt = "\n\n" + self.progress_text()
        return (f"{'Marked' if done else 'Reopened'} **{titles[sid]}** "
                f"{'as done' if done else ''} (by {m['by']}, {m['at'][:16].replace('T', ' ')} UTC)." + nxt)

    def _started(self, res: dict, what: str) -> str:
        if res.get("error"):
            return res["error"]
        r = res["run"]
        if res.get("status") == "already_running":
            return f"Already running: `{r['name']}` ({r['state']}). Ask *scan status*."
        return (f"Started the {what}.\n\n* Run: `{r['name']}` — {r['state'] or 'PENDING'}\n* Nothing is written to "
                "AAP.\n\nAsk *scan status* to follow it; the run is also visible in the OpenShift AI dashboard.")

    def _orchestrate_llm(self, message: str, history, llm) -> dict:
        used: list[str] = []

        def delegate(agent_id: str):
            def fn(task: str) -> str:
                used.append(agent_id)
                self.events.append({"type": "handoff", "agent": agent_id, "task": task[:200], "via": "llm"})
                r = route(task)
                intent = r[1] if r and r[0] == agent_id else "free"
                answer, _ = self._run_specialist(agent_id, intent, task, None, llm)
                self.events.append({"type": "agent_result", "agent": agent_id,
                                    "summary": _plain(next((ln for ln in answer.splitlines() if ln.strip()), ""))[:160]})
                return answer
            return fn

        tools = [Tool(f"ask_{a.id}", f"{a.name}: {a.role}", {"type": "object", "properties": {
            "task": {"type": "string", "description": "the request for this agent, self-contained"}},
            "required": ["task"]}, delegate(a.id)) for a in AGENTS.values()]
        system = ("You are the AAP Odyssey orchestrator for an AAP migration. Delegate every "
                  "request to the right specialist with its ask_<agent> tool (one or more), then give the final "
                  "answer in English Markdown built ONLY from what the agents returned. Say which agent did what "
                  "and whether a human must act. Agents:\n"
                  + "\n".join(f"- {a.id}: {a.name} — {a.role}" for a in AGENTS.values()))
        answer, _ = llm.run_tools(system, message, tools, max_turns=6, history=history)
        agent_id = used[-1] if used else "copilot"
        return self._result(answer, agent_id, llm_used=True)


TOOL_LABELS = {
    "patch_releases": "Read the patch releases on docs.redhat.com and compare them with the installed AAP",
    "installed_version": "Read the version of the installed target AAP (anonymous ping)",
    "start_readiness_scan": "Start the read-only pre-migration analysis pipeline",
    "scan_status": "Follow the pipeline runs started by AAP Odyssey",
    "migration_status": "Show the progress of the migration (phases, counts, errors)",
    "overview": "Summarize a migration: objects, results, reports",
    "readiness": "Show what will fail in the migration, with causes and fixes",
    "migration_plan": "Show the migration plan (order, phases, blocking items)",
    "search": "Search the migration data for an object",
    "explain": "Explain why an object failed and how to fix it",
    "journey_status": "Show where the migration is in the journey and what comes next",
    "list_reports": "List the reports of one migration, grouped by phase",
    "migrations_overview": "List all migrations with their stage and reports",
    "model_status": "Show the trained risk model and its metrics",
    "start_training": "Start the pipeline that trains the risk model",
    "start_prediction": "Start the pipeline that predicts the risk of the next migration",
    "predict": "Show the objects most likely to fail",
    "generate_casc": "Generate the Configuration as Code of existing objects",
    "casc_schema": "Show the infra.aap_configuration format for new objects",
    "validate_casc": "Validate Configuration as Code before it is applied",
    "generate_smoke_suite": "Generate the smoke test suite (brand new objects only)",
    "smoke_results": "Read the uploaded smoke test results and find regressions",
}


def catalog() -> list[dict]:
    """The agents shown in the UI (Agents tab). Each tool has a readable label."""
    return [{"id": a.id, "name": a.name, "role": a.role, "tools": list(a.tools),
             "capabilities": [{"tool": t, "label": TOOL_LABELS.get(t, t.replace("_", " "))} for t in a.tools],
             "writes": a.writes, "phase": a.phase} for a in AGENTS.values()]
