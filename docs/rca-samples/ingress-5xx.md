# RCA: IngressHighErrorRate on fault-ingress-5xx

**Report:** rca-fault-ingress-5xx-20261026-135540 | **Priority:** P2 | **Alert severity:** page | **Analysis mode:** rule_based

## Priority and where the problem is
**P2**: respond within 1 hour. Still ongoing.
- alert severity is 'page'
- no direct user traffic depends on it
- it is still ongoing

**Application level** (low confidence): errors returned by service fault-ingress-5xx
- 1 ready backend pods, so the controller reaches them
- errors originate in the backend

## What happened
100% of requests through `fault-ingress-5xx` ended in 5xx (statuses {'503': 0.971}) and p95 reached 5 ms. The backend `sandbox/fault-ingress-5xx` is running (1 ready pods) but returns the errors itself, so the cause is in the application or its dependencies.

## Root cause (low confidence)
Category: `ingress_backend_failure`

The backend answers with errors: see the evidence.

## Impact
Users of fault-ingress-5xx get errors.
SLO: ingress 5xx below 1% and p95 below 500 ms.

## How to fix it now
- [ ] kubectl --context kind-platform -n sandbox logs -l app.kubernetes.io/name=fault-ingress-5xx --tail=50
- [ ] Check the dependencies named in the backend's error lines

## How to prevent it
- [ ] Give the backend its own SLO alert (RED metrics) so this is caught before the ingress sees it

## How to confirm it is fixed
- 5xx ratio on the ingress below 1%

## Timeline
- 2026-10-26T13:49:02Z: Pulled: fault-ingress-5xx-574cb7dfdb-f4pln Container image "nginxinc/nginx-unprivileged:1.27-alpine" already present on mac
- 2026-10-26T13:49:02Z: Created: fault-ingress-5xx-574cb7dfdb-f4pln Created container app
- 2026-10-26T13:49:02Z: Started: fault-ingress-5xx-574cb7dfdb-f4pln Started container app
- 2026-10-26T13:49:27Z: Sync: fault-ingress-5xx Scheduled for sync
- 2026-10-26T13:55:40Z: alert IngressHighErrorRate started firing
- 2026-10-26T13:56:24Z: investigation completed

## Contributing factors
- none identified

## Detection
The alert `IngressHighErrorRate` fired in Grafana alerting and was sent by webhook to the SRE agent, which started this investigation as an Argo Workflow automatically.

## Observability gaps (what to add so the next one is faster)
- No error log lines for `fault-ingress-5xx` reached Loki: make sure the process logs to stdout/stderr and mark errors with a level (the collector derives the `level` label from JSON `level` or from the text)
- No traces for `fault-ingress-5xx`: instrument it with OpenTelemetry (OTEL_EXPORTER_OTLP_ENDPOINT=http://alloy.observability:4318) so slow or failing requests show the span that caused it

## Evidence
- **mimir**: 0.97 req/s, 5xx ratio peaked at 100% (now 100%), p95 up to 5 ms; by status {'503': 0.971}
  - `nginx_ingress_controller_requests fault-ingress-5xx`
- **kubernetes**: 1 pods; fault-ingress-5xx-574cb7dfdb-f4pln: Running, restarts 0
  - `pods sandbox/fault-ingress-5xx* in platform`
- **kubernetes**: Sync fault-ingress-5xx: Scheduled for sync; Pulled fault-ingress-5xx-574cb7dfdb-f4pln: Container image "nginxinc/nginx-unprivileged:1.27-alpine" already present on machine; Created fault-ingress-5xx-574cb7dfdb-f4pln: Created container app; Started fault-ingress-5xx-574cb7dfdb-f4pln: Started container app
  - `events sandbox in platform`
- **loki**: error lines of backend fault-ingress-5xx: no error lines in the window
  - `{cluster="platform", namespace="sandbox", pod=~"fault-ingress-5xx-.*"} |~ "(?i)error|fatal|panic|exception|traceback|refused|denied|timeout|unable|failed|cannot"`
- **mimir**: node(s) platform-worker: Ready, no pressure, CPU 0% and memory 4% of allocatable
  - `kube_node_status_condition / node usage`
- **argocd**: no Argo CD application of that name could be read
  - `history prod-fault-ingress-5xx`
- **tempo**: 0 error traces in the window
  - `{ resource.service.name = "fault-ingress-5xx" && status = error }`

_This RCA is blameless and was generated automatically. Review it before acting on it._
