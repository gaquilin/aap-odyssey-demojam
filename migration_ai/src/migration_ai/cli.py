"""``migration-ai`` command line: one sub-command per pipeline step.

Every step follows the same pattern: pull the migration workspace from S3,
work locally in ``--work-dir`` (default ``$WORK_DIR`` or /work), push results.
Without S3 settings it works on the local directory only (handy on a laptop).
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from pathlib import Path

from migration_ai import __version__
from migration_ai.config import WORKSPACE_FOLDERS, Settings, load_settings

log = logging.getLogger("migration_ai")


def str2bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    if value.lower() in ("1", "true", "yes", "y", "on"):
        return True
    if value.lower() in ("0", "false", "no", "n", "off", ""):
        return False
    raise argparse.ArgumentTypeError(f"expected a boolean, got {value!r}")


def flag(parser: argparse.ArgumentParser, name: str, help_text: str = "") -> None:
    """--name, --name true, --name false all work (pipelines pass explicit values)."""
    parser.add_argument(name, type=str2bool, nargs="?", const=True, default=False, help=help_text)

def _store(settings: Settings):
    if not settings.s3.configured:
        return None
    from migration_ai.storage import Store

    return Store(settings.s3)


def _ws_prefix(settings: Settings, mid: str) -> str:
    from migration_ai.storage import workspace_prefix

    return workspace_prefix(settings.migrations_prefix, mid)


def pull(settings: Settings, mid: str, work: Path, folders=WORKSPACE_FOLDERS) -> None:
    store = _store(settings)
    if store is None:
        log.warning("S3 not configured: using local workspace %s", work)
        return
    from migration_ai.storage import pull_workspace

    counts = pull_workspace(store, _ws_prefix(settings, mid), work, folders)
    log.info("pulled workspace %s: %s", mid, counts)


def push(settings: Settings, mid: str, work: Path, folders=WORKSPACE_FOLDERS) -> None:
    store = _store(settings)
    if store is None:
        return
    from migration_ai.storage import push_workspace

    counts = push_workspace(store, _ws_prefix(settings, mid), work, folders)
    log.info("pushed workspace %s: %s", mid, counts)


def push_dir(settings: Settings, local: Path, key_prefix: str) -> None:
    store = _store(settings)
    if store is not None and local.is_dir():
        store.delete_prefix(key_prefix)
        n = store.upload_dir(local, key_prefix)
        log.info("uploaded %s files to s3://%s/%s", n, store.bucket, key_prefix)


def _write(path: str | None, text: str) -> None:
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text, encoding="utf-8")


def _md_to_html(md: str, title: str) -> str:
    """Tiny Markdown wrapper so KFP can show reports in the run's artifact view."""
    import html

    return (f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
            "<style>body{font:15px/1.5 system-ui,sans-serif;max-width:960px;margin:24px auto;padding:0 16px}"
            "pre{white-space:pre-wrap;background:#f4f6f8;padding:12px;border-radius:6px}</style></head><body>"
            f"<pre>{html.escape(md)}</pre></body></html>")


def cmd_ws(a, s: Settings) -> int:
    work = Path(a.work_dir)
    folders = a.folders.split(",") if a.folders else WORKSPACE_FOLDERS
    if a.action == "pull":
        pull(s, a.migration_id, work, folders)
    else:
        push(s, a.migration_id, work, folders)
    from migration_ai.storage import local_workspace_summary

    print(json.dumps(local_workspace_summary(work), indent=1))
    return 0


def cmd_ls(a, s: Settings) -> int:
    """Browse the 'AAP migration data' bucket: folders, or files with size and date."""
    store = _store(s)
    if store is None:
        print("S3 not configured (connection aap-migration-data)", file=sys.stderr)
        return 2
    prefix = a.prefix.strip("/")
    paginator = store.client.get_paginator("list_objects_v2")
    kw = {"Bucket": store.bucket, "Prefix": (prefix + "/") if prefix else ""}
    if not a.recursive:
        kw["Delimiter"] = "/"
        for page in paginator.paginate(**kw):
            for p in page.get("CommonPrefixes", []):
                print(f"{'':16}  {'<dir>':>12}  {p['Prefix']}")
    n = size = 0
    for page in paginator.paginate(**kw):
        for o in page.get("Contents", []):
            n += 1
            size += o["Size"]
            print(f"{o['LastModified']:%Y-%m-%d %H:%M}  {o['Size']:>12,}  {o['Key']}")
    print(f"-- bucket {store.bucket}: {n} files, {size / 1e6:.1f} MB under '{prefix or '/'}'", file=sys.stderr)
    return 0


