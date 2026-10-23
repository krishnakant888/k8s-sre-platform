#!/usr/bin/env bash
# Failure scenarios for the frontend and for the cluster itself. Complements scripts/drill.sh,
# which injects faults into orders-api. See docs/SCENARIOS.md for what each one should trigger.
#
#   scripts/scenarios.sh list
#   scripts/scenarios.sh <healthy|error500|error503|notfound|mixed> [requests]   traffic against the frontend
#   scripts/scenarios.sh <crashloop|oom|bad-image|pending|bad-probe>             broken workloads in ns "scenarios"
#   scripts/scenarios.sh status | cleanup
set -euo pipefail

CTX=${KUBE_CONTEXT:-kind-platform}
URL=${FRONTEND_URL:-http://frontend.localhost:8080}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
N=${2:-60}

k() { kubectl --context "$CTX" "$@"; }

load() {
  local path=$1 count=$2
  echo "GET ${URL}${path} x${count}"
  for _ in $(seq 1 "$count"); do
    curl -s -o /dev/null -w '%{http_code}\n' "${URL}${path}" || echo 000
  done | sort | uniq -c
}

apply() {
  k get namespace scenarios >/dev/null 2>&1 || k create namespace scenarios
  k apply -f "${ROOT}/scenarios/$1.yaml"
}

case "${1:-list}" in
  list) sed -n '2,8p' "$0" ;;
  healthy)  load / "$N" ;;
  error500) load /_scenarios/error500 "$N" ;;
  error503) load /_scenarios/error503 "$N" ;;
  notfound) load /_scenarios/notfound "$N" ;;
  mixed)    load / "$N"; load /_scenarios/notfound "$((N / 3))"; load /_scenarios/error500 "$((N / 3))" ;;
  crashloop|oom|bad-image|pending|bad-probe) apply "$1" ;;
  status)
    k -n scenarios get pods 2>/dev/null || true
    k -n demo get pods -l app.kubernetes.io/name=frontend ;;
  cleanup) k delete namespace scenarios --ignore-not-found ;;
  *) sed -n '2,8p' "$0"; exit 1 ;;
esac
