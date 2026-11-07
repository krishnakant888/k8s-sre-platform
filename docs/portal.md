# Developer portal (Backstage)

An internal developer platform on top of this repo: one place to see what runs, who owns it, how it is doing, and to create a new service that already fits the platform. It is Backstage, in the **gitops cluster**, at **https://backstage-portal.localhost:7443** (also http on :7080). Sign-in is the guest user and the portal is read-only.

## What it gives you

| Where | What |
|---|---|
| **Catalog** | Every component (orders-api, frontend, the SRE agent, Traefik, Argo CD, Grafana, Mimir, Loki, Tempo, Alloy, Kyverno...), the three clusters as resources, owner, system, dependencies and links to Grafana, Argo CD, the runbook and the RCA reports. The data is in `gitops/backstage/catalog/`. |
| **Kubernetes tab** of a component | The pods and deployments of that component, read live from the three clusters. |
| **Argo CD card** | Sync and health of the application that deploys the component, and its last sync. |
| **Alerts card** | The Grafana alerts that are firing for the component (alerts whose labels match `grafana/alert-label-selector`). |
| **RCA reports card** | The RCA reports the agent wrote for the component, with a link to open each. |
| **Create > New service on the platform** | Generates the Deployment, Service, HTTPRoute, PodDisruptionBudget, the Argo CD application and the catalog entry for a new service, compliant with the Kyverno policies, Pod Security and the gateway. You commit the generated files to this repo. |

## How it is put together

```
backstage-portal/ folder     the Backstage app: plugins, the three cards, the new-service template  ->  container image
k8s-sre-platform repo        how it runs here (gitops/backstage: portal + Postgres + route) and what it shows (the catalog)
```

- **Deployment:** `gitops/hub/apps/portal.yaml` is the Argo CD application `gitops-backstage-portal`; it deploys `gitops/backstage` (kustomize): the portal, its Postgres (a StatefulSet on a volume), the route `backstage-portal.localhost`, the catalog ConfigMap and the portal's configuration (`app-config.k8s.yaml`).
- **Catalog changes** are a pull request to `gitops/backstage/catalog/*.yaml`; the ConfigMap name carries a hash of its content, so the portal restarts and reads the new catalog when Argo CD syncs.
- **Read-only access:** `bootstrap.py` creates a service account `portal-reader` in each cluster (a ClusterRole with get/list/watch on workloads, pods, services, ConfigMaps and routes, no Secrets, no writes), a read-only `portal` account in Argo CD, and a Viewer service account in Grafana. Their tokens are in the Secret `portal-secrets` in the `portal` namespace, never in Git.
- **Grafana, RCA viewer:** the portal reaches them through the backend proxy (`/api/proxy/grafana`, `/api/proxy/rca`) with the token or login held server side, through the observability cluster's gateway.

## Run it

`python3 bootstrap/bootstrap.py` creates the access and the Secret. Grafana is not up yet at that point, so its token is created by a second step once Argo CD has synced:

```bash
python3 bootstrap/bootstrap.py --portal-access
```

**The image.** The image is built from the `backstage-portal/` folder of this repo (the `backstage-portal-image` workflow builds it to ghcr for amd64 and arm64). Downloads from ghcr.io can be very slow from some networks, so on this machine build it with Docker and load it into the gitops cluster instead:

```bash
docker build -t ghcr.io/krishnakant888/backstage-portal:0.1.0 -f packages/backend/Dockerfile .   # in portal/al repo, after yarn install, tsc and build:backend
kind load docker-image ghcr.io/krishnakant888/backstage-portal:0.1.0 --name gitops
```

## Not in the first version

TechDocs (the docs in this repo rendered in the portal), real sign-in (GitHub or OIDC), and templates that open a pull request (they need a GitHub token with write access). Today the template shows the generated files and you commit them.