def cmd_get(a, s: Settings) -> int:
    """Download one file or a whole folder of the bucket."""
    store = _store(s)
    if store is None:
        print("S3 not configured (connection aap-migration-data)", file=sys.stderr)
        return 2
    key, dest = a.key.strip("/"), Path(a.dest)
    if store.exists(key):
        target = dest / Path(key).name if dest.is_dir() or a.dest.endswith("/") else dest
        store.get_file(key, target)
        print(target)
    else:
        n = store.download_dir(key, dest)
        print(f"{n} files -> {dest}")
    return 0


def cmd_storage_init(a, s: Settings) -> int:
    store = _store(s)
    if store is None:
        print("S3 not configured", file=sys.stderr)
        return 2
    for b in [s.s3.bucket, *(a.extra_bucket or [])]:
        store.ensure_bucket(b)
        print(f"bucket ready: {b}")
    return 0


def cmd_prepare(a, s: Settings) -> int:
    """Pull workspace, export from AAP 2.4 when needed, readiness + dependency reports, push."""
    from migration_ai import toolkit

    work = Path(a.work_dir)
    pull(s, a.migration_id, work)
    has_export = (work / "exports" / "metadata.json").is_file()
    steps = []
    if a.refresh_export or not has_export:
        steps.append(("export", ["export", "--yes", "--force"]))
    steps.append(("premigration_report", ["premigration-report", "--format", "all"]))
    if a.with_dependencies:
        steps.append(("dependencies", ["analyze-dependencies", "--all", "--output-json",
                                       "reports/dependencies.json"]))
    rc = 0
    for name, args in steps:
        res = toolkit.run(s, work, args, f"prepare-{name}")
        print(f"[{name}] exit {res.returncode} in {res.seconds}s\n{res.output_tail[-1500:]}")
        if res.returncode != 0 and name != "dependencies":
            rc = res.returncode
            break
    if (work / "reports" / "dependencies.json").is_file() and (work / "reports" / "premigration-report.json").is_file():
        toolkit.run(s, work, ["render-dependency-report", "--input-json", "reports/dependencies.json",
                              "--risk-report", "reports/premigration-report.json",
                              "--output-html", "reports/dependencies.html"], "prepare-render")
    push(s, a.migration_id, work)
    if a.out_html:
        if (work / "reports" / "premigration-report.html").is_file():
            shutil.copy(work / "reports" / "premigration-report.html", a.out_html)
        else:
            _write(a.out_html, _md_to_html("premigration-report.html not produced: see the step log",
                                           "Pre-migration readiness"))
    return rc


def cmd_agent(a, s: Settings) -> int:
    from migration_ai.agent import MigrationAgent

    work = Path(a.work_dir)
    pull(s, a.migration_id, work)
    agent = MigrationAgent(s, work, a.mode, migration_id=a.migration_id, include_credentials=a.include_credentials,
                           include_iam=a.include_iam, include_settings=a.include_settings,
                           dry_run=a.dry_run, refresh_export=a.refresh_export,
                           max_retries=a.max_retries, abort_failure_rate=a.abort_failure_rate,
                           use_llm=a.use_llm and not a.no_llm)
    summary = agent.run()
    push(s, a.migration_id, work)
    push_dir(s, work / "reports" / "agent", f"{s.migrations_prefix}/{a.migration_id}/agent")
    if a.mode == "collect":
        from migration_ai.agent import render_summary_markdown

        md = render_summary_markdown(summary)
    else:
        md_name = "migration-plan.md" if a.mode == "plan" else "agent-summary.md"
        md = (work / "reports" / "agent" / md_name).read_text(encoding="utf-8")
    _write(a.out_md, md)
    _write(a.out_status, summary["status"])
    imported = sum(v for k, v in agent.db.totals().items() if k in ("completed", "failed"))
    collectable = summary["status"] in ("completed", "completed_with_failures") or (
        a.mode in ("execute", "collect") and not a.dry_run and imported > 0)
    _write(a.out_collectable, "true" if collectable else "false")
    print(md)
    return 0


