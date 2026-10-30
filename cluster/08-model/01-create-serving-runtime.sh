#!/usr/bin/env bash
set -euo pipefail
NS=aap-migration-ai
TEMPLATE=vllm-cuda-runtime-template
echo "vLLM templates available in this cluster:"
oc get templates -n redhat-ods-applications -o custom-columns=NAME:.metadata.name,DISPLAY:".metadata.annotations.openshift\.io/display-name" | grep -i vllm || true
oc get template "$TEMPLATE" -n redhat-ods-applications >/dev/null || {
  echo "Template $TEMPLATE not found: pick the NVIDIA GPU one from the list above and edit this script." >&2; exit 1; }
oc process -n redhat-ods-applications "$TEMPLATE" | oc apply -n "$NS" -f -
oc get servingruntimes -n "$NS"
echo "vLLM image of the runtime:"
oc get servingruntime vllm-cuda-runtime -n "$NS" -o jsonpath='{.spec.containers[0].image}{"\n"}'
