# k8s-sre-platform

Three kind clusters (gitops, observability, platform) run by Argo CD from this repository, with the Traefik gateway, cert-manager, Kyverno, the Grafana stack, an SRE agent that writes an RCA report for every alert, and this developer portal.

- **How the observability stack fits together:** [Observability](observability-architecture.md) and [Logging](logging.md).
- **What you can ask for in the portal** (scale, resources, image tags, alerts, dashboards, sandboxes, access, failure drills): [Self-service](self-service.md).
- **The portal itself:** [Developer portal](portal.md).
- **When something breaks:** the RCA reports at https://reports.localhost:9443 and the runbook of [orders-api](runbooks/orders-api.md).
