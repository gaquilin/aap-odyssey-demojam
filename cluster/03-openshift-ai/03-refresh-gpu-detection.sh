#!/usr/bin/env bash
set -euo pipefail
NS=redhat-ods-applications
oc -n "$NS" delete configmap migration-gpu-status --ignore-not-found
oc -n "$NS" rollout restart deployment/rhods-dashboard
oc -n "$NS" rollout status deployment/rhods-dashboard --timeout=10m
