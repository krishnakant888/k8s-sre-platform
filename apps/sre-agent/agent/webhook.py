"""Receives alert webhooks (Grafana alerting, the payload is Alertmanager compatible) and starts one investigation Argo Workflow per firing alert."""
import logging
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
from pydantic import BaseModel

from agent import config, generic, incident
from agent.logging_setup import setup_logging

setup_logging("alert-webhook")
log = logging.getLogger("alert-webhook")
STARTED = Counter("investigations_started_total", "Investigation workflows started", ["alertname"])
DEDUPLICATED = Counter("investigations_deduplicated_total", "Alerts that joined a running or recent investigation of the same workload", ["alertname"])

# A counter label that first appears with value 1 has no earlier sample, so increase() reports 0 for that first investigation (the dashboards showed zeros).
# Start every alert the agent knows at 0 so the first increment is counted too.
for _name in [*generic.FAMILIES, "OrdersApiErrorBudgetFastBurn", "OrdersApiErrorBudgetSlowBurn", "OrdersApiHighLatencyP99"]:
    STARTED.labels(_name)
    DEDUPLICATED.labels(_name)

app = FastAPI(title="alert-webhook")
WORKFLOWS_URL = f"{config.K8S_URL}/apis/argoproj.io/v1alpha1/namespaces/{config.WORKFLOW_NAMESPACE}/workflows"
_WORKLOADS_DEFAULT = "orders-api:demo,dummy-shop:sandbox"
_TRIGGER_TEMPLATES = {
    "health-review": "weekly-health-review",
    "self-learning": "self-learning",
}


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


async def create_workflow(alert: dict) -> tuple[str, bool]:
    """Returns the workflow name and whether it was newly created. A 409 means this alert already has one."""
    workflow = incident.build_workflow(alert)
    token = Path(f"{config.SA_DIR}/token").read_text()  # re-read every time, projected tokens rotate
    async with httpx.AsyncClient(verify=f"{config.SA_DIR}/ca.crt", timeout=15) as client:
        listing = await client.get(WORKFLOWS_URL, params={"labelSelector": f"sre-agent/target={workflow['metadata']['labels']['sre-agent/target']}"}, headers={"Authorization": f"Bearer {token}"})
        if listing.status_code == 200:
            twin = incident.recent_duplicate(listing.json().get("items", []), time.time())
            if twin and twin != workflow["metadata"]["name"]:
                DEDUPLICATED.labels(alert.get("labels", {}).get("alertname", "unknown")).inc()
                log.info("alert joined an existing investigation", extra={"alertname": alert.get("labels", {}).get("alertname"), "workflow": twin})
                return twin, False
        resp = await client.post(WORKFLOWS_URL, json=workflow, headers={"Authorization": f"Bearer {token}"})
    if resp.status_code == 409:
        return workflow["metadata"]["name"], False
    resp.raise_for_status()  # a 5xx here makes Grafana retry the notification
    return workflow["metadata"]["name"], True


@app.post("/alertmanager", status_code=202)
async def alertmanager(request: Request):
    payload = await request.json()
    started = []
    for alert in payload.get("alerts", []):
        if alert.get("status") != "firing":
            continue
        name = alert.get("labels", {}).get("alertname", "unknown")
        workflow_name, created = await create_workflow(alert)
        if not created:
            continue  # a repeat notification for the same alert is a no-op
        STARTED.labels(name).inc()
        log.info("investigation started", extra={"alertname": name, "workflow": workflow_name})
        started.append(workflow_name)
    return {"started": started}


class TriggerRequest(BaseModel):
    workflow_type: str
    services: str = "all"


