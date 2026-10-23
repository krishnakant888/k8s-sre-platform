#!/usr/bin/env bash
# Failure drills for the demo. Each one trips an alert, which starts an Argo Workflow investigation and an RCA report.
#
#   scripts/drill.sh latency   slow the inventory call by 1.5s (MS=1500, SECONDS_ON=900)
#   scripts/drill.sh errors    make 60% of inventory calls fail (RATE=0.6, SECONDS_ON=900)
#   scripts/drill.sh oom       allocate memory until the containers are OOMKilled
#   scripts/drill.sh kill      delete the orders-api pods
#   scripts/drill.sh reset     clear injected faults
#
# Faults live inside each pod, so the call is repeated to reach both replicas through the ingress.
set -euo pipefail

CTX=${KUBE_CONTEXT:-kind-platform}
URL=${ORDERS_URL:-http://orders.localhost:8080}

token() { kubectl --context "$CTX" -n demo get secret orders-api-chaos -o jsonpath='{.data.CHAOS_TOKEN}' | base64 -d; }

call() {
  local path=$1 tok
  tok=$(token)
  for _ in $(seq 1 12); do
    curl -fsS -o /dev/null -X POST -H "x-chaos-token: ${tok}" "${URL}${path}" || true
  done
}

case "${1:-}" in
  latency) call "/chaos/latency?ms=${MS:-1500}&seconds=${SECONDS_ON:-900}"; echo "latency injected for ${SECONDS_ON:-900}s" ;;
  errors)  call "/chaos/errors?rate=${RATE:-0.6}&seconds=${SECONDS_ON:-900}"; echo "errors injected for ${SECONDS_ON:-900}s" ;;
  oom)     call "/chaos/oom"; echo "memory hog started, containers will be OOMKilled" ;;
  kill)    kubectl --context "$CTX" -n demo delete pod -l app.kubernetes.io/name=orders-api ;;
  reset)   call "/chaos/reset"; echo "faults cleared" ;;
  *) sed -n '2,9p' "$0"; exit 1 ;;
esac

echo "watch: http://grafana.localhost:8080/d/orders-api   reports: kubectl -n sre-agent get cm -l sre-agent/report=true"
