# RCA: DeploymentReplicasUnavailable on fault-unschedulable

**Report:** rca-fault-unschedulable-20261029-070650 | **Priority:** P3 | **Alert severity:** ticket | **Analysis mode:** rule_based

## Priority and where the problem is
**P3**: respond next business day. Still ongoing.
- alert severity is 'ticket'
- no direct user traffic depends on it
- a platform component with no direct user impact is capped at P3
- it is still ongoing

**Infrastructure level** (high confidence): capacity of the platform cluster (memory)
- FailedScheduling event: 0/3 nodes are available: 1 node(s) had untolerated taint {node-role.kubernetes.io/control-plane: }, 2 Insufficient memory. preemption: 0/3 nodes are available: 
- the problem is cluster capacity, not the workload's code

## What happened
The scheduler cannot place a pod of `sandbox/fault-unschedulable` in the platform cluster because no node has enough free memory. The pod stays Pending, so the workload runs with fewer replicas than wanted. Node usage at the time: CPU 0%, memory 0% of allocatable.

## Root cause (high confidence)
Category: `scheduling_failure`

No node has enough memory for the pod: 0/3 nodes are available: 1 node(s) had untolerated taint {node-role.kubernetes.io/control-plane: }, 2 Insufficient memory. preemption: 0/3 nodes are available: .

## Impact
fault-unschedulable is below its desired replicas until capacity is added.
SLO: no SLO defined for this signal.

## How to fix it now
- [ ] kubectl --context kind-platform describe nodes | grep -A8 'Allocated resources'  # see what is requested on each node
- [ ] Free capacity: scale down or delete unneeded workloads, or add a node (kind: add a worker to bootstrap/kind-config*.yaml and recreate; cloud: scale the node pool)

## How to prevent it
- [ ] Right-size requests of `fault-unschedulable` in the manifest of `fault-unschedulable` in Git: requests are what the scheduler reserves
- [ ] Add a Kyverno/LimitRange rule that caps requests, and alert on `kube_node_status_allocatable` vs requested at 85%

## How to confirm it is fixed
- kubectl --context kind-platform -n sandbox get pods -l app.kubernetes.io/name=fault-unschedulable  # all Running

## Timeline
- 2026-10-29T06:02:48Z: FailedScheduling: fault-unschedulable-789f767f8d-cnl7p skip schedule deleting pod: sandbox/fault-unschedulable-789f767f8d-cnl7p
- 2026-10-29T07:03:30.355816Z: FailedScheduling: fault-unschedulable-789f767f8d-kbbjk 0/3 nodes are available: 1 node(s) had untolerated taint {node-role.kubernetes.i
- 2026-10-29T07:03:30Z: SuccessfulCreate: fault-unschedulable-789f767f8d Created pod: fault-unschedulable-789f767f8d-kbbjk
- 2026-10-29T07:03:30Z: ScalingReplicaSet: fault-unschedulable Scaled up replica set fault-unschedulable-789f767f8d to 1
- 2026-10-29T07:06:50Z: alert DeploymentReplicasUnavailable started firing
- 2026-10-29T07:07:38Z: investigation completed

## Contributing factors
- none identified

## Detection
The alert `DeploymentReplicasUnavailable` fired in Grafana alerting and was sent by webhook to the SRE agent, which started this investigation as an Argo Workflow automatically.

## Observability gaps (what to add so the next one is faster)
- `fault-unschedulable` has no Argo CD application named prod-fault-unschedulable or fault-unschedulable: label deploys so the report can name the change and the file

## Evidence
- **kubernetes**: 1 pods; fault-unschedulable-789f767f8d-kbbjk: Pending not ready, restarts 0, waiting Unschedulable
  - `pods sandbox/fault-unschedulable* in platform`
- **kubernetes**: SuccessfulCreate fault-unschedulable-789f767f8d: Created pod: fault-unschedulable-789f767f8d-kbbjk; ScalingReplicaSet fault-unschedulable: Scaled up replica set fault-unschedulable-789f767f8d to 1; FailedScheduling fault-unschedulable-789f767f8d-kbbjk: 0/3 nodes are available: 1 node(s) had untolerated taint {node-role.kubernetes.io/control-plane: }, 2 Insuffic; FailedScheduling fault-unschedulable-789f767f8d-cnl7p: skip schedule deleting pod: sandbox/fault-unschedulable-789f767f8d-cnl7p
  - `events sandbox in platform`
- **mimir**: restarts in 45m: 0; CPU throttled up to 0%
  - `cluster="platform", namespace="sandbox", pod=~"fault-unschedulable(-[a-z0-9]{8,10})?-[a-z0-9]{5}|fault-unschedulable-[0-9]+"`
- **loki**: last log lines of the workload: no error lines in the window
  - `{cluster="platform", namespace="sandbox", pod=~"fault-unschedulable(-[a-z0-9]{8,10})?-[a-z0-9]{5}|fault-unschedulable-[0-9]+"}`
- **argocd**: no Argo CD application of that name could be read
  - `history prod-fault-unschedulable`
- **tempo**: 0 error traces in the window
  - `{ resource.service.name = "fault-unschedulable" && status = error }`

_This RCA is blameless and was generated automatically. Review it before acting on it._