@app.post("/trigger", status_code=202)
async def trigger_review(req: TriggerRequest):
    if req.workflow_type not in _TRIGGER_TEMPLATES:
        raise HTTPException(status_code=400, detail=f"unknown workflow_type: {req.workflow_type!r}")
    template_name = _TRIGGER_TEMPLATES[req.workflow_type]
    workloads = None if req.workflow_type == "self-learning" else (
        req.services if req.services != "all" else _WORKLOADS_DEFAULT)
    spec: dict = {"workflowTemplateRef": {"name": template_name}}
    if workloads:
        spec["arguments"] = {"parameters": [{"name": "workloads", "value": workloads}]}
    workflow = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Workflow",
        "metadata": {
            "generateName": f"manual-{req.workflow_type}-",
            "namespace": config.WORKFLOW_NAMESPACE,
            "labels": {"sre-agent/trigger": "manual", "sre-agent/review-type": req.workflow_type},
        },
        "spec": spec,
    }
    token = Path(f"{config.SA_DIR}/token").read_text()
    async with httpx.AsyncClient(verify=f"{config.SA_DIR}/ca.crt", timeout=15) as client:
        resp = await client.post(WORKFLOWS_URL, json=workflow, headers={"Authorization": f"Bearer {token}"})
    resp.raise_for_status()
    name = resp.json().get("metadata", {}).get("name", "unknown")
    log.info("manual review triggered", extra={"review_type": req.workflow_type, "workflow": name})
    return {"workflow": name, "workflow_type": req.workflow_type, "services": req.services}


@app.get("/active-workflows")
async def list_active_workflows():
    token = Path(f"{config.SA_DIR}/token").read_text()
    async with httpx.AsyncClient(verify=f"{config.SA_DIR}/ca.crt", timeout=10) as client:
        resp = await client.get(
            WORKFLOWS_URL,
            params={"limit": "50"},
            headers={"Authorization": f"Bearer {token}"},
        )
    resp.raise_for_status()
    items = resp.json().get("items") or []
    active = [
        {
            "name": w["metadata"]["name"],
            "workflow_type": w["metadata"].get("labels", {}).get("sre-agent/review-type", ""),
            "phase": w.get("status", {}).get("phase", "Unknown"),
            "startedAt": w.get("status", {}).get("startedAt"),
            "progress": w.get("status", {}).get("progress", ""),
        }
        for w in items
        if w.get("status", {}).get("phase") in ("Running", "Pending")
    ]
    return {"workflows": active}


@app.get("/workloads")
async def list_workloads():
    import os
    raw = os.environ.get("KNOWN_WORKLOADS", _WORKLOADS_DEFAULT)
    items = [w.strip() for w in raw.split(",") if w.strip()]
    return {"workloads": items}


@app.post("/re-analyze/{report_id}", status_code=202)
async def re_analyze(report_id: str):
    """Re-run investigation for an existing report using its stored alert_details."""
    import json as _json
    report_path = Path(config.REPORTS_DIR) / report_id / "report.json"
    if not report_path.exists():
        raise HTTPException(status_code=404, detail=f"report {report_id!r} not found")
    report = _json.loads(report_path.read_text())
    ad = report.get("alert_details", {})
    if not ad.get("alertname"):
        raise HTTPException(status_code=422, detail="report has no alert_details to re-analyze from")
    # reconstruct minimal Alertmanager alert dict
    alert = {
        "labels": {**ad.get("labels", {}), "alertname": ad["alertname"]},
        "annotations": ad.get("annotations", {}),
        "startsAt": ad.get("starts_at", ""),
        "status": "firing",
    }
    workflow_name, newly_created = await create_workflow(alert)
    STARTED.labels(ad["alertname"]).inc()
    log.info("re-analysis triggered", extra={"report_id": report_id, "alertname": ad["alertname"], "workflow": workflow_name, "newly_created": newly_created})
    return {"workflow": workflow_name, "report_id": report_id, "newly_created": newly_created}


@app.get("/workflow-status/{name}")
async def get_workflow_status(name: str):
    token = Path(f"{config.SA_DIR}/token").read_text()
    async with httpx.AsyncClient(verify=f"{config.SA_DIR}/ca.crt", timeout=10) as client:
        resp = await client.get(
            f"{WORKFLOWS_URL}/{name}",
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="workflow not found")
    resp.raise_for_status()
    status = resp.json().get("status", {})
    return {
        "phase": status.get("phase", "Unknown"),
        "startedAt": status.get("startedAt"),
        "finishedAt": status.get("finishedAt"),
        "message": status.get("message", ""),
        "progress": status.get("progress", ""),  # "X/Y" e.g. "2/4"
    }
