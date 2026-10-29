# RCA: NodeNotReady on platform-worker2

**Report:** rca-platform-worker2-20261029-064720 | **Priority:** P1 | **Alert severity:** page | **Analysis mode:** rule_based

## Priority and where the problem is
**P1**: respond now: page the on-call and open an incident. Still ongoing.
- alert severity is 'page'
- `platform-worker2` or something on this path serves users
- it is effectively down: the node under user pods is gone
- it is still ongoing

**Infrastructure level** (high confidence): node platform-worker2 of the platform cluster
- kube_node_status_condition Ready is false
- this is below the workload layer: every pod on the node is affected

## What happened
Node `platform-worker2` in the platform cluster stopped reporting Ready, so its pods are not served and will be evicted after the grace period (8 pods run there, including frontend-58cf76ff89-cdlsh, loadgen-58d9846574-hkhsm). The kubelet is down, the node lost its network, or (in kind) the node container stopped.

## Root cause (high confidence)
Category: `node_failure`

Node platform-worker2 is NotReady.

## Impact
8 pods run there, including frontend-58cf76ff89-cdlsh, loadgen-58d9846574-hkhsm
SLO: no SLO defined for this signal.

## How to fix it now
- [ ] kubectl --context kind-platform describe node platform-worker2 | grep -A10 Conditions
- [ ] kind: docker ps -a --filter name=platform-worker2  # then docker start platform-worker2
- [ ] Cloud: cordon and drain, then replace the instance

## How to prevent it
- [ ] Run at least 2 replicas spread over nodes (topologySpreadConstraints), and use node auto-repair in the cloud

## How to confirm it is fixed
- kubectl --context kind-platform get nodes  # platform-worker2 Ready
- kubectl --context kind-platform get pods -A -o wide | grep platform-worker2

## Timeline
- 2026-10-29T06:39:14Z: NodeNotReady: platform-worker2 Node platform-worker2 status is now: NodeNotReady
- 2026-10-29T06:47:20Z: alert NodeNotReady started firing
- 2026-10-29T06:48:09Z: investigation completed

## Contributing factors
- none identified

## Detection
The alert `NodeNotReady` fired in Grafana alerting and was sent by webhook to the SRE agent, which started this investigation as an Argo Workflow automatically.

## Observability gaps (what to add so the next one is faster)
- The telemetry available was enough for this investigation.

## Evidence
- **mimir**: node(s) platform-worker2: NotReady, no pressure, CPU 0% and memory 5% of allocatable
  - `kube_node_status_condition / node usage`
- **mimir**: 8 pods run on the node; biggest memory users: demo/loadgen-58d9846574-hkhsm (30 MiB), ingress-nginx/ingress-nginx-controller-677d99b6c-4lkrn (60 MiB), kube-system/calico-node-xgsbb (163 MiB), kube-system/kube-proxy-ghwfk (30 MiB), observability/alloy-6769f66d54-5qkpw (402 MiB)
  - `pods on platform-worker2`

_This RCA is blameless and was generated automatically. Review it before acting on it._
