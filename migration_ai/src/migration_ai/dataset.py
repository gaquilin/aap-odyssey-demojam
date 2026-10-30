"""Training data from a finished migration.

One row per AAP 2.4 object that the toolkit tried to import:

* **features** come from the RAW export (the same metadata used by
  ``enhanced-report`` and ``premigration-report``) plus the deterministic
  pre-migration rules (which RC codes fired), so the model can learn which
  rules really predict failures and which patterns the rules miss;
* **labels** come from the toolkit state database: the import status
  (completed / failed / skipped) and, for failures, the normalised error class
  (``normalize_error_key``, the same classification as ``enhanced-report``).

Organization names, object names and IDs are kept as metadata columns but are
never used as features, so a model trained on one AAP 2.4 can score another.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from migration_ai import toolkit

FEATURE_VERSION = 1
SEVERITY_SCORE = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
_SCHEDULE_PROMPTS = ("inventory", "scm_branch", "job_type", "job_tags", "skip_tags", "limit", "diff_mode",
                     "verbosity", "execution_environment", "forks", "job_slice_count", "timeout")
META_COLUMNS = ["key", "resource_type", "source_id", "name", "organization"]
TRANSIENT_RE = re.compile(r"\[50[234]\]|Bad Gateway|Service Unavailable|Gateway Timeout|timed? ?out|"
                          r"Connection (reset|refused|aborted)", re.I)


def _days(now: datetime, value: Any) -> float | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (now - dt).total_seconds() / 86400)


def _as_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            v = json.loads(value)
            return v if isinstance(v, dict) else {}
        except json.JSONDecodeError:
            try:
                import yaml

                v = yaml.safe_load(value)
                return v if isinstance(v, dict) else {}
            except Exception:
                return {}
    return {}


def resource_features(rtype: str, item: dict, findings: list, now: datetime) -> dict[str, Any]:
    """Generic, instance-independent features of one RAW AAP 2.4 object."""
    from aap_migration.cli.commands.migration_report_v2 import extract_export_metadata

    m = extract_export_metadata(rtype, item)
    sf = item.get("summary_fields") or {}
    f: dict[str, Any] = {"type": rtype}

    last_run = _days(now, m.get("last_job_run"))
    modified = _days(now, m.get("modified"))
    f["never_run"] = int(last_run is None and rtype in ("job_templates", "workflow_job_templates", "projects"))
    f["log_days_since_run"] = round(math.log1p(last_run), 3) if last_run is not None else -1.0
    f["log_days_since_modified"] = round(math.log1p(modified), 3) if modified is not None else -1.0
    f["last_job_failed"] = int(bool(m.get("last_job_failed")))
    f["last_update_failed"] = int(bool(m.get("last_update_failed")))
    status = (m.get("sync_status") or "").lower()
    if status:
        f["status"] = status
    f["has_organization"] = int(bool(item.get("organization") or sf.get("organization")))
    f["custom_virtualenv"] = int(bool(item.get("custom_virtualenv")))

    if rtype == "projects":
        f["scm_type"] = item.get("scm_type") or "manual"
        f["has_scm_url"] = int(bool(item.get("scm_url")))
        f["has_credential"] = int(bool(item.get("credential")))
    elif rtype == "job_templates":
        f["has_project"] = int(bool(item.get("project") or sf.get("project")))
        f["has_playbook"] = int(bool(item.get("playbook")))
        f["has_inventory"] = int(bool(item.get("inventory") or sf.get("inventory")))
        f["ask_inventory"] = int(bool(item.get("ask_inventory_on_launch")))
        f["ask_variables"] = int(bool(item.get("ask_variables_on_launch")))
        f["n_ask_flags"] = sum(1 for k, v in item.items() if k.startswith("ask_") and v)
        f["survey_enabled"] = int(bool(item.get("survey_enabled")))
        spec = (item.get("survey_spec") or {}).get("spec") or []
        f["n_survey_required"] = sum(1 for q in spec if isinstance(q, dict) and q.get("required"))
        f["n_credentials"] = len(sf.get("credentials") or [])
        f["callback"] = int(bool(item.get("host_config_key")))
    elif rtype == "workflow_job_templates":
        nodes = item.get("nodes") or []
        f["wf_nodes"] = len(nodes)
        f["wf_null_nodes"] = sum(1 for n in nodes if isinstance(n, dict) and n.get("unified_job_template") is None)
        f["survey_enabled"] = int(bool(item.get("survey_enabled")))
    elif rtype == "schedules":
        f["enabled"] = int(bool(item.get("enabled", True)))
        f["has_next_run"] = int(bool(item.get("next_run")))
        f["n_extra_data"] = len(_as_dict(item.get("extra_data")))
        f["n_overrides"] = sum(1 for k in _SCHEDULE_PROMPTS if item.get(k) not in (None, "", [], {}))
        ujt = (sf.get("unified_job_template") or {}).get("unified_job_type") or "none"
        f["parent_type"] = ujt
    elif rtype == "inventories":
        f["kind"] = item.get("kind") or "normal"
        f["log_hosts"] = round(math.log1p(int(item.get("total_hosts") or 0)), 3)
        f["pending_deletion"] = int(bool(item.get("pending_deletion")))
        total_src = int(item.get("total_inventory_sources") or 0)
        f["n_sources"] = total_src
        f["sources_failing_ratio"] = round(int(item.get("inventory_sources_with_failures") or 0) / total_src, 3) \
            if total_src else 0.0
    elif rtype == "inventory_sources":
        f["source"] = item.get("source") or "unknown"
        f["has_credential"] = int(bool(item.get("credential") or sf.get("credentials")))
        f["has_source_project"] = int(bool(item.get("source_project")))
        f["has_source_path"] = int(bool(item.get("source_path")))
    elif rtype == "credentials":
        inputs = item.get("inputs") if isinstance(item.get("inputs"), dict) else {}
        f["credential_type"] = (sf.get("credential_type") or {}).get("name") or "unknown"
        f["n_secret_inputs"] = sum(1 for v in inputs.values() if v == "$encrypted$")
        f["n_inputs"] = len(inputs)
    elif rtype == "users":
        f["external_auth"] = int(bool(item.get("ldap_dn") or item.get("external_account")))
        f["superuser"] = int(bool(item.get("is_superuser")))
    elif rtype == "instance_groups":
        f["container_group"] = int(bool(item.get("is_container_group")))
        f["n_policy_instances"] = len(item.get("policy_instance_list") or [])
    elif rtype == "execution_environments":
        f["deprecated_image"] = int(any(x in (item.get("image") or "").lower() for x in ("ee-29", "ansible-2.9")))

    rcs = Counter(fd.rc for fd in findings)
    for rc, n in rcs.items():
        f[f"rule_{rc}"] = n
    f["rule_max_severity"] = max((SEVERITY_SCORE[fd.severity] for fd in findings if fd.rc != "STALE"), default=0)
    f["rule_cascade"] = int(any(fd.cascade_from for fd in findings))
    f["rule_stale"] = int(rcs.get("STALE", 0) > 0)
    return f


def build_rows(work_dir: Path, require_labels: bool = True, migration_id: str = "") -> list[dict]:
    """Rows (features + metadata + labels) for every exported object of a workspace."""
    from aap_migration.analysis.premigration import (
        ANALYSED_TYPES,
        PreMigrationAnalyzer,
        _rid,
        load_inventory_from_export,
    )

    export_dir = work_dir / "exports"
    inventory = load_inventory_from_export(export_dir)
    analyzer = PreMigrationAnalyzer(inventory)
    findings = analyzer.analyze()
    by_key: dict[str, list] = {}
    for fd in findings:
        by_key.setdefault(fd.key, []).append(fd)

    labels: dict[tuple[str, int], dict] = {}
    if require_labels:
        db = toolkit.StateDB(work_dir / toolkit.DB_RELATIVE)
        for r in db.import_rows():
            labels[(r["resource_type"], int(r["source_id"]))] = r

    now = datetime.now(tz=timezone.utc)
    meta_now = _export_time(export_dir) or now
    rows: list[dict] = []
    for rtype in ANALYSED_TYPES:
        for item in inventory.get(rtype, []) or []:
            rid = _rid(item)
            if rid is None:
                continue
            if rtype == "credentials" and item.get("managed"):
                continue
            key = f"{rtype}:{rid}"
            lab = labels.get((rtype, rid))
            if require_labels and lab is None:
                continue
            row = {
                "key": key, "resource_type": rtype, "source_id": rid,
                "name": item.get("name") or item.get("username") or key,
                "organization": analyzer.idx.org_of(rtype, item),
                "migration_id": migration_id,
                "features": resource_features(rtype, item, by_key.get(key, []), meta_now),
            }
            if lab is not None:
                row.update(_label(lab))
            rows.append(row)
    return rows


def _export_time(export_dir: Path) -> datetime | None:
    meta = export_dir / "metadata.json"
    if not meta.is_file():
        return None
    try:
        ts = json.loads(meta.read_text(encoding="utf-8")).get("export_timestamp")
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _label(rec: dict) -> dict:
    from aap_migration.cli.commands.migration_report_v2 import normalize_error_key

    status = (rec.get("status") or "").lower()
    msg = rec.get("error_message") or ""
    if status == "failed" and TRANSIENT_RE.search(msg):
        return {"import_status": status, "outcome": "transient", "error_key": "", "error_message": msg[:500]}
    if status == "completed":
        outcome = "ok"
    elif status == "failed":
        outcome = "failed"
    elif status == "skipped":
        outcome = "skipped"
    else:
        outcome = "pending"
    return {
        "import_status": status, "outcome": outcome,
        "error_key": normalize_error_key(msg) if outcome in ("failed", "skipped") else "",
        "error_message": msg[:500],
    }


def write_dataset(rows: list[dict], out_dir: Path, migration_id: str) -> dict:
    """dataset.jsonl (one row per object) + datacard.json."""
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "dataset.jsonl", "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, default=str) + "\n")
    labelled = [r for r in rows if r.get("outcome") in ("ok", "failed", "skipped")]
    transient = sum(1 for r in rows if r.get("outcome") == "transient")
    card = {
        "migration_id": migration_id, "created": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "feature_version": FEATURE_VERSION, "rows": len(rows), "labelled_rows": len(labelled),
        "outcomes": dict(Counter(r.get("outcome") for r in labelled)),
        "excluded_transient_failures": transient,
        "by_type": dict(Counter(r["resource_type"] for r in labelled)),
        "top_error_keys": Counter(r["error_key"] for r in labelled if r.get("outcome") == "failed").most_common(15),
        "sources": ["exports/ (RAW AAP 2.4 objects)", "database/migration_state.db (import results)",
                    "premigration rules (aap_migration.analysis.premigration)"],
    }
    (out_dir / "datacard.json").write_text(json.dumps(card, indent=1, default=str), encoding="utf-8")
    return card


def read_dataset(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]