def cmd_rebuild_summary(a, s: Settings) -> int:
    """Rebuild the Migration run summary from agent-log.jsonl and save it in the bucket."""
    from migration_ai.agent import MigrationAgent

    work = Path(a.work_dir)
    pull(s, a.migration_id, work)
    agent = MigrationAgent(s, work, "execute", migration_id=a.migration_id, include_credentials=a.include_credentials,
                           include_iam=a.include_iam, use_llm=False)
    summary = agent.rebuild_run_summary()
    if summary is None:
        print("no real migration run (mode=execute, dry_run=false) in reports/agent/agent-log.jsonl", file=sys.stderr)
        return 1
    push_dir(s, work / "reports" / "agent", f"{s.migrations_prefix}/{a.migration_id}/agent")
    print(f"Migration run summary rebuilt: status {summary['status']}, "
          f"{sum(1 for x in summary['steps'] if x['status'] == 'succeeded')} steps succeeded")
    return 0


def cmd_post_reports(a, s: Settings) -> int:
    from migration_ai import toolkit

    work = Path(a.work_dir)
    pull(s, a.migration_id, work)
    for fmt in ("html", "csv"):
        res = toolkit.run(s, work, ["enhanced-report", "--format", fmt], f"post-enhanced-{fmt}")
        print(f"[enhanced-report {fmt}] exit {res.returncode}\n{res.output_tail[-800:]}")
    push(s, a.migration_id, work, ("reports", "logs"))
    html = work / "reports" / "org-failures-enhanced.html"
    if a.out_html:
        if html.is_file():
            shutil.copy(html, a.out_html)
        else:
            _write(a.out_html, _md_to_html("enhanced-report not produced: see the step log", "Enhanced report"))
    return 0


def cmd_dataset(a, s: Settings) -> int:
    from migration_ai.dataset import build_rows, write_dataset

    work = Path(a.work_dir)
    pull(s, a.migration_id, work, ("exports", "database"))
    rows = build_rows(work, require_labels=True, migration_id=a.migration_id)
    out = work / "dataset"
    card = write_dataset(rows, out, a.migration_id)
    push_dir(s, out, f"{s.migrations_prefix}/{a.migration_id}/dataset")
    print(json.dumps(card, indent=1, default=str))
    if card["labelled_rows"] == 0:
        print("No labelled rows: the state database has no import results for the exported objects.",
              file=sys.stderr)
        return 3 if a.strict else 0
    return 0


def cmd_train(a, s: Settings) -> int:
    from migration_ai.dataset import read_dataset
    from migration_ai.train import train

    work = Path(a.work_dir)
    rows: list[dict] = []
    store = _store(s)
    if store is not None:
        mids = store.list_children(s.migrations_prefix) if a.migrations == "all" else a.migrations.split(",")
        for mid in mids:
            key = f"{s.migrations_prefix}/{mid}/dataset/dataset.jsonl"
            if store.exists(key):
                local = work / "datasets" / mid / "dataset.jsonl"
                store.get_file(key, local)
                rows.extend(read_dataset(local))
                log.info("dataset %s: loaded", mid)
    else:
        for p in sorted(work.glob("**/dataset/dataset.jsonl")):
            rows.extend(read_dataset(p))
    out = work / "model"
    if out.exists():
        shutil.rmtree(out)
    report = train(rows, out, threshold=a.threshold)
    print(json.dumps({k: v for k, v in report.items() if k != "top_features"}, indent=1, default=str))
    card = (out / "model-card.md").read_text(encoding="utf-8")
    _write(a.out_md, card)
    _write(a.out_status, report["status"])
    if report["status"] == "trained" and store is not None:
        prefix = f"{s.models_prefix}/{report['version']}"
        store.upload_dir(out, prefix)
        store.put_json(f"{s.models_prefix}/latest.json", {
            "version": report["version"], "trained": report["trained"], "rows": report["rows"],
            "migrations": report["migrations"], "metrics": report.get("model_cv"), "path": prefix})
        print(f"model published: s3://{store.bucket}/{prefix} (latest.json updated)")
    if report["status"] == "trained":
        m = report.get("model_cv") or {}
        print(f"RESULT: TRAINED - model {report['version']} on {report['rows']} objects; "
              f"recall(failed)={m.get('recall_failed')} vs rules {report['rules_baseline'].get('recall_failed')}")
    else:
        print(f"RESULT: NOT TRAINED ({report['status']}) - {report.get('message', '')}")
    return 0


