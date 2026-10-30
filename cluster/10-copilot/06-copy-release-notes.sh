#!/usr/bin/env bash
set -euo pipefail
NS=aap-migration-ai
MODE=browser
HEADLESS=0
for a in "$@"; do
  case "$a" in
    --browser) MODE=browser ;;
    --curl) MODE=curl ;;
    --headless) HEADLESS=1 ;;
    *) echo "unknown option $a" >&2; exit 2 ;;
  esac
done
PY="${PYTHON:-}"
[ -z "$PY" ] && [ -x "$HOME/.venv-aap-docs/bin/python" ] && PY="$HOME/.venv-aap-docs/bin/python"
PY="${PY:-python3}"
if [ "$MODE" = browser ] && ! "$PY" -c "import playwright" 2>/dev/null; then
  cat >&2 <<EOF
Playwright is not installed for $PY. One-time setup:
  python3 -m venv ~/.venv-aap-docs
  ~/.venv-aap-docs/bin/pip install playwright
  ~/.venv-aap-docs/bin/playwright install chromium     # not needed if Google Chrome is installed
Or run with --curl.
EOF
  exit 2
fi

POD=(oc -n "$NS" exec -i deploy/migration-copilot -c copilot --)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/pages"

download() {
  MODE="$MODE" HEADLESS="$HEADLESS" "$PY" - "$1" "$WORK/pages" <<'PY'
import hashlib, json, os, pathlib, socket, subprocess, sys, time

urls, out = json.load(open(sys.argv[1])), pathlib.Path(sys.argv[2])
ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
by = "copy from a workstation"

def looks_valid(html: str) -> bool:
    low = html.lower()
    return "ansible automation platform" in low and "access denied" not in low[:2000]

def save(url: str, html: str) -> None:
    name = hashlib.sha256(url.encode()).hexdigest()[:24]
    (out / (name + ".html")).write_text(html, encoding="utf-8")
    (out / (name + ".json")).write_text(json.dumps({"url": url, "by": by,
                                                    "fetched": time.strftime("%Y-%m-%dT%H:%M:%S")}))

def with_curl() -> int:
    ok = 0
    for url in urls:
        r = subprocess.run(["curl", "-sSL", "--compressed", "--max-time", "60", "-A", ua,
                            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                            "-H", "Accept-Language: en-US,en;q=0.9", "-w", "\n%{http_code}", url],
                           capture_output=True, text=True)
        body, _, code = r.stdout.rpartition("\n")
        if code == "200" and looks_valid(body):
            save(url, body); ok += 1
            print(f"  OK   {url}")
        else:
            print(f"  FAIL {code or r.stderr.strip()} {url}")
        time.sleep(0.3)
    return ok

def with_browser() -> int:
    from playwright.sync_api import sync_playwright

    ok = 0
    headless = os.environ.get("HEADLESS") == "1"
    fixture = os.environ.get("COPY_RELEASE_NOTES_FIXTURE")       # tests only: {url: html}
    with sync_playwright() as p:
        browser = None
        for kw in ({"channel": "chrome"}, {}):                     # Google Chrome first, then Chromium
            try:
                browser = p.chromium.launch(headless=headless, **kw)
                print(f"  browser: {'Google Chrome' if kw else 'Chromium (Playwright)'}")
                break
            except Exception as exc:
                last = exc
        if browser is None:
            sys.exit(f"cannot start a browser: {last}. Run: ~/.venv-aap-docs/bin/playwright install chromium")
        page = browser.new_context(locale="en-US").new_page()
        if fixture:
            pages = json.load(open(fixture))
            page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html",
                                                           body=pages.get(route.request.url, pages.get("*")))
                       if route.request.url in pages or "*" in pages else route.fulfill(status=404, body="not found"))
        for url in urls:
            status, html = 0, ""
            for attempt in range(3):                                # the CDN may challenge first
                try:
                    resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(1500)
                    status = resp.status if resp else 0
                    # the HTML sent by the server (what AAP Odyssey would read), not the DOM
                    # rebuilt by the browser scripts; the DOM only as a fallback
                    html = resp.text() if resp and resp.ok else ""
                    if not looks_valid(html):
                        html = page.content()
                except Exception as exc:
                    status, html = str(exc).splitlines()[0][:80], ""
                if looks_valid(html):
                    break
                page.wait_for_timeout(3000 * (attempt + 1))
            if looks_valid(html):
                save(url, html); ok += 1
                print(f"  OK   {url}")
            else:
                print(f"  FAIL {status} {url}")
        browser.close()
    return ok

if not urls:
    print("  nothing to download")
    sys.exit(0)
