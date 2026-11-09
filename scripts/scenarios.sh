#!/usr/bin/env bash
# Failure scenarios for the cluster. Complements scripts/drill.sh,
# which injects faults into orders-api. See docs/SCENARIOS.md for what each one should trigger.
#
#   scripts/scenarios.sh list
#   scripts/scenarios.sh <crashloop|oom|bad-image|pending|bad-probe>             broken workloads in ns "scenarios"
#   scripts/scenarios.sh status | cleanup
set -euo pipefail

CTX=${KUBE_CONTEXT:-kind-platform}
ROOT=$(cd "$(dirname "$0")/.." && pwd)

k() { kubectl --context "$CTX" "$@"; }

apply() {
  k get namespace scenarios >/dev/null 2>&1 || k create namespace scenarios
  k apply -f "${ROOT}/scenarios/$1.yaml"
}

case "${1:-list}" in
  list) sed -n '2,7p' "$0" ;;
  crashloop|oom|bad-image|pending|bad-probe) apply "$1" ;;
  status)
    k -n scenarios get pods 2>/dev/null || true ;;
  cleanup) k delete namespace scenarios --ignore-not-found ;;
  *) sed -n '2,7p' "$0"; exit 1 ;;
esac
