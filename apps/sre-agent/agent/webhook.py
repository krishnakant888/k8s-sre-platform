"""Receives Alertmanager webhooks and starts one investigation Argo Workflow per firing alert."""
import logging
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest

from agent import config, incident
from agent.logging_setup import setup_logging

setup_logging("alert-webhook")
log = logging.getLogger("alert-webhook")
STARTED = Counter("investigations_started_total", "Investigation workflows started", ["alertname"])

app = FastAPI(title="alert-webhook")
WORKFLOWS_URL = f"{config.K8S_URL}/apis/argoproj.io/v1alpha1/namespaces/{config.WORKFLOW_NAMESPACE}/workflows"


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
        resp = await client.post(WORKFLOWS_URL, json=workflow, headers={"Authorization": f"Bearer {token}"})
    if resp.status_code == 409:
        return workflow["metadata"]["name"], False
    resp.raise_for_status()  # a 5xx here makes Alertmanager retry the notification
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
