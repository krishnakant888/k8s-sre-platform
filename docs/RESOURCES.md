# Resource requests and limits

Every container sets CPU and memory requests and a memory limit (Kyverno enforces it in `demo` and `sre-agent`).
The values were sized from measured usage, not guessed.

## Rules

- **Requests** are about 1.25x the measured peak, so the scheduler reserves what the container really needs. Components whose data grows
  (Mimir, Loki, Tempo) get headroom above what they use on a young cluster.
- **Memory limits** are about 2x the request: a spike does not OOMKill the container, and a leak still gets stopped.
- **CPU has requests only, no limits.** A CPU limit throttles a container that has spare CPU on the node, which hurts latency for no gain.
- **Tiny sidecars and hook jobs** get small explicit values instead of the chart defaults, which are often much larger than needed.

## Values

| Component | CPU request | Memory request | Memory limit | Peak measured (1h) |
|---|---|---|---|---|
| Argo CD application controller | 500m | 1Gi | 2Gi | 599Mi (was OOMKilled at 768Mi) |
| Argo CD repo-server | 100m | 256Mi | 1Gi | 78Mi, spikes while rendering charts |
| Argo CD server | 100m | 256Mi | 512Mi | 40Mi |
| Argo CD redis, applicationset | 50m | 128Mi | 256Mi | 9Mi, 27Mi |
| Argo Workflows controller | 50m | 128Mi | 512Mi | 18Mi |
| Argo Workflows server | 50m | 128Mi | 256Mi | 21Mi |
| Argo Workflows executor (init, wait) | 25m | 64Mi | 128Mi | |
| Incident steps: collect, analyze | 100m | 128Mi, 256Mi | 512Mi, 768Mi | old worker: 57Mi |
| Incident steps: store, notify | 50m | 64Mi | 256Mi | |
| Mimir | 500m | 1Gi | 2Gi | 481Mi |
| Loki | 200m | 512Mi | 1Gi | 157Mi |
| Tempo | 200m | 768Mi | 1536Mi | 415Mi (was above its 256Mi request) |
| Alloy, config-reloader | 200m, 10m | 384Mi, 32Mi | 768Mi, 64Mi | 301Mi (was above its 192Mi request), 8Mi |
| Grafana, sidecars (each) | 100m, 20m | 256Mi, 128Mi | 512Mi, 256Mi | 135Mi, 85Mi (was above its 64Mi request) |
| kube-state-metrics | 25m | 64Mi | 256Mi | 23Mi, grows with object count |
| Developer portal (Backstage) | 200m | 640Mi | 1280Mi | not measured |
| Portal database (Postgres) | 50m | 128Mi | 384Mi | not measured |
| Traefik (gateway, one per cluster) | 100m | 128Mi | 256Mi | not measured |
| cert-manager, webhook, cainjector (one set per cluster) | 25m, 10m, 25m | 64Mi, 32Mi, 128Mi | 256Mi, 128Mi, 256Mi | not measured |
| Kyverno admission | 100m | 256Mi | 768Mi | 54Mi |
| Kyverno background, reports | 50m | 128Mi | 512Mi | 34Mi, 52Mi |
| Kyverno cleanup | 25m | 64Mi | 256Mi | 30Mi |
| Sealed Secrets controller | 25m | 64Mi | 256Mi | |
| orders-api | 100m | 128Mi | 256Mi | 44Mi |
| frontend | 50m | 64Mi | 128Mi | 19Mi |
| alert-webhook | 50m | 128Mi | 256Mi | 57Mi |
| RCA viewer (nginx) | 25m | 32Mi | 128Mi | |
| RCA reports cleanup job | 10m | 16Mi | 64Mi | |

## Revisit when

The peaks were read from Mimir over a cluster that was about an hour old. Re-measure after a few days of data, especially for Mimir, Loki and Tempo:

```
max by (pod, container) (max_over_time(container_memory_working_set_bytes{container!=""}[7d]))
```

Add a VerticalPodAutoscaler in recommendation mode, or Goldilocks, to keep this honest instead of re-reading the table by hand.

## Safe scale-down review

The SRE agent runs a daily, guarded scale-down check for `orders-api` and `frontend`. It requires complete coverage of the 24-hour metric window,
no matching active alert, no container restarts, all desired replicas available, and no deployment-file change in the last 24 hours.
It proposes at most one step per workload per run and attempts to auto-merge after those checks; repository protections can keep the PR open.
The workflow uses the read-only Grafana token in `rca-viewer-grafana`; if that Secret predates this check, run `python3 bootstrap/bootstrap.py --viewer-access` once to add its `token` key.

- Replicas decrease one at a time only when CPU P95 is below 50% of capacity, throttling is below 1%, and memory P99 is below 60% of its limit. User-facing services stay at 2 or more replicas.
- At the replica floor, memory limits can decrease by 128Mi or 512Mi steps, never below the request. CPU limits can decrease by 50m, never below the request.
- Missing telemetry, an active alert, a recent restart, or an unstable rollout means no recommendation.
