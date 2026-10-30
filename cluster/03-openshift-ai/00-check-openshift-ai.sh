#!/usr/bin/env bash
set -uo pipefail

ok() { printf '  [ OK ] %s\n' "$*"; }
ko() { printf '  [FIX ] %s\n' "$*"; FIX=1; }
FIX=0

echo "OpenShift:  $(oc get clusterversion version -o jsonpath='{.status.desired.version}')"
CSV=$(oc get csv -n redhat-ods-operator -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.phase}{"\n"}{end}' 2>/dev/null | grep rhods-operator)
[ -n "$CSV" ] && ok "OpenShift AI operator: $CSV" || ko "OpenShift AI operator not found in redhat-ods-operator"

DSC=$(oc get datasciencecluster -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -z "$DSC" ]; then
  ko "no DataScienceCluster: apply cluster/03-openshift-ai/01-datasciencecluster.yaml"
else
  echo "DataScienceCluster: $DSC (phase $(oc get datasciencecluster "$DSC" -o jsonpath='{.status.phase}'))"
  for c in dashboard kserve aipipelines workbenches; do
    s=$(oc get datasciencecluster "$DSC" -o jsonpath="{.spec.components.$c.managementState}")
    if [ "$s" = "Managed" ]; then ok "$c: Managed"
    elif [ "$c" = "workbenches" ]; then printf '  [ -- ] %s: %s (optional)\n' "$c" "${s:-unset}"
    else ko "$c: ${s:-unset}  ->  oc patch datasciencecluster $DSC --type merge -p '{\"spec\":{\"components\":{\"$c\":{\"managementState\":\"Managed\"}}}}'"
    fi
  done
  s=$(oc get datasciencecluster "$DSC" -o jsonpath='{.spec.components.aipipelines.argoWorkflowsControllers.managementState}')
  [ "$s" = "Removed" ] && echo "  [INFO] aipipelines uses an external Argo Workflows (argoWorkflowsControllers Removed)"
fi

oc get csv -n cert-manager-operator --no-headers 2>/dev/null | grep -c Succeeded >/dev/null \
  && ok "cert-manager Operator installed" \
  || ko "cert-manager Operator missing (needed by kserve): oc apply -f cluster/01-operators/03-cert-manager-operator.yaml"

oc get templates -n redhat-ods-applications -o name 2>/dev/null | grep -c vllm-cuda-runtime-template >/dev/null \
  && ok "vLLM NVIDIA GPU runtime template available" \
  || ko "template vllm-cuda-runtime-template not found: oc get templates -n redhat-ods-applications | grep -i vllm"

[ "$FIX" = 0 ] && echo "OpenShift AI is ready for the demo." || echo "Apply the [FIX] commands above, then run this check again."
