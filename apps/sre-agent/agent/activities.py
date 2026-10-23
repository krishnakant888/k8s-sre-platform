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
    """Writes report.json and postmortem.md under <REPORTS_DIR>/<report id>/ (a volume) and logs one json line (searchable in Loki)."""
    r = RcaReport.model_validate(report)
    log.info("rca report", extra={"report_id": r.id, "alert": r.alert, "target_service": r.service, "root_cause": r.root_cause.category, "confidence": r.root_cause.confidence, "mode": r.mode, "report": r.model_dump()})
    folder = Path(config.REPORTS_DIR) / r.id
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in (("report.json", json.dumps(report, indent=2)), ("postmortem.md", r.postmortem_markdown)):
        tmp = folder / f".{name}.tmp"
        tmp.write_text(text)
        tmp.replace(folder / name)  # a retry overwrites, and a reader never sees a half written file
    return r.id


def notify(report: dict) -> bool:
    """Posts a short summary to a slack compatible webhook when one is configured."""
    if not config.NOTIFY_WEBHOOK_URL:
        return False
    text = f"*{report['alert']}* on `{report['service']}`: {report['root_cause']['category']} ({report['root_cause']['confidence']})\n{report['summary']}"
    httpx.post(config.NOTIFY_WEBHOOK_URL, json={"text": text}, timeout=10).raise_for_status()
    return True
