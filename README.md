# k8s-sre-platform

A local, production-style platform for a B2B SaaS + AI product, running on upstream Kubernetes (kind) and managed entirely by Argo CD:
GitOps, the LGTM observability stack, Temporal, a sample API with an SLO, and an **AI SRE agent that investigates alerts on its own**
and writes a structured RCA and blameless postmortem.

One command creates the cluster and installs Argo CD. Everything else is pulled from this repo.

```
alert fires (Mimir ruler) -> Alertmanager -> webhook -> Temporal workflow -> SRE agent queries Mimir, Loki, Tempo,
Kubernetes and Argo CD -> structured RCA + postmortem (ConfigMap, JSON log in Loki, optional chat webhook)
```

## Quick start

Needs Linux or WSL2 with: Docker, [kind](https://kind.sigs.k8s.io), kubectl, helm, Python 3.9+ (standard library only). About 10 GB of RAM free.

```bash
git clone https://github.com/krishnakant888/k8s-sre-platform && cd k8s-sre-platform
export ANTHROPIC_API_KEY=...        # optional, enables LLM analysis in the agent
python3 bootstrap/bootstrap.py
```

The script creates the kind cluster (1 control plane, 2 workers) with Calico for NetworkPolicy, installs Argo CD, creates the few secrets with random values,
and applies one root Application. The first sync takes about 10 minutes. It prints the logins at the end.

| What | URL |
|---|---|
| Argo CD | http://argocd.localhost:8880 |
| Grafana | http://grafana.localhost:8880 |
| Temporal UI | http://temporal.localhost:8880 |
| Sample API | http://orders.localhost:8880/orders |

`*.localhost` resolves to 127.0.0.1 in browsers, no hosts file changes. Tear down with `python3 bootstrap/bootstrap.py --destroy`.

## Try the failure drill

```bash
scripts/drill.sh latency     # also: errors, oom, kill, reset
```

1. The API's downstream call becomes slow. The SLO dashboard (`Services / orders-api: RED and SLO`) shows the error budget burning.
2. After a few minutes the `OrdersApiErrorBudgetFastBurn` alert fires in Mimir and Alertmanager calls the agent webhook.
3. A Temporal workflow `incident-OrdersApiErrorBudgetFastBurn-...` starts (see it in the Temporal UI).
4. The agent runs a query playbook, classifies the cause, stores the report and logs it:
   `kubectl -n sre-agent get cm -l sre-agent/report=true` and `kubectl -n sre-agent get cm <report> -o jsonpath='{.data.postmortem\.md}'`.
5. Grafana `Platform / SRE agent: investigations` lists the reports. Sample reports for each drill are in [docs/rca-samples](docs/rca-samples).

## Architecture

```mermaid
flowchart LR
  Git[(this repo)] -->|pull| Argo[Argo CD]
  subgraph kind cluster
    Argo --> Platform[sealed-secrets, kyverno, cnpg, rbac, namespaces + netpols]
    Argo --> Obs[observability: Alloy, Mimir, Loki, Tempo, Grafana, kube-state-metrics]
    Argo --> Temporal[temporal + postgres]
    Argo --> Demo[demo: orders-api, loadgen]
    Argo --> Agent[sre-agent: worker, alert-webhook]
    Demo -->|metrics, logs, OTLP traces| Obs
    Obs -->|alert webhook| Agent
    Agent -->|start workflow| Temporal
    Temporal -->|activities| Agent
    Agent -->|read only queries| Obs
  end
```

| Namespace | Contents |
|---|---|
| `argocd` | Argo CD, manages itself |
| `observability` | Alloy (collector), Mimir (metrics, ruler, alertmanager), Loki, Tempo, Grafana, kube-state-metrics |
| `temporal` | Temporal server + UI, PostgreSQL (CloudNativePG) |
| `demo` | `orders-api` and a load generator |
| `sre-agent` | agent worker, alert webhook, stored reports |
| `kyverno`, `cnpg-system`, `traefik`, `kube-system` | policy engine, Postgres operator, ingress controller, sealed-secrets controller |

## Repo layout

```
bootstrap/          kind config + the one imperative script
gitops/root/        the root Application (app of apps)
gitops/apps/        one Application per component, AppProjects, sync waves
gitops/platform/    namespaces, network policies, RBAC, Kyverno policies, operator values
gitops/observability/  Mimir, Loki, Tempo, Grafana, Alloy configs, rules, dashboards
gitops/workloads/   orders-api, temporal, sre-agent manifests
apps/orders-api/    sample service (FastAPI): code, tests, Dockerfile
apps/sre-agent/     the agent: tools, RCA, Temporal workflow, webhook
scripts/drill.sh    failure drills
docs/               secrets, runbooks, RCA samples
```

## Observability

- **Metrics** Alloy scrapes annotated pods, kube-state-metrics and cAdvisor (read from the kubelet) and remote-writes to Mimir. Tempo's metrics generator adds span metrics and a service graph.
- **Logs** Alloy tails pod logs through the Kubernetes API (no hostPath, so Pod Security `baseline` holds) into Loki. Apps log JSON with `trace_id`, so Grafana jumps from a log line to its trace.
- **Traces** apps send OTLP to Alloy, which forwards to Tempo. Grafana links traces to logs and metrics.
- **Dashboards and datasources** are provisioned from Git (ConfigMaps picked up by the Grafana sidecar).

### SLO

`orders-api`: 99.5% of requests succeed (not 5xx) **and** finish within 300 ms, over 28 days (error budget about 3h 22m).
The SLI is computed from one histogram with a `status` label, so a slow 500 is counted once.
Alerts use multiwindow, multi-burn-rate rules ([rules file](gitops/observability/mimir/files/orders-api-slo.yaml)):
14.4x over 1h and 5m pages, 6x over 6h and 30m opens a ticket. Runbook: [docs/runbooks/orders-api.md](docs/runbooks/orders-api.md).

## Temporal and the operational workflow

`IncidentInvestigationWorkflow` ([workflows.py](apps/sre-agent/agent/workflows.py)): wait for data to settle, collect evidence, analyze,
store the report, notify. Each step is an activity with retries and timeouts, so a flaky query or a restart of the worker does not lose the investigation.
Workflow IDs are derived from the alert fingerprint and start time, so a repeated notification does not start a second run.

## The AI SRE agent

- **Read only by construction.** Tools are bounded (time range, row count, output size) and the ServiceAccount can only read pods, events and Argo CD applications. It can write one thing: a ConfigMap in its own namespace.
- **Playbook first.** A fixed set of queries (error ratio, p99, replicas, restarts, top error logs, slow and failing traces, pod state, events, recent Argo CD syncs) produces evidence and a rule based classification: dependency latency, dependency errors, pod crash, resource exhaustion (OOM), deploy regression or unknown.
- **LLM on top.** With `ANTHROPIC_API_KEY`, Claude gets the evidence plus the same read only tools and must verify it, then submit a report that validates against a schema (`submit_rca`). If the key is missing or the call fails the rule based report is kept and says `mode: rule_based`.
- **Honest output.** Reports carry the evidence with the exact query, a confidence level, contributing factors, and say when a fault injection flag is active (a drill).
- Tool output is treated as data, never as instructions, secrets are redacted from log lines, and the loop has a step limit.

## Production posture

| Area | What is in place |
|---|---|
| GitOps | Argo CD app of apps, AppProjects restrict repos, namespaces and cluster resources, automated sync with prune and self-heal, sync waves for ordering. After bootstrap nothing is applied by hand. |
| Isolation | One namespace per concern, default-deny ingress and egress with explicit allows, Pod Security labels (restricted enforced for our workloads). |
| Workloads | Requests and limits, startup/readiness/liveness probes, non-root, read-only root fs, drop ALL capabilities, seccomp, PDBs, topology spread, pinned image tags (CI writes the commit sha into Git). |
| Guardrails | Kyverno requires resource settings, pinned tags and non-root pods (enforced in `demo` and `sre-agent`, audit elsewhere). |
| RBAC | Dedicated ServiceAccounts, no token mounted unless needed, Argo CD default role is read-only. |
| Secrets | Not in Git. Created at bootstrap with random values; the Sealed Secrets controller is installed for encrypted secrets in Git ([docs/secrets.md](docs/secrets.md)). |
| Supply chain | CI: tests, image build, dependency scan, push to GHCR, tag bump commit. yamllint and kubeconform on every PR. |
| Logging | Structured JSON everywhere with trace correlation. |
| Config changes | Config and rule ConfigMaps are generated with a content hash, so a change rolls the pod. |

## Design decisions and trade-offs

- **kind with Calico.** Upstream Kubernetes in containers: reproducible, one command up and down, works on Linux, WSL2 and macOS. kind has no ingress controller and its default CNI does not enforce NetworkPolicy reliably, so Calico is installed at bootstrap and Traefik is deployed by Argo CD (host ports 8880/8444 map to its NodePorts).
- **Local disk (PVCs) instead of S3.** Mimir, Loki and Tempo run single binary with their data on PVCs. That is the production plan for this stage, and the cost is no scale-out. Moving to object storage later is a config change in each tool's storage block. I tried MinIO first and dropped it: its images are no longer pullable.
- **Mimir monolithic, Loki and Tempo single binary.** Fits a laptop. Production would run them in distributed mode with replication.
- **Mimir ruler and Alertmanager use local, Git-managed config.** Rules and routing are files in Git. Trade-off: no per-tenant API, and a change restarts Mimir (handled by hashed ConfigMaps).
- **Temporal in one pod.** `auto-setup` runs frontend, history, matching and worker together, with CloudNativePG Postgres. Production uses the official chart with separate services and a replicated database.
- **Secrets seeded at bootstrap.** Sealed Secrets are bound to a cluster key, so secrets committed for my cluster could not be decrypted on yours. Seeding at bootstrap keeps the repo runnable from scratch without putting a private key in Git.
- **No HPA locally.** metrics-server is disabled to save resources. With it enabled the API gets an HPA on CPU.
- **Rule based analysis plus an LLM.** The agent is useful without an API key and testable without a model; the model adds verification and better prose.
- **TLS is not enabled** on the local ingress. Ingress objects are ready for cert-manager.

## Moving to EKS or AKS

Only environment specifics change: storage classes (`gp3` / `managed-csi`), the ingress controller and TLS (ALB or App Gateway plus cert-manager), Sealed Secrets or bootstrap seeding replaced by External Secrets, replicas and PVC sizes, and optionally object storage for the LGTM backends.
The Argo CD apps, projects, policies, dashboards, rules and the agent are unchanged.

## Roadmap

1. External Secrets Operator with a real backend, cert-manager and TLS.
2. Object storage and distributed modes for Mimir, Loki, Tempo; HA Postgres with backups and restore drills.
3. Progressive delivery (Argo Rollouts) with SLO-based analysis, so a bad deploy is rolled back automatically.
4. Agent: human approval signal in the workflow before any remediation, remediation playbooks (restart, rollback PR), evaluation suite of recorded incidents, cost and token budgets.
5. Multi-cluster Argo CD with ApplicationSets, per-environment overlays.
6. Alert-driven SLO reporting workflow (weekly) and a status page.
7. Kyverno image signature verification (cosign) and SBOMs.

## AI usage

This repo was built with Claude Code as a pair engineer: architecture options, manifests, the agent, tests, and debugging. The full session log is in [docs/ai-log](docs/ai-log).
