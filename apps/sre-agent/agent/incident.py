"""Helpers for the incident workflow: the Argo Workflow object the webhook creates for an alert, and size limits
for the JSON that steps hand to each other (workflow parameters live in the Workflow object, which is capped by etcd)."""
import hashlib
import json
import re

from agent import config

# one workflow parameter has to stay well under the 1 MiB object limit that the whole Workflow shares
MAX_PARAM_BYTES = 150_000


def _slug(text: str, limit: int) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-")


def workflow_name(alert: dict) -> str:
    """Deterministic per alert, so a repeated notification for the same alert maps to the same name (a no-op)."""
    labels = alert.get("labels", {})
    name = labels.get("alertname", "unknown")
    key = f"{name}|{alert.get('fingerprint', '')}|{alert.get('startsAt', '')[:19]}"
    return f"incident-{_slug(name, 40) or 'unknown'}-{hashlib.sha1(key.encode()).hexdigest()[:10]}"


def build_workflow(alert: dict) -> dict:
    name = workflow_name(alert)
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Workflow",
        "metadata": {
            "name": name,
            "namespace": config.WORKFLOW_NAMESPACE,
            "labels": {
                "app.kubernetes.io/part-of": "sre-agent",
                "sre-agent/alertname": _slug(alert.get("labels", {}).get("alertname", "unknown"), 63) or "unknown",
            },
        },
        "spec": {
            "workflowTemplateRef": {"name": config.WORKFLOW_TEMPLATE},
            "arguments": {"parameters": [{"name": "alert", "value": json.dumps(alert)}]},
        },
    }


def fit_evidence(ctx: dict, limit: int = MAX_PARAM_BYTES) -> dict:
    """Drops the last evidence items until the serialized context fits, and says so in the context."""
    dropped = 0
    while len(json.dumps(ctx)) > limit and ctx.get("evidence"):
        ctx["evidence"].pop()
        dropped += 1
    if dropped:
        ctx["truncated_evidence_items"] = dropped
    return ctx