def cmd_predict(a, s: Settings) -> int:
    from migration_ai import toolkit
    from migration_ai.predict import fetch_latest_model, load_bundle, write_ai_report

    work = Path(a.work_dir)
    pull(s, a.migration_id, work, ("exports", "reports"))
    if a.refresh_export or not (work / "exports" / "metadata.json").is_file():
        res = toolkit.run(s, work, ["export", "--yes", "--force"], "predict-export")
        if res.returncode != 0:
            print(res.output_tail, file=sys.stderr)
            return res.returncode
        push(s, a.migration_id, work, ("exports",))
    if a.model:
        model_path = Path(a.model)
    else:
        got = fetch_latest_model(s, work / "models-cache")
        if not got:
            msg = "No trained model yet: run the training pipeline first."
            print(msg, file=sys.stderr)
            _write(a.out_html, _md_to_html(msg, "AI readiness"))
            return 0 if not a.strict else 4
        model_path = got[0]
    bundle = load_bundle(model_path)
    out = work / "ai"
    meta = json.loads((work / "exports" / "metadata.json").read_text()) if (work / "exports" / "metadata.json").is_file() else {}
    report = write_ai_report(work, bundle, out, source_url=meta.get("source_url", ""))
    push_dir(s, out, f"{s.migrations_prefix}/{a.migration_id}/ai")
    if a.out_html:
        shutil.copy(out / "ai-readiness-report.html", a.out_html)
    print(json.dumps(report["ai"], indent=1, default=str))
    return 0


def cmd_casc(a, s: Settings) -> int:
    from migration_ai import casc, toolkit

    work = Path(a.work_dir)
    if a.source == "live":
        env = toolkit.load_env_file(s.toolkit_env_file)
        objs = casc.objects_from_live(env.get("TARGET__URL", ""), env.get("TARGET__TOKEN", ""),
                                      verify=env.get("TARGET__VERIFY_SSL", "true").lower() != "false",
                                      include_hosts=a.include_hosts, organization=a.organization or None)
    else:
        pull(s, a.migration_id, work, ("exports", "database"))
        objs = casc.objects_from_migration(work)
    types = a.resource_types.split(",") if a.resource_types else casc.CASC_TYPES
    scopes, secrets, notes = casc.convert(objs, a.organization or None, types, a.include_hosts)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = work / "casc" / stamp
    summary = casc.write_casc(scopes, secrets, notes, out, a.source)
    review = ""
    if a.review:
        review = _casc_review(s, out, summary)
        if review:
            (out / "AI-REVIEW.md").write_text(review, encoding="utf-8")
    archive = shutil.make_archive(str(out), "zip", out)
    store = _store(s)
    if store is not None:
        prefix = f"{s.migrations_prefix}/{a.migration_id}/casc/{stamp}"
        store.upload_dir(out, prefix)
        store.put_file(Path(archive), f"{prefix}.zip")
        print(f"CasC uploaded to s3://{store.bucket}/{prefix}/ and {prefix}.zip")
    readme = (out / "README.md").read_text(encoding="utf-8") + ("\n\n" + review if review else "")
    _write(a.out_md, readme)
    print(readme)
    return 1 if summary["validation"]["errors"] and a.strict else 0


def _casc_review(s: Settings, out: Path, summary: dict) -> str:
    from migration_ai.llm import LLM

    llm = LLM(s.llm)
    if not llm.available():
        return ""
    sample = []
    for f in summary["files"][:6]:
        sample.append(f"# {f}\n" + (out / f).read_text(encoding="utf-8")[:3000])
    system = ("You review Ansible Automation Platform (target version) Configuration as Code written for the "
              "infra.aap_configuration collection. In English, Markdown, max 250 words: list risks "
              "(hard-coded environment values, missing references, secrets, manual projects, VM-specific "
              "instance groups) and concrete improvements. Only use the content provided.")
    try:
        text = llm.complete(system, json.dumps({"summary": summary}, default=str)[:6000] + "\n\n" + "\n\n".join(sample),
                            900)
        return "## Revisione AI\n\n" + text
    except Exception as exc:
        log.warning("CasC review failed: %s", exc)
        return ""


def cmd_ask(a, s: Settings) -> int:
    from migration_ai.assistant import Copilot
    from migration_ai.predict import fetch_latest_model

    work = Path(a.work_dir)
    pull(s, a.migration_id, work)
    model = Path(a.model) if a.model else None
    if model is None and _store(s) is not None:
        got = fetch_latest_model(s, work / "models-cache")
        model = got[0] if got else None
    from migration_ai.agents import AGENT_NAMES, Orchestrator

    cp = Copilot(s, work, migration_id=a.migration_id, model_path=model, use_llm=not a.no_llm)
    res = Orchestrator(cp).ask(a.question)
    print(f"[{AGENT_NAMES.get(res['agent'], res['agent'])}]")
    print(res["answer"])
    for f in res["files"]:
        print(f"[file] {f}")
    return 0


