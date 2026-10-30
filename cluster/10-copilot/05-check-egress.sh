#!/usr/bin/env bash
set -euo pipefail
NS=aap-migration-ai
oc -n "$NS" exec -i deploy/migration-copilot -c copilot -- python3 - <<'EOF'
import json, pathlib, httpx
try:
    from migration_ai.releases import http_headers
except ImportError:              # image older than 0.3.1: same headers as the agent
    def http_headers():
        return {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/130.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9"}

env = {}
p = pathlib.Path("/etc/aap-bridge/.env")
if p.is_file():
    for line in p.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")

def check(label, url, verify=True, headers=None):
    if not url:
        print(f"SKIP {label}: URL not set in aap-bridge-env")
        return
    try:
        r = httpx.get(url, timeout=20, verify=verify, follow_redirects=True, headers=headers)
        extra = ""
        if "ping" in url and r.headers.get("content-type", "").startswith("application/json"):
            extra = " version " + str(r.json().get("version"))
        state = "OK" if r.status_code < 400 else "FAIL"
        print(f"{state} {r.status_code} {label}: {url}{extra}")
    except Exception as exc:
        print(f"FAIL {label}: {url} -> {type(exc).__name__}: {exc}")

check("Red Hat docs", "https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.6/whats_new-async_updates",
      headers=http_headers())
src = env.get("SOURCE__URL", "").rstrip("/")
tgt = env.get("TARGET__URL", "").rstrip("/")
src_root = src.split("/api/")[0] if src else ""
check("AAP 2.4 (source)", src_root and src_root + "/api/v2/ping/", env.get("SOURCE__VERIFY_SSL", "true").lower() != "false")
tgt_root = tgt.split("/api/")[0] if tgt else ""
check("target AAP", tgt_root and tgt_root + "/api/controller/v2/ping/", env.get("TARGET__VERIFY_SSL", "true").lower() != "false")
EOF
