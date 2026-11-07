# Self-service

What a developer can ask for from the portal (Create > ...), and how it reaches the clusters. Nothing in the portal writes to a cluster: each request becomes a **pull request** that Argo CD applies after the merge, so every change is reviewed, in Git and revertable.

```
portal template  ->  GitHub Actions workflow `selfservice`  ->  scripts/selfservice.py edits the files  ->  pull request  ->  merge  ->  Argo CD applies
```

The portal starts the workflow with the `github:actions:dispatch` action. The workflow checks the inputs, makes the change with `scripts/selfservice.py`, runs yamllint on the files it touched and opens the pull request. A request the script cannot do safely fails with the reason in the run log.

## What can be asked for

| Request (`action`) | Parameters | Result |
|---|---|---|
| `scale` | service, replicas (1 to 10) | `spec.replicas` of the Deployment |
| `resources` | service, memory limit, memory request, CPU request | container resources (the request cannot exceed the limit) |
| `env` | service, name, value | a plain environment variable. Names that look like secrets are refused: use a SealedSecret (`docs/secrets.md`) |
| `image` | service, tag | the image tag (`latest` is refused, as Kyverno does) |
| `rollback` | service | the image of the previous change to the Deployment file |
| `route` | service, host | one more `*.localhost` host name on the HTTPRoute (the gateway listener's wildcard certificate covers it) |
| `promote` | service | moves a service from `sandbox` to `demo` |
| `alert` | service, kind (error-rate, latency, availability, restarts, memory), threshold, severity | a Grafana alert rule in `gitops/observability/alerting/request-<service>-<kind>.yaml` |
| `dashboard` | service, extra panels (title and PromQL) | a Grafana dashboard `service-<service>.yaml` in the folder Services |
| `sandbox` | name, owner, days (1 to 14), CPU, memory | namespace `sbx-<name>` with quota, limit range and network policies (the same as `sandbox`), removed when the time is up |
| `access` | user, namespace, days (1 to 30) | a RoleBinding of the built-in `view` role (read-only, no Secrets) for a Kubernetes user; authorization only, the user still needs a credential for the cluster |
| `drill` | scenario (bad-image, bad-probe, crashloop, oom, pending), minutes | a deliberately broken workload in the `scenarios` namespace, so the alerts and the SRE agent can be seen working (report at https://reports.localhost:9443) |
| `expire` | none | removes sandboxes, access and drills whose `# sre-platform/expires:` time has passed. Runs every 30 minutes on a schedule and opens a pull request |

The script edits the YAML as text so comments and the flow style of the repo stay as written, then parses the file before and after and refuses the change if anything other than the asked-for field moved. `scripts/test_selfservice.py` runs every action on a copy of the real manifests.

## In the portal

Create > pick a template. The requests above are the templates *Scale a service*, *Change the resources of a service*, *Set an environment variable*, *Deploy a new image tag*, *Roll back a service*, *Add a host name to a service*, *Promote a sandbox service to demo*, *Request an alert for a service*, *Request a Grafana dashboard for a service*, *Request a short-lived sandbox namespace*, *Request read-only access to a namespace* and *Run a failure drill*. *New service on the platform* opens its own pull request with the whole service (manifests, Argo CD application, catalog entry in `gitops/backstage/catalog/services/`, which the portal reads from GitHub, so the service shows up in the catalog after the merge).

Services offered in the pickers are the components annotated `selfservice/enabled: "true"` (orders-api, frontend, dummy-shop and the services created from the template).

On a component's page: **Resources** (CPU and memory against request and limit, with hints), **Scorecard** (eight checks of what the platform expects of a service), Argo CD status, firing alerts and RCA reports. The documentation of the platform is in the catalog as `platform-docs` (TechDocs, built from `docs/` with `mkdocs.yml`).

## Not done

- **Re-run an investigation from the portal**: the SRE agent only starts investigations from an alert; a manual one needs an endpoint on the agent and a decision on what it concludes when nothing is wrong.
- **Real sign-in** (GitHub or OIDC instead of guest): needs a GitHub OAuth app (client id and secret) that only an owner can create, the `auth-backend-module-github-provider` in the image, and a sign-in page with the provider. Until then every request is made as `guest`.

## Merging

Every request made through the `selfservice` workflow is merged by the workflow itself once the `lint` checks on its pull request pass (the checks are started by the workflow, because a pull request opened with the built-in token does not start them). If a check fails the pull request stays open for a person. The workflow stops at nothing else: there is no review step, so the lint result and the checks inside `scripts/selfservice.py` (secrets, `latest` tags, a memory request above the limit, unknown services) are the guard. The template run in the portal follows it all in one log: the steps of the workflow, the merge, and then the Argo CD application of the change (platform-<service> for a service, platform-base for sandboxes and access, platform-drills for drills, observability-alerting and observability-dashboards for alerts and dashboards) until it is Synced and Healthy; a Degraded application is reported as an error with the reason (a drill is expected to be Degraded and is reported as running). The Self-service page shows the same in its Deployment column. *New service on the platform* does not go through the workflow: its pull request is opened by the portal and waits for a review.

## Setup (once)

1. In the repo: Settings > Actions > General > **Allow GitHub Actions to create and approve pull requests**.
2. A token for the portal that may start the workflow: a fine-grained personal access token (or a GitHub App) limited to this repository with **Actions: read and write**, and **Contents** and **Pull requests: read and write** if the New service template should open the pull request itself. Add it to the portal's Secret as `GITHUB_TOKEN`:

   ```bash
   kubectl --context kind-gitops -n portal patch secret portal-secrets --type merge -p '{"stringData":{"GITHUB_TOKEN":"<token>"}}'
   kubectl --context kind-gitops -n portal delete pod -l app.kubernetes.io/name=portal   # a rollout restart would be reverted by Argo CD
   ```

   Without the token the portal still works; the templates that need it show the failure of the GitHub step.

## Run it by hand

```bash
python3 scripts/selfservice.py scale '{"service": "frontend", "replicas": 3}'
git diff
```
