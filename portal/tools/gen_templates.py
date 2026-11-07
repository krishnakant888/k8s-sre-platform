#!/usr/bin/env python3
"""Generates templates/<name>/template.yaml for the self-service requests of the platform. Each template asks a few questions and starts the
`selfservice` workflow of the platform repo (scripts/selfservice.py there), which opens a pull request; nothing is applied until it is merged.

    python3 tools/gen_templates.py

Edit this file, not the generated templates.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "templates"
REPO = "github.com?owner=krishnakant888&repo=k8s-sre-platform"

SERVICE = {
    "title": "Service",
    "type": "string",
    "description": "A service the platform deploys (workloads of gitops/workloads).",
    "ui:field": "EntityPicker",
    "ui:options": {"catalogFilter": {"kind": "Component", "metadata.annotations.selfservice/enabled": "true"}, "defaultKind": "Component"},
}
SERVICE_NAME = "(parameters.service | parseEntityRef | pick('name'))"

# name, title, description, action, required, properties, params expression (python dict of name -> template expression), notes shown after
TEMPLATES = [
    dict(name="scale-service", title="Scale a service", action="scale", tags=["day-2", "scaling"],
         description="Change the number of replicas of a service. Opens a pull request.",
         required=["service", "replicas"],
         props={"service": SERVICE, "replicas": {"title": "Replicas", "type": "integer", "enum": [1, 2, 3, 4, 5, 6, 8, 10], "default": 2}},
         params={"service": SERVICE_NAME, "replicas": "parameters.replicas"}),
    dict(name="change-resources", title="Change the resources of a service", action="resources", tags=["day-2", "resources"],
         description="Set the memory limit and requests of a service's container. Kyverno requires a memory limit; the request cannot be above it.",
         required=["service", "memory_limit"],
         props={"service": SERVICE,
                "memory_limit": {"title": "Memory limit", "type": "string", "enum": ["128Mi", "256Mi", "512Mi", "1Gi", "2Gi", "4Gi"], "default": "512Mi"},
                "memory_request": {"title": "Memory request (optional)", "type": "string", "enum": ["", "64Mi", "128Mi", "256Mi", "512Mi", "1Gi", "2Gi"], "default": ""},
                "cpu_request": {"title": "CPU request (optional)", "type": "string", "description": "For example 100m or 1.", "pattern": "^(\\d{1,5}m|\\d{1,2})?$", "default": ""}},
         params={"service": SERVICE_NAME, "memory_limit": "parameters.memory_limit", "memory_request": "parameters.memory_request", "cpu_request": "parameters.cpu_request"}),
    dict(name="set-environment-variable", title="Set an environment variable", action="env", tags=["day-2", "config"],
         description="Add or change a plain environment variable of a service. Secrets are refused: use a SealedSecret.",
         required=["service", "name", "value"],
         props={"service": SERVICE,
                "name": {"title": "Name", "type": "string", "pattern": "^[A-Z][A-Z0-9_]{0,62}$", "description": "Upper case letters, digits and underscores."},
                "value": {"title": "Value", "type": "string", "maxLength": 300}},
         params={"service": SERVICE_NAME, "name": "parameters.name", "value": "parameters.value"}),
    dict(name="deploy-image-tag", title="Deploy a new image tag", action="image", tags=["day-2", "release"],
         description="Change the tag of a service's image (the repository stays). The `latest` tag is refused.",
         required=["service", "tag"],
         props={"service": SERVICE, "tag": {"title": "Image tag", "type": "string", "pattern": "^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$", "description": "For example 1.4.0 or sha-3f2c9ab."}},
         params={"service": SERVICE_NAME, "tag": "parameters.tag"}),
    dict(name="roll-back-service", title="Roll back a service", action="rollback", tags=["day-2", "release"],
         description="Go back to the image of the previous change to the service's Deployment. Opens a pull request that reverts the tag.",
         required=["service"], props={"service": SERVICE}, params={"service": SERVICE_NAME}),
    dict(name="add-hostname", title="Add a host name to a service", action="route", tags=["day-2", "gateway"],
         description="Serve a service on one more host name. Only *.localhost names: the gateway's certificate covers them.",
         required=["service", "host"],
         props={"service": SERVICE, "host": {"title": "Host name", "type": "string", "pattern": "^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*\\.localhost$", "description": "For example shop.localhost. Reachable at https://<host>:8443."}},
         params={"service": SERVICE_NAME, "host": "parameters.host"}),
    dict(name="promote-to-demo", title="Promote a sandbox service to demo", action="promote", tags=["day-2", "release"],
         description="Move a service from the sandbox namespace to demo (restricted Pod Security, Kyverno enforced).",
         required=["service"], props={"service": SERVICE}, params={"service": SERVICE_NAME}),
    dict(name="request-alert", title="Request an alert for a service", action="alert", tags=["observability", "alerting"],
         description="Add a Grafana alert rule for a service. When it fires, the SRE agent investigates and writes a report like for any other alert.",
         required=["service", "kind"],
         props={"service": SERVICE,
                "kind": {"title": "What to alert on", "type": "string", "enum": ["error-rate", "latency", "availability", "restarts", "memory"], "enumNames": [
                    "Error rate (share of 5xx at the gateway, default 0.05)", "Latency (p95 in seconds, default 1)", "Availability (fewer ready replicas than, default 1)",
                    "Restarts (in 15 minutes, default 3)", "Memory (share of the limit, default 0.9)"], "default": "error-rate"},
                "threshold": {"title": "Threshold (optional)", "type": "string", "description": "A number; leave empty for the default of the kind.", "pattern": "^([0-9]+(\\.[0-9]+)?)?$", "default": ""},
                "severity": {"title": "Severity", "type": "string", "enum": ["ticket", "page"], "default": "ticket", "description": "page also notifies the on-call."}},
         params={"service": SERVICE_NAME, "kind": "parameters.kind", "threshold": "parameters.threshold", "severity": "parameters.severity"}),
    dict(name="request-dashboard", title="Request a Grafana dashboard for a service", action="dashboard", tags=["observability", "dashboards"],
         description="A dashboard of a service: replicas, restarts, CPU, memory, gateway traffic and latency, logs. Extra panels can be added later by editing the file.",
         required=["service"], props={"service": SERVICE}, params={"service": SERVICE_NAME}),
    dict(name="request-sandbox", title="Request a short-lived sandbox namespace", action="sandbox", tags=["environments"],
         description="A namespace sbx-<name> in the platform cluster with quota, limit range and network policies, removed automatically when the time is up.",
         required=["name", "owner", "days"],
         props={"name": {"title": "Name", "type": "string", "pattern": "^[a-z][a-z0-9-]{1,30}$", "description": "The namespace is sbx-<name>."},
                "owner": {"title": "Owner", "type": "string", "ui:field": "OwnerPicker", "ui:options": {"catalogFilter": {"kind": "Group"}}},
                "days": {"title": "Lives for", "type": "integer", "enum": [1, 2, 3, 5, 7, 14], "default": 3, "description": "Days."},
                "cpu": {"title": "CPU quota (cores)", "type": "string", "enum": ["1", "2", "4"], "default": "2"},
                "memory": {"title": "Memory quota", "type": "string", "enum": ["1Gi", "2Gi", "4Gi"], "default": "2Gi"}},
         params={"name": "parameters.name", "owner": "parameters.owner", "days": "parameters.days", "cpu": "parameters.cpu", "memory": "parameters.memory"}),
    dict(name="request-access", title="Request read-only access to a namespace", action="access", tags=["access"],
         description="Give a Kubernetes user the read-only `view` role (no Secrets) in a namespace of the platform cluster for a while.",
         required=["user", "namespace", "days"],
         props={"user": {"title": "Kubernetes user", "type": "string", "pattern": "^[a-z][a-z0-9._@-]{1,60}$", "description": "The name in the user's credential (the CN of its certificate or its OIDC username)."},
                "namespace": {"title": "Namespace", "type": "string", "description": "demo, sandbox, sre-agent or a sandbox (sbx-<name>).", "pattern": "^[a-z][a-z0-9-]{1,50}$", "default": "demo"},
                "days": {"title": "For", "type": "integer", "enum": [1, 3, 7, 14, 30], "default": 7, "description": "Days."}},
         params={"user": "parameters.user", "namespace": "parameters.namespace", "days": "parameters.days"}),
    dict(name="run-failure-drill", title="Run a failure drill", action="drill", tags=["reliability", "drills"],
         description="Break a workload on purpose in the scenarios namespace of the platform cluster, to see the alerts fire and the SRE agent write its report. It is removed when the time is up.",
         required=["scenario", "minutes"],
         props={"scenario": {"title": "What breaks", "type": "string", "enum": ["crashloop", "oom", "bad-image", "bad-probe", "pending"], "enumNames": [
                    "A container that crashes at start (CrashLoopBackOff)", "A container that runs out of memory (OOMKilled)", "An image that does not exist (ImagePullBackOff)",
                    "A readiness probe that never passes", "A pod that cannot be scheduled (Pending)"], "default": "crashloop"},
                "minutes": {"title": "Runs for", "type": "integer", "enum": [15, 30, 60, 120], "default": 30, "description": "Minutes."}},
         params={"scenario": "parameters.scenario", "minutes": "parameters.minutes"}),
]


def dump(obj, indent=0):
    """Small YAML writer (JSON-compatible values only) so the output does not depend on a library."""
    pad = "  " * indent
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = json.dumps(k) if not str(k).replace("_", "").replace("-", "").isalnum() else str(k)
            if isinstance(v, (dict, list)) and v:
                if isinstance(v, list) and all(not isinstance(x, (dict, list)) for x in v):
                    out.append(f"{pad}{key}: {json.dumps(v)}")
                else:
                    out.append(f"{pad}{key}:")
                    out.append(dump(v, indent + 1))
            else:
                out.append(f"{pad}{key}: {json.dumps(v)}")
    else:
        for x in obj:
            body = dump(x, indent + 1).lstrip() if isinstance(x, (dict, list)) else json.dumps(x)
            out.append(f"{pad}- {body}")
    return "\n".join(out)


def render(t):
    params_obj = "{" + ", ".join(f"'{k}': {v}" for k, v in t["params"].items()) + "}"
    head = {"apiVersion": "scaffolder.backstage.io/v1beta3", "kind": "Template",
            "metadata": {"name": t["name"], "title": t["title"], "description": t["description"], "tags": ["self-service", *t["tags"]]},
            "spec": {"owner": "group:platform-team", "type": "self-service",
                     "parameters": [{"title": t["title"], "required": t["required"], "properties": t["props"]}]}}
    text = "# Generated by tools/gen_templates.py: edit the generator, not this file.\n" + dump(head).replace('"tags": ', "tags: ")
    text += f"""
  steps:
    - id: request
      name: The request
      action: debug:log
      input:
        message: "${{{{ {params_obj} | dump }}}}"
    - id: dispatch
      name: Run the request on GitHub and follow it
      action: selfservice:run
      input:
        action: {t['action']}
        params: "${{{{ {params_obj} }}}}"
        requestedBy: "${{{{ user.entity.metadata.name if user.entity else 'the developer portal' }}}}"
  output:
    links:
      - title: The pull request
        url: ${{{{ steps['dispatch'].output.pullRequestUrl }}}}
      - title: The workflow run on GitHub
        url: ${{{{ steps['dispatch'].output.runUrl }}}}
      - title: The deployment in Argo CD
        url: ${{{{ steps['dispatch'].output.argoUrl }}}}
      - title: All requests
        url: https://portal.localhost:7443/self-service
    text:
      - title: What happens next
        content: |
          The steps of the `selfservice` workflow of the platform repo, the merge of its pull request and the deployment by Argo CD were followed in the log above.
          The pull request merges itself once the checks pass and Argo CD applies it; if the workflow refused the request, or the application did not become healthy, the reason is shown above as the error.
"""
    return text


def main():
    for t in TEMPLATES:
        d = ROOT / t["name"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "template.yaml").write_text(render(t))
    print(len(TEMPLATES), "templates written under", ROOT)


if __name__ == "__main__":
    main()