ok = with_browser() if os.environ.get("MODE") == "browser" else with_curl()
if ok == 0:
    sys.exit("no page downloaded. Check that the page opens in your browser; with --curl try --browser.")
PY
}

push() {
  python3 -c '
import io, sys, tarfile
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w:gz") as t:
    t.add(sys.argv[1], arcname=".")
sys.stdout.buffer.write(buf.getvalue())' "$WORK/pages" | "${POD[@]}" python3 -c '
import os, sys, tarfile
d = os.path.join(os.environ.get("COPILOT_DATA_DIR", "/data"), "_cache", "releases")
os.makedirs(d, exist_ok=True)
with tarfile.open(fileobj=sys.stdin.buffer, mode="r|gz") as t:
    try:
        t.extractall(d, filter="data")
    except TypeError:
        t.extractall(d)
print("  copied into", d)
try:
    from migration_ai.config import Settings
    from migration_ai.storage import Store
    st = Settings().s3
    if st.configured:
        n = Store(st).upload_dir(__import__("pathlib").Path(d), "release-notes")
        print(f"  saved in the bucket: {n} files under release-notes/ (survives a restart of the pod)")
    else:
        print("  S3 not configured in AAP Odyssey pod: the copy stays in the pod until it restarts")
except Exception as exc:
    print(f"  WARNING: not saved in the bucket: {type(exc).__name__}: {exc}")'
}

echo "0. AAP Odyssey image"
if ! "${POD[@]}" python3 - <<'PY'
import sys
import migration_ai
from migration_ai import releases
v = getattr(migration_ai, "__version__", "?")
ok = hasattr(releases, "_index_releases") and hasattr(releases, "COMPONENT_VERSION_RE")
print(f"  migration_ai {v} in the pod: " + ("OK" if ok else "TOO OLD"))
if not ok:
    sys.exit("  The parser for the current docs.redhat.com layout is missing. Rebuild the image from the\n"
             "  latest package (version 0.3.2 or later): image/prepare-context.sh, oc start-build,\n"
             "  oc rollout restart deployment/migration-copilot deployment/aap-migration-console")
PY
then
  exit 1
fi

echo "1. Pages read by the Smart Planning agent"
"${POD[@]}" python3 -c 'import json; from migration_ai.releases import urls; print(json.dumps(sorted(urls().values())))' \
  > "$WORK/index.json"
cat "$WORK/index.json"; echo

echo "2. Index pages ($MODE)"
download "$WORK/index.json"
push

echo "3. Release pages linked by the indexes"
"${POD[@]}" python3 - > "$WORK/releases.json" <<'PY'
import json, os
os.environ["AAP_RELEASE_NOTES_SOURCE"] = "copy"      # read the copies only, no request to docs.redhat.com
from pathlib import Path
from migration_ai import releases
f = releases.Fetcher(Path(os.environ.get("COPILOT_DATA_DIR", "/data")) / "_cache" / "releases", ttl_hours=10**6)
out = []
for v in sorted(releases.urls()):
    try:
        out += [r.url for r in releases.list_releases(v, f) if r.url and not r.detailed]
    except Exception as exc:
        import sys
        print(f"AAP {v}: {type(exc).__name__}: {exc}", file=sys.stderr)
print(json.dumps(sorted(set(out))))
PY
N=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$WORK/releases.json")
echo "  $N release pages"
if [ "$N" = 0 ]; then
  mkdir -p release-notes-debug
  cp "$WORK"/pages/*.html "$WORK"/pages/*.json release-notes-debug/ 2>/dev/null || true
  echo "No release page found in the index pages (AAP 2.6/2.7 should list several)." >&2
  echo "The downloaded index pages are in ./release-notes-debug: send them to whoever maintains" >&2
  echo "this toolkit, so that the parser can be adapted to the page layout." >&2
  exit 1
fi
download "$WORK/releases.json"
push

echo "4. Result, as the agent sees it"
"${POD[@]}" python3 - <<'PY'
import os
os.environ["AAP_RELEASE_NOTES_SOURCE"] = "copy"
from pathlib import Path
from migration_ai import releases
f = releases.Fetcher(Path(os.environ.get("COPILOT_DATA_DIR", "/data")) / "_cache" / "releases", ttl_hours=10**6)
for v in sorted(releases.urls()):
    try:
        rels = releases.list_releases(v, f)
        print(f"  AAP {v}: {len(rels)} releases, latest {rels[0].date if rels else '-'}")
    except Exception as exc:
        print(f"  AAP {v}: not available ({type(exc).__name__})")
PY

echo "Done. Ask AAP Odyssey: Show the patch releases of the installed AAP"
