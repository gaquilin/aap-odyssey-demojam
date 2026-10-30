#!/usr/bin/env bash
set -euo pipefail

NS="${NS:-aap-migration-ai}"
OUT="$(cd "$(dirname "$0")" && pwd)/generated"
SERVICE_CA=/run/secrets/kubernetes.io/serviceaccount/service-ca.crt
mkdir -p "$OUT"

for obc in obc-aap-migration-data obc-pipeline-artifacts; do
  phase=$(oc -n "$NS" get obc "$obc" -o jsonpath='{.status.phase}' 2>/dev/null || true)
  if [ "$phase" != "Bound" ]; then
    echo "ObjectBucketClaim $obc is not Bound (phase: ${phase:-missing}). Apply 01/02 and wait." >&2
    exit 1
  fi
done

cm()  { oc -n "$NS" get configmap "$1" -o jsonpath="{.data.$2}"; }
sec() { oc -n "$NS" get secret "$1" -o jsonpath="{.data.$2}" | base64 -d; }

HOST=$(cm obc-aap-migration-data BUCKET_HOST)
OBC_PORT=$(cm obc-aap-migration-data BUCKET_PORT)
PORT="${PORT:-$OBC_PORT}"
if [ -z "${SCHEME:-}" ]; then
  if [ "$PORT" = "443" ]; then SCHEME=https; else SCHEME=http; fi
fi
ENDPOINT="$SCHEME://$HOST:$PORT"
REGION="${REGION:-$(cm obc-aap-migration-data BUCKET_REGION)}"
REGION="${REGION:-us-east-1}"

connection() {
  local bucket; bucket=$(cm "$3" BUCKET_NAME)
  cat > "$OUT/connection-$1.yaml" <<EOF
# Generated from ObjectBucketClaim $3 - contains credentials: do not commit to Git.
# $4
apiVersion: v1
kind: Secret
metadata:
  name: $1
  namespace: $NS
  labels:
    opendatahub.io/dashboard: "true"
    opendatahub.io/managed: "true"
  annotations:
    opendatahub.io/connection-type-protocol: s3
    opendatahub.io/connection-type: s3
    openshift.io/display-name: $2
type: Opaque
stringData:
  AWS_ACCESS_KEY_ID: "$(sec "$3" AWS_ACCESS_KEY_ID)"
  AWS_SECRET_ACCESS_KEY: "$(sec "$3" AWS_SECRET_ACCESS_KEY)"
  AWS_S3_ENDPOINT: "$ENDPOINT"
  AWS_S3_BUCKET: "$bucket"
  AWS_DEFAULT_REGION: "$REGION"
EOF
}
connection aap-migration-data "AAP migration data" obc-aap-migration-data \
  "Data source of the AI: toolkit folders of every migration, datasets, models, CasC."
connection pipeline-artifacts "Pipeline artifacts" obc-pipeline-artifacts \
  "Bucket of the pipeline server (pipeline definitions and run artifacts)."

ARTIFACTS_BUCKET=$(cm obc-pipeline-artifacts BUCKET_NAME)
CA_BLOCK=""
if [ "$SCHEME" = "https" ]; then
  CA_BLOCK="    cABundle:                        # trust the OpenShift service CA (ODF internal S3 certificate)
      configMapName: openshift-service-ca.crt
      configMapKey: service-ca.crt"
fi
cat > "$OUT/dspa.yaml" <<EOF
# Generated for ODF - replaces cluster/05-pipeline-server/01-dspa.yaml (step 5.1).
apiVersion: datasciencepipelinesapplications.opendatahub.io/v1
kind: DataSciencePipelinesApplication
metadata:
  name: dspa
  namespace: $NS
spec:
  dspVersion: v2
  apiServer:
    enableSamplePipeline: false
    enableRoute: true
    cacheEnabled: false
$CA_BLOCK
  objectStorage:
    externalStorage:
      host: $HOST
      port: "$PORT"
      scheme: $SCHEME
      region: $REGION
      bucket: $ARTIFACTS_BUCKET
      s3CredentialsSecret:
        secretName: pipeline-artifacts
        accessKey: AWS_ACCESS_KEY_ID
        secretKey: AWS_SECRET_ACCESS_KEY
EOF

echo "Endpoint:          $ENDPOINT"
echo "Data bucket:       $(cm obc-aap-migration-data BUCKET_NAME)"
echo "Artifacts bucket:  $ARTIFACTS_BUCKET"
echo "Files written in:  $OUT"
echo
if [ "$SCHEME" = "https" ]; then
  echo "In cluster/06-config/02-configmap-migration-ai-config.yaml (step 6.2) set:"
  echo "  AWS_S3_VERIFY_TLS: \"$SERVICE_CA\""
else
  echo "In cluster/06-config/02-configmap-migration-ai-config.yaml (step 6.2) keep:"
  echo "  AWS_S3_VERIFY_TLS: \"true\"      (ignored with http)"
fi
