# RCA: ArgoAppUnhealthy on fault-argo-degraded

**Report:** rca-fault-argo-degraded-20261029-063610 | **Priority:** P3 | **Alert severity:** ticket | **Analysis mode:** rule_based

## Priority and where the problem is
**P3**: respond next business day. Still ongoing.
- alert severity is 'ticket'
- it is platform infrastructure
- it is still ongoing

**Application level** (medium confidence): resources of fault-argo-degraded: Deployment/drill-degraded: Degraded Deployment "drill-degraded" exceeded its progress deadline
- sync is fine, health is not
- see the pod status and events of the unhealthy resource in the evidence

## What happened
`fault-argo-degraded` is synced from Git but its health is Degraded: Deployment/drill-degraded: Degraded Deployment "drill-degraded" exceeded its progress deadline (destination platform/sandbox). The Git change itself applied; what it deployed does not run correctly.

## Root cause (medium confidence)
Category: `deploy_regression`

The application deployed but its resources are unhealthy: Deployment/drill-degraded: Degraded Deployment "drill-degraded" exceeded its progress deadline.

## Impact
fault-argo-degraded is partly or fully down.
SLO: no SLO defined for this signal.

## How to fix it now
- [ ] Look at the unhealthy resource (Deployment/drill-degraded: Degraded Deployment "drill-degraded" exceeded its progress deadline): its pods, events and logs
- [ ] Revert the last commit to the app if it started with that deploy

## How to prevent it
- [ ] Use sync waves and progressive delivery so a bad release stops early

## How to confirm it is fixed
- argocd app get fault-argo-degraded  # Healthy

## Timeline
- 2026-10-29T06:36:10Z: alert ArgoAppUnhealthy started firing
- 2026-10-29T06:36:58Z: investigation completed

## Contributing factors
- none identified

## Detection
The alert `ArgoAppUnhealthy` fired in Grafana alerting and was sent by webhook to the SRE agent, which started this investigation as an Argo Workflow automatically.

## Observability gaps (what to add so the next one is faster)
- The telemetry available was enough for this investigation.

## Evidence
- **argocd**: sync Synced, health Degraded, destination platform; operation Succeeded: successfully synced (all tasks run); unhealthy resources: Deployment/drill-degraded: Degraded Deployment "drill-degraded" exceeded its progress deadline
  - `application fault-argo-degraded`

_This RCA is blameless and was generated automatically. Review it before acting on it._
