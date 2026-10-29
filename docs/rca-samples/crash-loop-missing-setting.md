# RCA: PodCrashLooping on fault-crash

**Report:** rca-fault-crash-20261029-055450 | **Priority:** P3 | **Alert severity:** ticket | **Analysis mode:** rule_based

## Priority and where the problem is
**P3**: respond next business day. Still ongoing.
- alert severity is 'ticket'
- no direct user traffic depends on it
- a platform component with no direct user impact is capped at P3
- it is still ongoing

**Application level** (medium confidence): process of fault-crash exits with code 1
- exit code 1
- node and resource signals are normal

## What happened
`sandbox/fault-crash` in the platform cluster is in a restart loop. The last exit was Error with code 1 and the most frequent error line is `KeyError: DATABASE_URL is not set
`. Nothing in the cluster explains it (nodes are healthy, memory is not at the limit), so the cause is inside the process.

## Root cause (medium confidence)
Category: `pod_crash`

The container keeps exiting (Error, code 1): KeyError: DATABASE_URL is not set
.

## Impact
fault-crash has fewer ready replicas than desired.
SLO: no SLO defined for this signal.

## How to fix it now
- [ ] kubectl --context kind-platform -n sandbox logs fault-crash-b48f849d-s9lpx --previous | tail -50
- [ ] Check the recent change of the manifest of `fault-crash` in Git

## How to prevent it
- [ ] Add the failing path to CI (a smoke test that starts the container with the production settings)

## How to confirm it is fixed
- kubectl --context kind-platform -n sandbox get pods  # Running and restarts stable for 10 minutes

## Timeline
- 2026-10-29T05:54:06Z: Pulled: fault-crash-b48f849d-s9lpx Container image "busybox:1.37.0" already present on machine
- 2026-10-29T05:54:06Z: Created: fault-crash-b48f849d-s9lpx Created container app
- 2026-10-29T05:54:06Z: Started: fault-crash-b48f849d-s9lpx Started container app
- 2026-10-29T05:54:18Z: BackOff: fault-crash-b48f849d-s9lpx Back-off restarting failed container app in pod fault-crash-b48f849d-s9lpx_sandb
- 2026-10-29T05:54:50Z: alert PodCrashLooping started firing
- 2026-10-29T05:55:40Z: investigation completed

## Contributing factors
- none identified

## Detection
The alert `PodCrashLooping` fired in Grafana alerting and was sent by webhook to the SRE agent, which started this investigation as an Argo Workflow automatically.

## Observability gaps (what to add so the next one is faster)
- Log lines of `fault-crash` have no `trace_id`: add it to the log context so a failing request links to its trace
- `fault-crash` has no Argo CD application named prod-fault-crash or fault-crash: label deploys so the report can name the change and the file
- No memory limit found for `fault-crash`: set resources.limits.memory (Kyverno require-limits would enforce it) so OOM and near-limit alerts have a reference

## Evidence
- **kubernetes**: 1 pods; fault-crash-b48f849d-s9lpx: Running not ready, restarts 4, waiting CrashLoopBackOff, last exit Error(1)
  - `pods sandbox/fault-crash* in platform`
- **kubernetes**: BackOff fault-crash-b48f849d-s9lpx: Back-off restarting failed container app in pod fault-crash-b48f849d-s9lpx_sandbox(adc3035b-ffcc-446f-98ef-d2f; Pulled fault-crash-b48f849d-s9lpx: Container image "busybox:1.37.0" already present on machine; Created fault-crash-b48f849d-s9lpx: Created container app; Started fault-crash-b48f849d-s9lpx: Started container app
  - `events sandbox in platform`
- **mimir**: restarts in 45m: 3; CPU throttled up to 0%
  - `cluster="platform", namespace="sandbox", pod=~"fault-crash(-[a-z0-9]{8,10})?-[a-z0-9]{5}|fault-crash-[0-9]+|fault-crash-b48f849d-s9lpx"`
- **loki**: last log lines of the workload: 'KeyError: DATABASE_URL is not set\n' x5
  - `{cluster="platform", namespace="sandbox", pod=~"fault-crash(-[a-z0-9]{8,10})?-[a-z0-9]{5}|fault-crash-[0-9]+|fault-crash-b48f849d-s9lpx"}`
- **mimir**: node(s) platform-worker2: Ready, no pressure, CPU 0% and memory 4% of allocatable
  - `kube_node_status_condition / node usage`
- **argocd**: no Argo CD application of that name could be read
  - `history prod-fault-crash`
- **tempo**: 0 error traces in the window
  - `{ resource.service.name = "fault-crash" && status = error }`

_This RCA is blameless and was generated automatically. Review it before acting on it._
