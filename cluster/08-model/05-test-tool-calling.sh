#!/usr/bin/env bash
set -euo pipefail
NS=aap-migration-ai
ISVC="${1:-qwen3-8b}"
SVC="${ISVC}-predictor"

echo "Waiting for InferenceService $ISVC to be Ready (up to 20 minutes)..."
if ! oc -n "$NS" wait "isvc/$ISVC" --for=condition=Ready --timeout=20m; then
  echo "Not Ready. Check: oc -n $NS get pods -l serving.kserve.io/inferenceservice=$ISVC" >&2
  echo "               oc -n $NS logs deploy/${ISVC}-predictor -c kserve-container --tail=40" >&2
  exit 1
fi

CIP=$(oc -n "$NS" get svc "$SVC" -o jsonpath='{.spec.clusterIP}')
PORT=$(oc -n "$NS" get svc "$SVC" -o jsonpath='{.spec.ports[0].port}')
TPORT=$(oc -n "$NS" get svc "$SVC" -o jsonpath='{.spec.ports[0].targetPort}')
if [ "$CIP" = "None" ]; then
  case "$TPORT" in ''|*[!0-9]*) TPORT=8080 ;; esac
  URL="http://${SVC}.${NS}.svc.cluster.local:${TPORT}"
  echo "Service $SVC is headless: use the container port $TPORT"
else
  URL="http://${SVC}.${NS}.svc.cluster.local"; [ "$PORT" = "80" ] || URL="${URL}:${PORT}"
fi
echo "LLM_BASE_URL for the ConfigMap (step 6.2): ${URL}/v1"

echo -n "vLLM "
oc -n "$NS" exec "deploy/${ISVC}-predictor" -c kserve-container -- \
  python3 -c 'import vllm; print(vllm.__version__)' 2>/dev/null || echo "(version not readable)"

oc -n "$NS" exec -i "deploy/${ISVC}-predictor" -c kserve-container -- \
  python3 - "$URL" "$ISVC" <<'EOF'
import json, sys, urllib.request, urllib.error

url, model = sys.argv[1], sys.argv[2]

def call(path, body=None):
    req = urllib.request.Request(url + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code} from {url}{path}: {e.read().decode()[:800]}")
    except Exception as e:
        sys.exit(f"cannot reach {url}{path}: {type(e).__name__}: {e}")

models = [m["id"] for m in call("/v1/models")["data"]]
print("served models:", models)
if model not in models:
    sys.exit(f"model name '{model}' not served (LLM_MODEL must be one of {models})")

body = {"model": model, "temperature": 0,
        "messages": [{"role": "user", "content": "Which patch releases exist for Ansible Automation Platform 2.6? "
                                                 "Use the tool."}],
        "tools": [{"type": "function", "function": {
            "name": "get_patch_releases", "description": "List the patch releases of an AAP version",
            "parameters": {"type": "object", "properties": {"version": {"type": "string", "enum": ["2.5", "2.6", "2.7"]}},
                           "required": ["version"]}}}],
        "tool_choice": "auto"}
if model.startswith("qwen3"):
    body["chat_template_kwargs"] = {"enable_thinking": False}      # same as LLM_EXTRA_BODY for Qwen3
msg = call("/v1/chat/completions", body)["choices"][0]["message"]
calls = msg.get("tool_calls") or []
if calls and calls[0]["function"]["name"] == "get_patch_releases":
    print("TOOL CALLING OK:", calls[0]["function"]["name"], calls[0]["function"]["arguments"])
else:
    print("answer without tool call:", (msg.get("content") or "")[:500])
    sys.exit("NO TOOL CALL: check --enable-auto-tool-choice and --tool-call-parser in the InferenceService "
             "(qwen3: hermes; granite: hermes or granite4; gpt-oss: openai)")
EOF
