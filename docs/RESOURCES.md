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
| ingress-nginx controller | 100m | 128Mi | 512Mi | 57Mi |
| Kyverno admission | 100m | 256Mi | 768Mi | 54Mi |
| Kyverno background, reports | 50m | 128Mi | 512Mi | 34Mi, 52Mi |
| Kyverno cleanup | 25m | 64Mi | 256Mi | 30Mi |
| Sealed Secrets controller | 25m | 64Mi | 256Mi | |
| orders-api | 100m | 128Mi | 256Mi | 44Mi |
| frontend | 50m | 64Mi | 128Mi | 19Mi |
| loadgen | 20m | 64Mi | 128Mi | 24Mi |
| alert-webhook | 50m | 128Mi | 256Mi | 57Mi |

## Revisit when

The peaks were read from Mimir over a cluster that was about an hour old. Re-measure after a few days of data, especially for Mimir, Loki and Tempo:

```
max by (pod, container) (max_over_time(container_memory_working_set_bytes{container!=""}[7d]))
```

Add a VerticalPodAutoscaler in recommendation mode, or Goldilocks, to keep this honest instead of re-reading the table by hand.
