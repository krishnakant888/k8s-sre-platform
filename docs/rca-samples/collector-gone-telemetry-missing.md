# RCA: ClusterTelemetryMissing on alloy

**Report:** rca-alloy-20261029-075640 | **Priority:** P2 | **Alert severity:** page | **Analysis mode:** rule_based

## Priority and where the problem is
**P2**: respond within 1 hour. Still ongoing.
- alert severity is 'page'
- it is platform infrastructure
- while telemetry is missing, other incidents in that cluster go unnoticed
- it is still ongoing

**Infrastructure level** (high confidence): the collector (Alloy) of the gitops cluster has no pod
- no collector pod in the cluster
- the cluster API answers

## What happened
No telemetry arrives from the gitops cluster because its collector has no pod: it was scaled to zero or deleted. The cluster itself answers its API. Dashboards and alerts for gitops are blind until the collector is back.

## Root cause (high confidence)
Category: `telemetry_gap`

The collector in gitops does not exist.

## Impact
gitops is unobserved.
SLO: no SLO defined for this signal.

## How to fix it now
- [ ] kubectl --context kind-gitops -n observability get deploy alloy
- [ ] kubectl --context kind-gitops -n observability scale deploy/alloy --replicas=1   # or let Argo CD restore it (self-heal)

## How to prevent it
- [ ] Keep self-heal on for the collector and alert on its absence from outside the cluster (this alert)

## How to confirm it is fixed
- up{job="alloy",cluster="gitops"} == 1

## Timeline
- 2026-10-29T07:46:16Z: Killing: alloy-68b778bfc7-pjxnt Stopping container alloy
- 2026-10-29T07:46:16Z: Killing: alloy-68b778bfc7-pjxnt Stopping container config-reloader
- 2026-10-29T07:46:16Z: SuccessfulDelete: alloy-68b778bfc7 Deleted pod: alloy-68b778bfc7-pjxnt
- 2026-10-29T07:46:16Z: ScalingReplicaSet: alloy Scaled down replica set alloy-68b778bfc7 to 0 from 1
- 2026-10-29T07:56:40Z: alert ClusterTelemetryMissing started firing
- 2026-10-29T07:57:29Z: investigation completed

## Contributing factors
- none identified

## Detection
The alert `ClusterTelemetryMissing` fired in Grafana alerting and was sent by webhook to the SRE agent, which started this investigation as an Argo Workflow automatically.

## Observability gaps (what to add so the next one is faster)
- The telemetry available was enough for this investigation.

## Evidence
- **mimir**: no sample from the collector in the window; failed remote writes 0.00/s, dropped log lines 0.00/s; scrape jobs down: none
  - `alloy in gitops`
- **kubernetes**: 0 pods; 
  - `pods observability/alloy* in gitops`
- **kubernetes**: Killing alloy-68b778bfc7-pjxnt: Stopping container alloy; Killing alloy-68b778bfc7-pjxnt: Stopping container config-reloader; SuccessfulDelete alloy-68b778bfc7: Deleted pod: alloy-68b778bfc7-pjxnt; ScalingReplicaSet alloy: Scaled down replica set alloy-68b778bfc7 to 0 from 1
  - `events observability in gitops`
- **loki**: collector error lines: 'node exited without error' x4
  - `{cluster="gitops", namespace="observability", service="alloy"} |~ "(?i)error|fatal|panic|exception|traceback|refused|denied|timeout|unable|failed|cannot"`

_This RCA is blameless and was generated automatically. Review it before acting on it._