def cmd_releases(a, s: Settings) -> int:
    """Smart Planning agent from the console: patch releases of an AAP version."""
    from migration_ai import releases

    work = Path(a.work_dir)
    fetcher = releases.fetcher_for(work / "_cache" / "releases", s.s3)
    version = a.version or releases.aap_version_for(a.installed or "", fetcher)
    if not version:
        print("AAP version unknown: pass --version, or --installed with the controller version", file=sys.stderr)
        return 2
    plan = releases.build_plan(version, fetcher, controller_version=a.installed or "")
    print(json.dumps(plan, indent=1, default=str) if a.json else releases.plan_markdown(plan))
    return 0


def cmd_smoke(a, s: Settings) -> int:
    """Smoke & regression test agent from the console: generate the platform test suite."""
    from migration_ai import smoke

    work = Path(a.work_dir)
    if a.migration_id and _store(s) is not None:
        pull(s, a.migration_id, work)
    res = smoke.generate_suite(work, a.migration_id or "standalone", Path(a.out) if a.out else None)
    print(f"Suite: {res['output_dir']}  ({len(res['tests'])} tests, validation errors: "
          f"{len(res['validation']['errors'])})\nZip:   {res['zip']}\nCustomize, then run: "
          "ansible-playbook playbooks/smoke.yml -e smoke_phase=pre|post|patch --ask-vault-pass")
    return 0


def cmd_serve(a, s: Settings) -> int:
    from migration_ai.webapp import serve

    serve(a.port)
    return 0


