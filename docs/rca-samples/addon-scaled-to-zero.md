# RCA: CriticalAddonDown on ingress-nginx-controller

**Report:** rca-ingress-nginx-controller-20261029-074410 | **Priority:** P4 | **Alert severity:** page | **Analysis mode:** rule_based

## Priority and where the problem is
**P4**: respond backlog, no action needed unless it repeats. No longer ongoing.
- alert severity is 'page'
- no direct user traffic depends on it
- a platform component with no direct user impact is capped at P3
- the signals have recovered, so the urgency is lower

**Infrastructure level** (high confidence): ingress-nginx-controller was scaled to 0 replicas
- kube_deployment_spec_replicas is 0
- while it was at 0 no pod existed, so there are no logs or restarts to explain it

## What happened
`ingress-nginx/ingress-nginx-controller` in the platform cluster was scaled to zero (Scaled down replica set ingress-nginx-controller-677d99b6c to 0 from 1): someone ran `kubectl scale`, a HorizontalPodAutoscaler or a Git change set it. The desired count is still 0; this is a component the cluster depends on.

## Root cause (high confidence)
Category: `scaled_to_zero`

ingress-nginx-controller was scaled to zero.

## Impact
Everything that depends on ingress-nginx-controller is affected.
SLO: no SLO defined for this signal.

## How to fix it now
- [ ] kubectl --context kind-platform -n ingress-nginx scale deploy/ingress-nginx-controller --replicas=<n>   # or revert the change in Git, Argo CD restores it on the next sync if self-heal is on
- [ ] kubectl --context kind-platform -n ingress-nginx get events --sort-by=.lastTimestamp | tail

## How to prevent it
- [ ] Keep automated sync with self-heal on for platform components and restrict `scale` rights with RBAC; alert on spec.replicas == 0 for critical add-ons (CriticalAddonDown)

## How to confirm it is fixed
- kubectl --context kind-platform -n ingress-nginx get deploy ingress-nginx-controller  # READY n/n

## Timeline
- 2026-10-29T07:41:55Z: Killing: ingress-nginx-controller-677d99b6c-fnn57 Stopping container controller
- 2026-10-29T07:41:59Z: Unhealthy: ingress-nginx-controller-677d99b6c-fnn57 Readiness probe failed: HTTP probe failed with statuscode: 500
- 2026-10-29T07:42:25.617821Z: PolicyViolation: ingress-nginx-controller policy require-run-as-non-root/autogen-run-as-non-root fail: validation error: p
- 2026-10-29T07:42:25.617918Z: PolicyViolation: ingress-nginx-controller-677d99b6c policy require-run-as-non-root/autogen-run-as-non-root fail: validation error: p
- 2026-10-29T07:44:10Z: alert CriticalAddonDown started firing
- 2026-10-29T07:44:56Z: investigation completed

## Contributing factors
- none identified

## Detection
The alert `CriticalAddonDown` fired in Grafana alerting and was sent by webhook to the SRE agent, which started this investigation as an Argo Workflow automatically.

## Observability gaps (what to add so the next one is faster)
- Log lines of `ingress-nginx-controller` have no `trace_id`: add it to the log context so a failing request links to its trace
- `ingress-nginx-controller` has no Argo CD application named prod-ingress-nginx-controller or ingress-nginx-controller: label deploys so the report can name the change and the file

## Evidence
- **kubernetes**: 0 pods; 
  - `pods ingress-nginx/ingress-nginx-controller* in platform`
- **kubernetes**: PolicyViolation ingress-nginx-controller-677d99b6c: policy require-run-as-non-root/autogen-run-as-non-root fail: validation error: pods must run as non-root with ; PolicyViolation ingress-nginx-controller: policy require-run-as-non-root/autogen-run-as-non-root fail: validation error: pods must run as non-root with ; Unhealthy ingress-nginx-controller-677d99b6c-fnn57: Readiness probe failed: HTTP probe failed with statuscode: 500; Killing ingress-nginx-controller-677d99b6c-fnn57: Stopping container controller
  - `events ingress-nginx in platform`
- **mimir**: restarts in 45m: 0; memory at 6% of its limit; memory grew 0.9x over the window; CPU throttled up to 0%
  - `cluster="platform", namespace="ingress-nginx", pod=~"ingress-nginx-controller(-[a-z0-9]{8,10})?-[a-z0-9]{5}|ingress-nginx-controller-[0-9]+"`
- **loki**: last log lines of the workload: '\n' x4; '-------------------------------------------------------------------------------\n' x4; '  nginx version: nginx/1.27.1\n' x2; '  Repository:    https://github.com/kubernetes/ingress-nginx\n' x2
  - `{cluster="platform", namespace="ingress-nginx", pod=~"ingress-nginx-controller(-[a-z0-9]{8,10})?-[a-z0-9]{5}|ingress-nginx-controller-[0-9]+"}`
- **argocd**: no Argo CD application of that name could be read
  - `history prod-ingress-nginx-controller`
- **tempo**: 0 error traces in the window
  - `{ resource.service.name = "ingress-nginx-controller" && status = error }`

_This RCA is blameless and was generated automatically. Review it before acting on it._
