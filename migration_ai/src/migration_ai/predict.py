"""AI-enhanced readiness report for the next AAP 2.4 to migrate (``enhanced-report`` style).

For every object of an AAP 2.4 export it combines:

* the deterministic pre-migration rules of the toolkit (RC-001..RC-011, stale)
* the risk model trained on finished migrations: probability of failure,
  most likely error class and the most similar objects that failed before

and renders the same interactive HTML/JSON/CSV contract as
``premigration-report``, with an extra root cause ``AI``.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from collections import Counter
from io import StringIO
from pathlib import Path
from typing import Any

import numpy as np

from migration_ai.config import Settings

log = logging.getLogger(__name__)

AI_RC = "AI"
AI_CATALOG = {
    "title": "Predicted by the risk model",
    "description": ("The model trained on previous migrations of this platform gives this object a high "
                    "probability of failing the import. It looks at the same metadata as the rules, plus "
                    "patterns learned from real import errors."),
    "remediation": ("Check the predicted error class and the similar objects that failed before; fix the "
                    "object on AAP 2.4 or plan a manual step after the import."),
    "post_migration_error": "learned from enhanced-report error classes",
}


def load_bundle(path: Path) -> dict:
    import joblib

    return joblib.load(path)


def fetch_latest_model(settings: Settings, dest: Path) -> tuple[Path, dict] | None:
    """Download the current model from S3 (models/<name>/latest.json)."""
    from migration_ai.storage import Store

    store = Store(settings.s3)
    latest = store.get_json(f"{settings.models_prefix}/latest.json")
    if not latest:
        return None
    base = f"{settings.models_prefix}/{latest['version']}"
    local = dest / latest["version"] / "model.joblib"
    if not local.is_file():
        store.get_file(f"{base}/model.joblib", local)
    for extra in ("model-card.md", "metrics.json"):
        if not (local.parent / extra).is_file() and store.exists(f"{base}/{extra}"):
            store.get_file(f"{base}/{extra}", local.parent / extra)
    return local, latest


def score(bundle: dict, rows: list[dict]) -> list[dict]:
    """Add p_fail, predicted_error and similar past failures to each row."""
    if not rows:
        return rows
    feats = [r["features"] for r in rows]
    model = bundle["failure_model"]
    proba = model.predict_proba(feats)[:, list(model.classes_).index(1)]
    err_model = bundle.get("error_model")
    err_pred = err_model.predict(feats) if err_model is not None else ["" for _ in rows]
    X = model.named_steps["vec"].transform(feats)
    _, idx = bundle["neighbors"].kneighbors(bundle["scaler"].transform(X))
    for r, p, e, nb in zip(rows, proba, err_pred, idx, strict=True):
        r["p_fail"] = round(float(p), 3)
        r["predicted_error"] = str(e) if p >= bundle["threshold"] else ""
        seen, similar = set(), []
        for i in nb:
            ex = bundle["examples"][i]
            if ex["outcome"] == "failed" and (ex["name"], ex["error_key"]) not in seen:
                seen.add((ex["name"], ex["error_key"]))
                similar.append(ex)
        r["similar_failed"] = similar[:3]
    return rows


def build_ai_report(work_dir: Path, bundle: dict, source_url: str = "", source_version: str = "") -> dict:
    """Toolkit premigration report + AI findings, same JSON contract."""
    from aap_migration.analysis.premigration import (
        RC_CATALOG,
        Finding,
        PreMigrationAnalyzer,
        PreMigrationResult,
        build_report_dict,
        load_inventory_from_export,
        summarize,
    )
    from aap_migration.cli.commands.migration_report_v2 import _HUMAN_READABLE_ERRORS

    from migration_ai.dataset import build_rows

    inventory = load_inventory_from_export(work_dir / "exports")
    analyzer = PreMigrationAnalyzer(inventory)
    rule_findings = analyzer.analyze()
    rows = score(bundle, build_rows(work_dir, require_labels=False))
    rule_worst: dict[str, str] = {}
    for f in rule_findings:
        if f.rc != "STALE" and (f.key not in rule_worst or f.severity == "critical"):
            rule_worst[f.key] = f.severity

    ai_findings = []
    thr = bundle["threshold"]
    for r in rows:
        p = r["p_fail"]
        if p < thr * 0.8:
            continue
        severity = "critical" if p >= max(thr, 0.7) else "high"
        rules = rule_worst.get(r["key"])
        agreement = ("also flagged by the pre-migration rules" if rules in ("critical", "high")
                     else "NOT flagged by the pre-migration rules")
        similar = "; ".join(f"{s['name']} ({s['type']}): {s['error_key'] or 'error'}" for s in r["similar_failed"])
        expl = _HUMAN_READABLE_ERRORS.get(r["predicted_error"], "")
        rid = int(r["source_id"])
        ai_findings.append(Finding(
            rc=AI_RC, severity=severity, resource_type=r["resource_type"], resource_id=rid,
            resource_name=r["name"], organization=r["organization"],
            title=f"{p:.0%} probability of import failure"
                  + (f" — likely '{r['predicted_error']}'" if r["predicted_error"] else ""),
            detail=(f"Model {bundle['version']}: {agreement}. "
                    + (f"Similar objects that failed in past migrations: {similar}. " if similar else "")
                    + (f"Typical cause: {expl.split('.')[0]}." if expl else "")).strip(),
            remediation=AI_CATALOG["remediation"],
            evidence={"p_fail": p, "predicted_error": r["predicted_error"], "similar_failed": r["similar_failed"]},
        ))
    findings = sorted(rule_findings + ai_findings, key=lambda f: (f.rc != AI_RC, f.rc, f.resource_type))
    resources, orgs = summarize(inventory, findings, analyzer.idx)
    from datetime import datetime, timezone

    result = PreMigrationResult(
        generated=datetime.now(tz=timezone.utc).isoformat(timespec="seconds"), source_url=source_url,
        source_version=source_version or "2.4", data_source="export",
        inventory_counts={rt: len(v or []) for rt, v in inventory.items() if v}, findings=findings,
        resources=resources, organizations=orgs,
    )
    report = build_report_dict(result)
    report["rc_catalog"] = {**RC_CATALOG, AI_RC: AI_CATALOG}
    p_by_key = {r["key"]: r for r in rows}
    for key, res in report["resources"].items():
        if key in p_by_key:
            res["p_fail"] = p_by_key[key]["p_fail"]
            res["predicted_error"] = p_by_key[key]["predicted_error"]
    flagged = [r for r in rows if r["p_fail"] >= thr]
    report["report_type"] = "ai_readiness"
    report["ai"] = {
        "model_version": bundle["version"], "threshold": thr,
        "trained_on": bundle["metrics"].get("migrations"), "model_metrics": bundle["metrics"].get("model_cv"),
        "rules_metrics": bundle["metrics"].get("rules_baseline"),
        "predicted_fail": len(flagged),
        "not_flagged_by_rules": sum(1 for r in flagged if rule_worst.get(r["key"]) not in ("critical", "high")),
        "rules_flag_model_disagrees": sum(1 for r in rows if rule_worst.get(r["key"]) == "critical"
                                          and r["p_fail"] < thr * 0.5),
        "top_predicted_errors": Counter(r["predicted_error"] for r in flagged if r["predicted_error"]).most_common(10),
        "expected_failures": round(float(np.sum([r["p_fail"] for r in rows])), 1),
    }
    return report


AI_REPORT_TITLE = "Pre-migration Agent Assessment"


def render_ai_html(report: dict) -> str:
    from aap_migration.reporting.premigration_html import render_premigration_html

    html = render_premigration_html(report)
    ai = report.get("ai", {})
    banner = (f"<p class=\"sub\"><span>AI model {ai.get('model_version')}</span>"
              f"<span>{ai.get('predicted_fail')} objects predicted to fail</span>"
              f"<span>{ai.get('not_flagged_by_rules')} not flagged by the rules</span></p>")
    html = re.sub(r"<title>.*?</title>", f"<title>{AI_REPORT_TITLE}</title>", html, count=1, flags=re.S)
    html = re.sub(r"<h1([^>]*)>.*?</h1>", lambda m: f"<h1{m.group(1)}>{AI_REPORT_TITLE}</h1>" + banner, html,
                  count=1, flags=re.S)
    return html


def render_ai_csv(report: dict) -> str:
    out = StringIO()
    w = csv.writer(out)
    w.writerow(["Organization", "Resource Type", "Resource ID", "Name", "Predicted Outcome", "AI p(fail)",
                "AI predicted error", "Rule codes", "Last Modified", "Modified By"])
    for r in sorted(report["resources"].values(), key=lambda x: -(x.get("p_fail") or 0)):
        w.writerow([r["organization"], r["resource_type"], r["id"], r["name"], r["outcome"], r.get("p_fail", ""),
                    r.get("predicted_error", ""), " ".join(c for c in r["rcs"] if c != AI_RC), r.get("modified", ""),
                    r.get("modified_by", "")])
    return out.getvalue()


def write_ai_report(work_dir: Path, bundle: dict, out_dir: Path, source_url: str = "") -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    report = build_ai_report(work_dir, bundle, source_url=source_url)
    (out_dir / "ai-readiness-report.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    (out_dir / "ai-readiness-report.html").write_text(render_ai_html(report), encoding="utf-8")
    (out_dir / "ai-readiness-report.csv").write_text(render_ai_csv(report), encoding="utf-8")
    return report


def quick_summary(settings: Settings, work_dir: Path) -> dict[str, Any]:
    """Short prediction summary for the agent's plan (no files written)."""
    local = None
    candidate = work_dir / "models" / "model.joblib"
    if candidate.is_file():
        local = candidate
    else:
        got = fetch_latest_model(settings, work_dir / "models-cache")
        if got:
            local = got[0]
    if local is None:
        return {"available": False, "reason": "no trained model yet"}
    bundle = load_bundle(local)
    report = build_ai_report(work_dir, bundle)
    return {"available": True, **report["ai"]}
