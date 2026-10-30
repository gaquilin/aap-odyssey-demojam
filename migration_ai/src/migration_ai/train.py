"""Train and evaluate the migration risk model.

Two scikit-learn models are trained on the datasets of finished migrations:

``failure``  binary classifier: will this AAP 2.4 object fail to import into the target AAP?
``error``    multi-class classifier (failed objects only): which error class?
             Classes are the ``enhanced-report`` error keys; rare ones -> "Other".

Evaluation is honest about small data: stratified cross-validation when
there are enough examples, a comparison with the deterministic pre-migration
rules (the baseline to beat), and a clear "not enough data" status otherwise.
The bundle also stores the training vectors so predictions can be explained
with the most similar objects that failed in past migrations.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from migration_ai.dataset import FEATURE_VERSION

log = logging.getLogger(__name__)

MIN_ROWS = 30
MIN_PER_CLASS = 5
MIN_ERROR_EXAMPLES = 3


def _pipeline(n_classes: int):
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.feature_extraction import DictVectorizer
    from sklearn.pipeline import Pipeline

    clf = HistGradientBoostingClassifier(
        max_iter=200, learning_rate=0.08, max_leaf_nodes=15, l2_regularization=1.0,
        class_weight="balanced", random_state=42,
    )
    return Pipeline([("vec", DictVectorizer(sparse=False)), ("clf", clf)])


def _rule_baseline(features: list[dict]) -> np.ndarray:
    """Pre-migration rules: an object is predicted to fail when a critical rule fired."""
    return np.array([1 if f.get("rule_max_severity", 0) >= 4 else 0 for f in features])


def _binary_metrics(y: np.ndarray, pred: np.ndarray, proba: np.ndarray | None = None) -> dict:
    from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score, roc_auc_score

    out = {
        "accuracy": round(float(accuracy_score(y, pred)), 3),
        "precision_failed": round(float(precision_score(y, pred, zero_division=0)), 3),
        "recall_failed": round(float(recall_score(y, pred, zero_division=0)), 3),
        "f1_failed": round(float(f1_score(y, pred, zero_division=0)), 3),
    }
    if proba is not None and len(set(y)) == 2:
        out["roc_auc"] = round(float(roc_auc_score(y, proba)), 3)
    return out


def train(rows: list[dict], out_dir: Path, version: str | None = None, threshold: float = 0.5) -> dict:
    """Train both models on labelled rows and write model.joblib + metrics.json + model-card.md."""
    import joblib
    from sklearn.inspection import permutation_importance
    from sklearn.model_selection import StratifiedKFold, cross_val_predict
    from sklearn.neighbors import NearestNeighbors
    from sklearn.preprocessing import StandardScaler

    out_dir.mkdir(parents=True, exist_ok=True)
    version = version or datetime.now(tz=timezone.utc).strftime("v%Y%m%d-%H%M%S")
    labelled = [r for r in rows if r.get("outcome") in ("ok", "failed", "skipped")]
    features = [r["features"] for r in labelled]
    y = np.array([1 if r["outcome"] == "failed" else 0 for r in labelled])
    counts = Counter(y.tolist())
    migrations = sorted({r.get("migration_id") or "" for r in labelled})
    report: dict[str, Any] = {
        "version": version, "trained": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "feature_version": FEATURE_VERSION, "rows": len(labelled), "failed": counts.get(1, 0),
        "not_failed": counts.get(0, 0), "migrations": migrations, "threshold": threshold,
    }

    if len(labelled) < MIN_ROWS or min(counts.get(0, 0), counts.get(1, 0)) < MIN_PER_CLASS:
        report["status"] = "not_enough_data"
        report["message"] = (f"Need at least {MIN_ROWS} labelled objects and {MIN_PER_CLASS} per class "
                             f"(have {len(labelled)}: {counts.get(1, 0)} failed, {counts.get(0, 0)} not failed). "
                             "Run more migrations (or the demo seed) and train again.")
        (out_dir / "metrics.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
        (out_dir / "model-card.md").write_text(render_model_card(report), encoding="utf-8")
        return report

    n_splits = max(2, min(5, min(counts.values())))
    cv = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=42)
    model = _pipeline(2)
    proba_cv = cross_val_predict(model, features, y, cv=cv, method="predict_proba")[:, 1]
    pred_cv = (proba_cv >= threshold).astype(int)
    report["cv_folds"] = n_splits
    report["model_cv"] = _binary_metrics(y, pred_cv, proba_cv)
    report["rules_baseline"] = _binary_metrics(y, _rule_baseline(features))
    model.fit(features, y)

    base = _rule_baseline(features)
    report["caught_by_model_missed_by_rules"] = int(((pred_cv == 1) & (base == 0) & (y == 1)).sum())
    report["false_alarms_rules_removed_by_model"] = int(((pred_cv == 0) & (base == 1) & (y == 0)).sum())

    vec = model.named_steps["vec"]
    X = vec.transform(features)
    imp = permutation_importance(model.named_steps["clf"], X, y, n_repeats=5, random_state=42, scoring="f1")
    names = vec.get_feature_names_out()
    order = np.argsort(-imp.importances_mean)[:15]
    report["top_features"] = [[str(names[i]), round(float(imp.importances_mean[i]), 4)] for i in order
                              if imp.importances_mean[i] > 0]

    failed = [r for r in labelled if r["outcome"] == "failed"]
    err_counts = Counter(r["error_key"] or "Unknown error" for r in failed)
    keep = {k for k, n in err_counts.items() if n >= MIN_ERROR_EXAMPLES}
    err_labels = [(r["error_key"] or "Unknown error") if (r["error_key"] or "Unknown error") in keep else "Other"
                  for r in failed]
    error_model = None
    if len(set(err_labels)) >= 2:
        error_model = _pipeline(len(set(err_labels)))
        ec = Counter(err_labels)
        if min(ec.values()) >= 2:
            ecv = StratifiedKFold(n_splits=min(3, min(ec.values())), shuffle=True, random_state=42)
            epred = cross_val_predict(error_model, [r["features"] for r in failed], err_labels, cv=ecv)
            report["error_model_cv_accuracy"] = round(float(np.mean(np.array(epred) == np.array(err_labels))), 3)
        error_model.fit([r["features"] for r in failed], err_labels)
        report["error_classes"] = dict(Counter(err_labels))
    else:
        report["error_classes"] = dict(Counter(err_labels))
        report["error_model"] = "skipped (a single error class)"

    scaler = StandardScaler().fit(X)
    nn = NearestNeighbors(n_neighbors=min(5, len(X))).fit(scaler.transform(X))
    examples = [{"key": r["key"], "name": r["name"], "type": r["resource_type"], "outcome": r["outcome"],
                 "error_key": r.get("error_key", ""), "migration_id": r.get("migration_id", "")} for r in labelled]

    bundle = {"version": version, "feature_version": FEATURE_VERSION, "threshold": threshold,
              "failure_model": model, "error_model": error_model, "scaler": scaler, "neighbors": nn,
              "examples": examples, "metrics": report}
    joblib.dump(bundle, out_dir / "model.joblib", compress=3)
    report["status"] = "trained"
    (out_dir / "metrics.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    (out_dir / "model-card.md").write_text(render_model_card(report), encoding="utf-8")
    return report


def render_model_card(r: dict) -> str:
    lines = [f"# Migration risk model `{r['version']}`", "",
             f"* Trained on {r['trained']} with {r['rows']} objects "
             f"({r['failed']} failed, {r['not_failed']} not failed) from {len(r['migrations'])} migration(s): "
             f"{', '.join(m or 'n/a' for m in r['migrations'])}",
             f"* Status: **{r.get('status')}**"]
    if r.get("status") != "trained":
        lines += ["", r.get("message", "")]
        return "\n".join(lines) + "\n"
    m, b = r["model_cv"], r["rules_baseline"]
    lines += [f"* Evaluation: stratified {r['cv_folds']}-fold cross-validation, threshold {r['threshold']}", "",
              "| Metric ('failed' class) | Model | Pre-migration rules |", "|---|---:|---:|",
              f"| Precision | {m['precision_failed']} | {b['precision_failed']} |",
              f"| Recall | {m['recall_failed']} | {b['recall_failed']} |",
              f"| F1 | {m['f1_failed']} | {b['f1_failed']} |",
              f"| Accuracy | {m['accuracy']} | {b['accuracy']} |"]
    if "roc_auc" in m:
        lines.append(f"| ROC AUC | {m['roc_auc']} | — |")
    lines += ["", f"* Failures caught by the model but missed by the rules: {r['caught_by_model_missed_by_rules']}",
              f"* Rule false alarms removed by the model: {r['false_alarms_rules_removed_by_model']}"]
    if r.get("error_model_cv_accuracy") is not None:
        lines.append(f"* Accuracy of the predicted error class (CV): {r['error_model_cv_accuracy']}")
    if r.get("top_features"):
        lines += ["", "## Most important features", "", "| Feature | Importance |", "|---|---:|"]
        lines += [f"| `{n}` | {v} |" for n, v in r["top_features"]]
    lines += ["", "## Limitations", "",
              "* The model learns from the real results of past migrations: with few objects or a single "
              "migration the metrics are only indicative. Retrain after every migration.",
              "* It does not replace the rules: the AI report shows both assessments.",
              "* It uses no names, IDs or organizations as features, so it applies to other AAP 2.4 instances."]
    return "\n".join(lines) + "\n"
