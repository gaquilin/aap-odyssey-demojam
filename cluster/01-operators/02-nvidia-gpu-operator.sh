#!/usr/bin/env bash
set -euo pipefail

oc apply -f - <<'EOF'
apiVersion: v1
kind: Namespace
metadata:
  name: nvidia-gpu-operator
---
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: nvidia-gpu-operator-group
  namespace: nvidia-gpu-operator
spec:
  targetNamespaces:
    - nvidia-gpu-operator
EOF

CHANNEL=$(oc get packagemanifest gpu-operator-certified -n openshift-marketplace -o jsonpath='{.status.defaultChannel}')
STARTING_CSV=$(oc get packagemanifests/gpu-operator-certified -n openshift-marketplace -o json \
  | jq -r --arg c "$CHANNEL" '.status.channels[] | select(.name == $c) | .currentCSV')
echo "NVIDIA GPU Operator: channel=$CHANNEL csv=$STARTING_CSV"

oc apply -f - <<EOF
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: gpu-operator-certified
  namespace: nvidia-gpu-operator
spec:
  channel: "${CHANNEL}"
  installPlanApproval: Manual
  name: gpu-operator-certified
  source: certified-operators
  sourceNamespace: openshift-marketplace
  startingCSV: "${STARTING_CSV}"
EOF

echo "Waiting for the InstallPlan..."
for _ in $(seq 1 60); do
  IP=$(oc get installplan -n nvidia-gpu-operator -o name 2>/dev/null | head -1)
  [ -n "$IP" ] && break
  sleep 5
done
[ -n "${IP:-}" ] || { echo "No InstallPlan found: check 'oc get sub -n nvidia-gpu-operator -o yaml'" >&2; exit 1; }
oc patch "$IP" -n nvidia-gpu-operator --type merge --patch '{"spec":{"approved":true}}'
echo "Approved $IP. Wait for: oc get csv -n nvidia-gpu-operator  -> Succeeded"
