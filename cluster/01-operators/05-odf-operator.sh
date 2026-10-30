#!/usr/bin/env bash
set -euo pipefail

if oc get storagecluster -A --no-headers 2>/dev/null | grep -c . >/dev/null; then
  echo "A StorageCluster already exists: ODF is installed, nothing to do."
  oc get storagecluster -A
  exit 0
fi

CHANNEL=$(oc get packagemanifest odf-operator -n openshift-marketplace -o jsonpath='{.status.defaultChannel}')
echo "ODF operator: channel $CHANNEL (default of the redhat-operators catalog)"

oc apply -f - <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: openshift-storage
  labels:
    openshift.io/cluster-monitoring: "true"
---
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: openshift-storage-operatorgroup
  namespace: openshift-storage
spec:
  targetNamespaces:
    - openshift-storage
---
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: odf-operator
  namespace: openshift-storage
spec:
  channel: "${CHANNEL}"
  installPlanApproval: Automatic
  name: odf-operator
  source: redhat-operators
  sourceNamespace: openshift-marketplace
EOF

echo "Waiting for the operator (up to 10 minutes)..."
for _ in $(seq 1 60); do
  phase=$(oc get csv -n openshift-storage -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.phase}{"\n"}{end}' \
          | awk '/^odf-operator/ {print $2}')
  [ "$phase" = "Succeeded" ] && break
  sleep 10
done
oc get csv -n openshift-storage

if ! oc get console.operator cluster -o jsonpath='{.spec.plugins}' | grep -c odf-console >/dev/null; then
  oc patch console.operator cluster --type json -p '[{"op":"add","path":"/spec/plugins/-","value":"odf-console"}]' \
    || oc patch console.operator cluster --type merge -p '{"spec":{"plugins":["odf-console"]}}'
fi
echo "Done. Now create the StorageSystem from the console (guide, step 1.6)."
