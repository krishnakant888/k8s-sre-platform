# Failure scenarios

Two scripts inject failures so you can see the alerts fire and the SRE agent write an RCA:

- `scripts/drill.sh` faults inside orders-api (latency, errors, OOM, pod kill).
- `scripts/scenarios.sh` the frontend and broken workloads, described here.

Workload scenarios live in `scenarios/` and deploy to the `scenarios` namespace. Argo CD does not manage it,
so nothing gets auto-healed and `scripts/scenarios.sh cleanup` removes everything. Kyverno only audits that
namespace, so the deliberately bad specs are admitted and show up in the policy reports.

## Frontend traffic

The frontend (`gitops/workloads/frontend`, host `frontend.localhost:8080`) is a stateless nginx that logs one JSON
object per request. These scenarios generate those logs.

| Scenario | Sends | Where to look |
|---|---|---|
| healthy | 200s | baseline, nothing should alert |
| error500, error503 | HTTP 5xx | Logs dashboard: `level="error"`, requests by status |
| notfound | HTTP 404 | Logs dashboard: `level="warn"` |
| mixed | 200, 404 and 500 mix | all of the above, 5xx should rank above 4xx |

The frontend exposes no metrics, and Mimir only evaluates metric rules, so these do not fire a platform alert.
They are visible in the Logs dashboard (`{service="frontend"} | json | status >= 500`).

## Broken workloads

| Scenario | Failure | Signal | Alert |
|---|---|---|---|
| crashloop | exits 1, fatal line in logs | CrashLoopBackOff, restarts | PodCrashLooping |
| oom | exceeds a 20Mi limit | OOMKilled, exit code 137 | PodOOMKilled |
| bad-image | tag does not exist | ErrImagePull, ImagePullBackOff, no logs | DeploymentReplicasUnavailable |
| pending | requests more than any node has | FailedScheduling event, no logs | DeploymentReplicasUnavailable |
| bad-probe | readiness probe path 404s | Running 0/1 Ready, normal logs | DeploymentReplicasUnavailable |

The last three have no application logs, so a good RCA has to come from Kubernetes events and pod status, not Loki.
`bad-probe` is the interesting one: the pod is up and logging normally, and the fault is a misconfiguration.

## What a good result looks like

The agent names the right workload and namespace, picks the right category (crash, OOM, image, scheduling, probe),
gives a root cause that matches the evidence, and does not blame unrelated services. For `healthy` it should raise nothing.

Whether the agent investigates alerts from the `scenarios` namespace depends on its RBAC and network policies in
`gitops/workloads/sre-agent`; check that before reading a missing report as an agent failure.
