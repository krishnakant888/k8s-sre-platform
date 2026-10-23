import json
import logging
from pathlib import Path

import httpx

from agent import config, llm, rca
from agent.models import RcaReport

log = logging.getLogger("sre-agent")


def collect_evidence(alert: dict) -> dict:
    ctx = rca.gather(alert)
    log.info("evidence gathered", extra={"alert": alert.get("labels", {}).get("alertname"), "target_service": ctx["service"], "evidence_items": len(ctx["evidence"])})
    return ctx


def analyze(alert: dict, ctx: dict) -> dict:
    """Rule based analysis first (always), then the model when a key is configured."""
    report = rca.analyze_rules(alert, ctx)
    if config.ANTHROPIC_API_KEY:
        try:
            report = llm.investigate(alert, ctx, report)
        except Exception as exc:
            log.warning("llm analysis failed, keeping the rule based report: %s", exc)
            report.contributing_factors.append(f"LLM analysis failed ({type(exc).__name__}); this report is rule based")
    return report.model_dump()


def store_report(report: dict) -> str:
    """Keeps the report as a ConfigMap (readable with kubectl) and as one json log line (searchable in Loki)."""
    r = RcaReport.model_validate(report)
    log.info("rca report", extra={"report_id": r.id, "alert": r.alert, "target_service": r.service, "priority": r.priority.level, "layer": r.fault_domain.layer, "root_cause": r.root_cause.category, "confidence": r.root_cause.confidence, "mode": r.mode, "report": r.model_dump()})
    token = Path(f"{config.SA_DIR}/token").read_text()
    body = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": r.id, "namespace": config.REPORT_NAMESPACE, "labels": {"app.kubernetes.io/part-of": "sre-agent", "sre-agent/report": "true"}},
        "data": {"report.json": json.dumps(report, indent=2), "postmortem.md": r.postmortem_markdown},
    }
    resp = httpx.post(
        f"{config.K8S_URL}/api/v1/namespaces/{config.REPORT_NAMESPACE}/configmaps",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        verify=f"{config.SA_DIR}/ca.crt",
        timeout=15,
    )
    if resp.status_code not in (201, 409):  # 409: already stored on a retry
        resp.raise_for_status()
    return r.id


def notify(report: dict) -> bool:
    """Posts a short summary to a slack compatible webhook when one is configured."""
    if not config.NOTIFY_WEBHOOK_URL:
        return False
    text = (
        f"*{report['priority']['level']}* {report['alert']} on `{report['service']}`: {report['fault_domain']['layer']} level, "
        f"{report['root_cause']['category']} ({report['root_cause']['confidence']})\n{report['summary']}\nRespond {report['priority']['respond_within']}."
    httpx.post(config.NOTIFY_WEBHOOK_URL, json={"text": text}, timeout=10).raise_for_status()
    return True