def cmd_demo(a, s: Settings) -> int:
    from migration_ai.demo import seed

    work = Path(a.work_dir)
    if work.exists() and a.clean:
        shutil.rmtree(work)
    info = seed(s, work, a.migration_id, orgs=a.orgs, rnd_seed=a.seed)
    if a.push:
        push(s, a.migration_id, work)
    print(json.dumps(info, indent=1))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="migration-ai", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--work-dir", default=None, help="local workspace (default $WORK_DIR or /work)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def mid(sp, required=True):
        sp.add_argument("--migration-id", required=required, help="folder under migrations/ in the bucket")

    sp = sub.add_parser("ws", help="pull/push the toolkit folders of a migration")
    sp.add_argument("action", choices=["pull", "push"])
    mid(sp)
    sp.add_argument("--folders", help=f"comma separated subset of {','.join(WORKSPACE_FOLDERS)}")
    sp.set_defaults(fn=cmd_ws)

    sp = sub.add_parser("ls", help="browse the migration data bucket (e.g. ls migrations/<id>)")
    sp.add_argument("prefix", nargs="?", default="")
    sp.add_argument("-r", "--recursive", action="store_true", help="all files below the prefix")
    sp.set_defaults(fn=cmd_ls)

    sp = sub.add_parser("get", help="download a file or folder of the bucket (e.g. get migrations/<id>/agent .)")
    sp.add_argument("key")
    sp.add_argument("dest", nargs="?", default=".")
    sp.set_defaults(fn=cmd_get)

    sp = sub.add_parser("storage-init", help="create the S3 buckets")
    sp.add_argument("--extra-bucket", action="append")
    sp.set_defaults(fn=cmd_storage_init)

    sp = sub.add_parser("prepare", help="export (if needed) + premigration report + dependency analysis")
    mid(sp)
    flag(sp, "--refresh-export")
    flag(sp, "--with-dependencies")
    sp.add_argument("--out-html")
    sp.set_defaults(fn=cmd_prepare)

    sp = sub.add_parser("agent", help="migration agent: plan, execute or collect")
    mid(sp)
    sp.add_argument("--mode", choices=["plan", "execute", "collect"], default="plan")
    flag(sp, "--dry-run", "execute without writing to the target AAP")
    flag(sp, "--refresh-export")
    flag(sp, "--include-credentials")
    flag(sp, "--include-iam")
    flag(sp, "--include-settings", "also import global settings (overwrites the target AAP configuration)")
    sp.add_argument("--max-retries", type=int, default=0, help=argparse.SUPPRESS)
    sp.add_argument("--abort-failure-rate", type=float, default=0.5)
    flag(sp, "--no-llm")
    sp.add_argument("--use-llm", type=str2bool, default=True, help="false = deterministic policy only")
    sp.add_argument("--out-md")
    sp.add_argument("--out-status")
    sp.add_argument("--out-collectable", help="file receiving 'true' when the migration produced import results")
    sp.set_defaults(fn=cmd_agent)

    sp = sub.add_parser("rebuild-summary", help="rebuild the Migration run summary from agent-log.jsonl")
    mid(sp)
    flag(sp, "--include-credentials")
    flag(sp, "--include-iam")
    sp.set_defaults(fn=cmd_rebuild_summary)

    sp = sub.add_parser("post-reports", help="toolkit enhanced-report (HTML + CSV) on a finished migration")
    mid(sp)
    sp.add_argument("--out-html")
    sp.set_defaults(fn=cmd_post_reports)

    sp = sub.add_parser("dataset", help="build the training dataset of a finished migration")
    mid(sp)
    flag(sp, "--strict", "fail when there is nothing to learn from")
    sp.set_defaults(fn=cmd_dataset)

    sp = sub.add_parser("train", help="train the risk model on the datasets in S3")
    sp.add_argument("--migrations", default="all", help="'all' or comma separated migration ids")
    sp.add_argument("--threshold", type=float, default=0.5)
    sp.add_argument("--out-md")
    sp.add_argument("--out-status")
    sp.set_defaults(fn=cmd_train)

    sp = sub.add_parser("predict", help="AI readiness report (enhanced-report style) for the next AAP 2.4 to migrate")
    mid(sp)
    flag(sp, "--refresh-export", "export again from the AAP in .env SOURCE__*")
    sp.add_argument("--model", help="local model.joblib instead of the latest in S3")
    flag(sp, "--strict")
    sp.add_argument("--out-html")
    sp.set_defaults(fn=cmd_predict)

    sp = sub.add_parser("casc", help="generate infra.aap_configuration CasC for the target AAP")
    mid(sp)
    sp.add_argument("--source", choices=["migration", "live"], default="migration")
    sp.add_argument("--organization", default="")
    sp.add_argument("--resource-types", default="")
    flag(sp, "--include-hosts")
    flag(sp, "--review", "ask the LLM to review the generated CasC")
    flag(sp, "--strict")
    sp.add_argument("--out-md")
    sp.set_defaults(fn=cmd_casc)

    sp = sub.add_parser("ask", help="ask AAP Odyssey")
    mid(sp)
    sp.add_argument("question")
    sp.add_argument("--model")
    flag(sp, "--no-llm")
    sp.set_defaults(fn=cmd_ask)

    sp = sub.add_parser("releases", help="Smart Planning agent: AAP patch releases from docs.redhat.com")
    sp.add_argument("--version", default="", help="AAP version, e.g. 2.5, 2.6, 2.7 (default: detected from --installed)")
    sp.add_argument("--installed", help="installed controller version (e.g. 4.7.16) to list the newer releases")
    flag(sp, "--json")
    sp.set_defaults(fn=cmd_releases)

    sp = sub.add_parser("smoke", help="generate the platform smoke test suite of the target AAP (brand new objects only)")
    mid(sp, required=False)
    sp.add_argument("--out", help="output folder (default <work-dir>/smoke)")
    sp.set_defaults(fn=cmd_smoke)

    sp = sub.add_parser("serve", help="AAP Odyssey web UI")
    sp.add_argument("--port", type=int, default=8080)
    sp.set_defaults(fn=cmd_serve)

    sp = sub.add_parser("demo-seed", help="synthetic migration workspace (no AAP needed)")
    mid(sp)
    sp.add_argument("--orgs", type=int, default=8)
    sp.add_argument("--seed", type=int, default=42)
    flag(sp, "--clean")
    flag(sp, "--push", "upload the workspace to S3")
    sp.set_defaults(fn=cmd_demo)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("botocore", "boto3", "urllib3", "httpx", "openai", "s3transfer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    settings = load_settings()
    if args.work_dir is None:
        args.work_dir = str(settings.work_dir)
    if hasattr(args, "migration_id") and args.migration_id:
        args.work_dir = str(Path(args.work_dir))
    Path(args.work_dir).mkdir(parents=True, exist_ok=True)
    return int(args.fn(args, settings) or 0)


if __name__ == "__main__":
    sys.exit(main())
