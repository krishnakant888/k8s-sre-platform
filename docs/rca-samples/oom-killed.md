# RCA: PodOOMKilled on fault-oom

**Report:** rca-fault-oom-20261026-125550 | **Priority:** P3 | **Alert severity:** ticket | **Analysis mode:** rule_based

## Priority and where the problem is
**P3**: respond next business day. Still ongoing.
- alert severity is 'ticket'
- no direct user traffic depends on it
- a platform component with no direct user impact is capped at P3
- it is still ongoing

**Application level** (medium confidence): memory growth in fault-oom (leak or unbounded cache)
- memory grew 3.3x without levelling off
- restarts reset the growth, which repeats

## What happened
`sandbox/fault-oom` in the platform cluster uses more memory every minute (x3.3 over the window) and is OOMKilled. Raising the limit would only delay the kill, so this points at the application's memory behaviour.

## Root cause (medium confidence)
Category: `resource_exhaustion`

Memory of fault-oom keeps growing (3.3x in the window) until the 64Mi limit kills it.

## Impact
fault-oom restarts repeatedly and drops requests while it does.
SLO: no SLO defined for this signal.

## How to fix it now
- [ ] Buy time: raise the memory limit of `fault-oom` in the manifest of `fault-oom` in Git (suggested 128Mi) and sync
- [ ] Capture the cause: kubectl --context kind-platform -n sandbox logs fault-oom-7cb4d9866c-8rzfv --previous | tail -50

## How to prevent it
- [ ] Find the leak with profiling (add Pyroscope or a heap profile endpoint), bound caches and queues, and set a limit with 25% headroom over the steady state

## How to confirm it is fixed
- max(container_memory_working_set_bytes{cluster="platform",namespace="sandbox",pod=~"fault-oom-.*"}) stays flat for 30 minutes

## Timeline
- 2026-10-26T12:55:50Z: alert PodOOMKilled started firing
- 2026-10-26T12:56:08Z: Pulled: fault-oom-7cb4d9866c-8rzfv Container image "python:3.12-alpine" already present on machine
- 2026-10-26T12:56:08Z: Created: fault-oom-7cb4d9866c-8rzfv Created container app
- 2026-10-26T12:56:08Z: Started: fault-oom-7cb4d9866c-8rzfv Started container app
- 2026-10-26T12:56:25Z: BackOff: fault-oom-7cb4d9866c-8rzfv Back-off restarting failed container app in pod fault-oom-7cb4d9866c-8rzfv_sandb
- 2026-10-26T12:56:42Z: investigation completed

## Contributing factors
- none identified

## Detection
The alert `PodOOMKilled` fired in Grafana alerting and was sent by webhook to the SRE agent, which started this investigation as an Argo Workflow automatically.

## Observability gaps (what to add so the next one is faster)
- `fault-oom` has no Argo CD application named prod-fault-oom or fault-oom: label deploys so the report can name the change and the file

## Evidence
- **kubernetes**: 1 pods; fault-oom-7cb4d9866c-8rzfv: Running not ready, restarts 3, last exit OOMKilled(137)
  - `pods sandbox/fault-oom* in platform`
- **kubernetes**: BackOff fault-oom-7cb4d9866c-8rzfv: Back-off restarting failed container app in pod fault-oom-7cb4d9866c-8rzfv_sandbox(83ee7550-5978-406e-8000-777; Pulled fault-oom-7cb4d9866c-8rzfv: Container image "python:3.12-alpine" already present on machine; Created fault-oom-7cb4d9866c-8rzfv: Created container app; Started fault-oom-7cb4d9866c-8rzfv: Started container app
  - `events sandbox in platform`
- **mimir**: restarts in 45m: 2; memory at 8% of its limit; memory grew 3.3x over the window; CPU throttled up to 0%
  - `cluster="platform", namespace="sandbox", pod=~"fault-oom-.*|fault-oom-7cb4d9866c-8rzfv"`
- **loki**: last log lines of the workload: no error lines in the window
  - `{cluster="platform", namespace="sandbox", pod=~"fault-oom-.*|fault-oom-7cb4d9866c-8rzfv"}`
- **mimir**: node(s) platform-worker: Ready, no pressure, CPU 0% and memory 3% of allocatable
  - `kube_node_status_condition / node usage`
- **argocd**: no Argo CD application of that name could be read
  - `history prod-fault-oom`

_This RCA is blameless and was generated automatically. Review it before acting on it._
