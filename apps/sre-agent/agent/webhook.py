"""Receives Alertmanager webhooks and starts one investigation workflow per firing alert."""
import logging

from fastapi import FastAPI, Request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
from fastapi.responses import Response
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy

from agent import config
from agent.logging_setup import setup_logging
from agent.workflows import IncidentInvestigationWorkflow

setup_logging("alert-webhook")
log = logging.getLogger("alert-webhook")
STARTED = Counter("investigations_started_total", "Investigation workflows started", ["alertname"])

app = FastAPI(title="alert-webhook")
_client: Client | None = None


async def temporal() -> Client:
    global _client
    if _client is None:
        _client = await Client.connect(config.TEMPORAL_ADDRESS, namespace=config.TEMPORAL_NAMESPACE)
    return _client


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/alertmanager", status_code=202)
async def alertmanager(request: Request):
    payload = await request.json()
    client = await temporal()
    started = []
    for alert in payload.get("alerts", []):
        if alert.get("status") != "firing":
            continue
        name = alert.get("labels", {}).get("alertname", "unknown")
        workflow_id = f"incident-{name}-{alert.get('fingerprint', 'x')}-{alert.get('startsAt', '')[:19]}"
        await client.start_workflow(
            IncidentInvestigationWorkflow.run,
            alert,
            id=workflow_id,
            task_queue=config.TASK_QUEUE,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,  # a repeat notification for the same alert is a no-op
        )
        STARTED.labels(name).inc()
        log.info("investigation started", extra={"alertname": name, "workflow_id": workflow_id})
        started.append(workflow_id)
    return {"started": started}
