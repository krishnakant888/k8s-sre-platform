# RCA: DeploymentReplicasUnavailable on fault-nodeselector

**Report:** rca-fault-nodeselector-20261029-071250 | **Priority:** P3 | **Alert severity:** ticket | **Analysis mode:** rule_based

## Priority and where the problem is
**P3**: respond next business day. Still ongoing.
- alert severity is 'ticket'
- no direct user traffic depends on it
- a platform component with no direct user impact is capped at P3
- it is still ongoing

**Application level** (high confidence): scheduling constraints of fault-nodeselector
- FailedScheduling event names a taint, affinity or selector
- the workload spec asks for something no node offers

## What happened
The pod of `sandbox/fault-nodeselector` in the platform cluster cannot be placed because its scheduling constraints (nodeSelector, affinity or tolerations) match no node, so it stays Pending.

## Root cause (high confidence)
Category: `scheduling_failure`

The pod's node selector, affinity or tolerations match no node: 0/3 nodes are available: 1 node(s) had untolerated taint {node-role.kubernetes.io/control-plane: }, 2 node(s) didn't match Pod's node affinity/selector. preempt.

## Impact
fault-nodeselector cannot start.
SLO: no SLO defined for this signal.

## How to fix it now
- [ ] kubectl --context kind-platform -n sandbox describe pod fault-nodeselector-78958d6d86-xhnxz | grep -A5 Events

## How to prevent it
- [ ] Fix the constraint in the manifest of `fault-nodeselector` in Git, or label/taint the nodes to match

## How to confirm it is fixed
- kubectl --context kind-platform -n sandbox get pods  # Running

## Timeline
- 2026-10-29T06:09:07Z: FailedScheduling: fault-nodeselector-78958d6d86-s6rqt skip schedule deleting pod: sandbox/fault-nodeselector-78958d6d86-s6rqt
- 2026-10-29T07:08:49.629860Z: FailedScheduling: fault-nodeselector-78958d6d86-xhnxz 0/3 nodes are available: 1 node(s) had untolerated taint {node-role.kubernetes.i
- 2026-10-29T07:08:49Z: SuccessfulCreate: fault-nodeselector-78958d6d86 Created pod: fault-nodeselector-78958d6d86-xhnxz
- 2026-10-29T07:08:49Z: ScalingReplicaSet: fault-nodeselector Scaled up replica set fault-nodeselector-78958d6d86 to 1
- 2026-10-29T07:12:50Z: alert DeploymentReplicasUnavailable started firing
- 2026-10-29T07:13:42Z: investigation completed

## Contributing factors
- none identified

## Detection
The alert `DeploymentReplicasUnavailable` fired in Grafana alerting and was sent by webhook to the SRE agent, which started this investigation as an Argo Workflow automatically.

## Observability gaps (what to add so the next one is faster)
- `fault-nodeselector` has no Argo CD application named prod-fault-nodeselector or fault-nodeselector: label deploys so the report can name the change and the file

## Evidence
- **kubernetes**: 1 pods; fault-nodeselector-78958d6d86-xhnxz: Pending not ready, restarts 0, waiting Unschedulable
  - `pods sandbox/fault-nodeselector* in platform`
- **kubernetes**: SuccessfulCreate fault-nodeselector-78958d6d86: Created pod: fault-nodeselector-78958d6d86-xhnxz; ScalingReplicaSet fault-nodeselector: Scaled up replica set fault-nodeselector-78958d6d86 to 1; FailedScheduling fault-nodeselector-78958d6d86-xhnxz: 0/3 nodes are available: 1 node(s) had untolerated taint {node-role.kubernetes.io/control-plane: }, 2 node(s) ; FailedScheduling fault-nodeselector-78958d6d86-s6rqt: skip schedule deleting pod: sandbox/fault-nodeselector-78958d6d86-s6rqt
  - `events sandbox in platform`
- **mimir**: restarts in 45m: 0; CPU throttled up to 0%
  - `cluster="platform", namespace="sandbox", pod=~"fault-nodeselector(-[a-z0-9]{8,10})?-[a-z0-9]{5}|fault-nodeselector-[0-9]+"`
- **loki**: last log lines of the workload: no error lines in the window
  - `{cluster="platform", namespace="sandbox", pod=~"fault-nodeselector(-[a-z0-9]{8,10})?-[a-z0-9]{5}|fault-nodeselector-[0-9]+"}`
- **argocd**: no Argo CD application of that name could be read
  - `history prod-fault-nodeselector`
- **tempo**: 0 error traces in the window
  - `{ resource.service.name = "fault-nodeselector" && status = error }`

_This RCA is blameless and was generated automatically. Review it before acting on it._
