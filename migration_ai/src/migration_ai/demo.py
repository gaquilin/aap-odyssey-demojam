"""Synthetic migration workspace, to try every pipeline without AAP.

``seed`` creates a workspace that looks exactly like one produced by the
toolkit after a real migration:

* ``exports/``   synthetic AAP 2.4 export (toolkit ``tools/generate_demo_export.py``)
* ``database/``  toolkit state DB with *simulated* import results
* ``logs/``      migration log lines for the failures
* ``reports/``   premigration report + dependency analysis

Import results follow realistic patterns: most objects flagged by the
pre-migration rules fail with the matching the target AAP API error, a few failure
patterns are NOT covered by the rules (so the risk model has something to
learn beyond them) and a small share of transient gateway errors adds noise.
Everything produced by ``seed`` is marked as synthetic in ``exports/metadata.json``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import random
from datetime import datetime, timezone
from pathlib import Path

from migration_ai.config import Settings

RC_ERRORS = {
    "RC-001": "APIError: [400] Playbook '{playbook}' not found for project.",
    "RC-002": "APIError: [400] inventory: Job Template must provide 'inventory' or allow prompting for it; field required.",
    "RC-003": "APIError: [400] variables_needed_to_start: 'change_ticket' value missing.",
    "RC-004": "APIError: [400] Field is not configured to prompt on launch.",
    "RC-005": "Cannot import workflow: template(s) failed to import",
    "RC-006": "APIError: [400] Credential is required for a cloud source.",
    "RC-008": "Duplicate exists in target",
    "RC-009": "APIError: [400] 'aap24-exec-01.example.com' is not a valid hostname in policy_instance_list.",
    "RC-010": "Validation failed: custom_virtualenv is not supported.",
}
HIDDEN = {
    "notification_templates": "APIError: [400] notification_configuration: ['This field is required.']",
    "ask_limit_schedule": "APIError: [400] Field is not configured to prompt on launch (limit).",
}
TRANSIENT = "APIError: [502] Bad Gateway from platform gateway"


def _load_generator(toolkit_dir: str):
    for candidate in (Path(toolkit_dir) / "tools" / "generate_demo_export.py",
                      Path(__file__).resolve().parents[4] / "tools" / "generate_demo_export.py"):
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location("generate_demo_export", candidate)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise FileNotFoundError("tools/generate_demo_export.py of the toolkit not found (set TOOLKIT_DIR)")


def seed(settings: Settings, work_dir: Path, migration_id: str, orgs: int = 8, rnd_seed: int = 42,
         failure_noise: float = 0.03) -> dict:
    gen = _load_generator(settings.toolkit_dir)
    rnd = random.Random(rnd_seed)
    data = gen.Builder(orgs, rnd_seed).build()
    export_dir = work_dir / "exports"
    gen.write_export(data, export_dir, f"https://aap24-{migration_id}.demo.example.com/api/v2")
    meta = json.loads((export_dir / "metadata.json").read_text())
    meta.update({"synthetic": True, "migration_id": migration_id})
    (export_dir / "metadata.json").write_text(json.dumps(meta, indent=2))

    from aap_migration.analysis.premigration import (
        ANALYSED_TYPES,
        PreMigrationAnalyzer,
        _rid,
        build_report_dict,
        load_inventory_from_export,
        run_analysis,
    )

    inventory = load_inventory_from_export(export_dir)
    analyzer = PreMigrationAnalyzer(inventory)
    findings = analyzer.analyze()
    worst: dict[str, tuple[str, str]] = {}
    for f in findings:
        if f.rc == "STALE":
            continue
        cur = worst.get(f.key)
        order = {"critical": 4, "high": 3, "medium": 2, "low": 1}
        if cur is None or order[f.severity] > order[cur[0]]:
            worst[f.key] = (f.severity, f.rc)

    results = []
    for rtype in ANALYSED_TYPES:
        for item in inventory.get(rtype, []) or []:
            rid = _rid(item)
            if rid is None or (rtype == "credential_types" and item.get("managed")):
                continue
            name = item.get("name") or item.get("username") or f"{rtype}_{rid}"
            sev, rc = worst.get(f"{rtype}:{rid}", (None, None))
            status, msg = "completed", None
            if rc == "RC-008":
                status, msg = "skipped", "Skipped: Duplicate credential with same name/org/type"
            elif sev == "critical" and rc in RC_ERRORS and rnd.random() < 0.92:
                status, msg = "failed", RC_ERRORS[rc].format(playbook=item.get("playbook") or "site.yml")
            elif sev == "high" and rc in RC_ERRORS and rnd.random() < 0.6:
                status, msg = "failed", RC_ERRORS[rc].format(playbook=item.get("playbook") or "site.yml")
            elif rtype == "notification_templates" and item.get("notification_type") == "email" \
                    and rnd.random() < 0.85:
                status, msg = "failed", HIDDEN["notification_templates"]
            elif rtype == "schedules" and item.get("limit") and rnd.random() < 0.8:
                status, msg = "failed", HIDDEN["ask_limit_schedule"]
            elif rnd.random() < failure_noise:
                status, msg = "failed", TRANSIENT
            results.append((rtype, rid, name, status, msg))

    _write_db(work_dir, results)
    _write_logs(work_dir, results)
    reports = work_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    report = build_report_dict(run_analysis(inventory, meta["source_url"], "2.4", "export"))
    (reports / "premigration-report.json").write_text(json.dumps(report, indent=1, default=str))
    _write_dependencies(inventory, reports / "dependencies.json")
    for folder in ("xformed", "schemas"):
        (work_dir / folder).mkdir(parents=True, exist_ok=True)
    return {"migration_id": migration_id, "objects": len(results),
            "failed": sum(1 for r in results if r[3] == "failed"),
            "skipped": sum(1 for r in results if r[3] == "skipped")}


def _write_db(work_dir: Path, results: list[tuple]) -> None:
    from aap_migration.migration.database import get_session, init_database
    from aap_migration.migration.models import MigrationProgress

    db = work_dir / "database" / "migration_state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    url = f"sqlite:///{db}"
    init_database(url)
    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    target = 1000
    with get_session(url) as session:
        for rtype, rid, name, status, msg in results:
            target += 1
            session.add(MigrationProgress(
                resource_type=rtype, source_id=rid, source_name=name, phase="import", status=status,
                target_id=target if status == "completed" else None, error_message=msg,
                started_at=now, completed_at=now,
            ))
        session.commit()


def _write_logs(work_dir: Path, results: list[tuple]) -> None:
    logs = work_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with open(logs / "migration.log", "w", encoding="utf-8") as fh:
        for rtype, rid, name, status, msg in results:
            if status == "failed":
                fh.write(json.dumps({"event": "import_failed", "level": "error", "resource_type": rtype,
                                     "source_id": rid, "source_name": name, "error": msg}) + "\n")


def _write_dependencies(inventory: dict, out: Path) -> None:
    from aap_migration.analysis.dependency_analyzer import CrossOrgDependencyAnalyzer
    from aap_migration.cli.commands.analyze_dependencies import serialize_global_report_to_json

    class FakeClient:
        base_url = "https://aap24.demo.example.com/api/v2"

        async def get_paginated(self, ep, params=None, page_size=200):
            items = inventory.get(ep.strip("/"), [])
            params = params or {}
            if "name" in params:
                items = [i for i in items if i.get("name") == params["name"]]
            if "organization" in params:
                items = [i for i in items if i.get("organization") == params["organization"]]
            return items

        async def get(self, ep, params=None):
            rt, rid = ep.strip("/").split("/")[:2]
            for i in inventory.get(rt, []):
                if i.get("id") == int(rid):
                    return i
            raise KeyError(ep)

    async def run():
        return await CrossOrgDependencyAnalyzer(FakeClient()).analyze_all_organizations()

    import logging

    logging.getLogger().setLevel(logging.WARNING)
    rep = asyncio.run(run())
    out.write_text(serialize_global_report_to_json(rep))
