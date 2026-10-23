# orders-api runbook

SLO: 99.5% of requests succeed (not 5xx) and finish within 300ms, over 28 days.
Dashboard: http://grafana.localhost:8880/d/orders-api

## fast-burn
Page. The error budget is being used 14x faster than allowed (1h and 5m windows).

1. Open the dashboard. Is it errors (status panel) or latency (percentiles panel)?
2. Errors: look at `{namespace="demo", service="orders-api"} | json | level="error"` in Loki, follow a `trace_id` to Tempo and find the failing span.
3. Latency: find the slow span in a trace. A slow `inventory.lookup` span points at the downstream dependency.
4. Recent deploy? Check the Argo CD history for `orders-api`, roll back by reverting the image tag commit.
5. Active fault injection? `kubectl -n demo get secret orders-api-chaos` is only the token, check the `chaos_active` metric.

## slow-burn
Ticket. Budget burning 6x faster than allowed over 6h. Same steps, no rush, look for a pattern (time of day, one route).

## down
Page. No available replicas. `kubectl -n demo describe deploy orders-api`, check events, image pulls, probes, OOMKilled.

## latency
p99 above 1s for 5 minutes. Check CPU throttling on the pods and the `inventory.lookup` span duration.
