import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx

from agent import config, insights, llm, rca, remediate
from agent.models import RcaReport, TimelineEntry

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
    try:
        report.insights = insights.build(report, ctx)
        report.rca_markdown = report.rca_markdown.rstrip("\n") + insights.section(report.insights)
    except Exception as exc:  # noqa: BLE001  the insights are extra: never lose the report because of them
        log.warning("insights failed: %s", exc)
    report.alert_details = alert_details(alert)
    report.incident = incident_details(report.mode)
    try:
        pr = remediate.maybe_open_pr(ctx, report)
        if pr:
            report.remediation = pr
            log.info("remediation taken", extra={"remediation_type": pr.get("type"), "workload": report.service, "pr_status": pr["status"], "pr_number": pr["pr_number"], "pr_url": pr.get("pr_url", ""), "file": pr.get("file", ""), "old_limit": pr.get("old_limit"), "new_limit": pr.get("new_limit"), "peak_mib": pr.get("peak_mib"), "old_replicas": pr.get("old_replicas"), "new_replicas": pr.get("new_replicas"), "throttle_pct": pr.get("throttle_pct"), "old_cpu_limit": pr.get("old_cpu_limit"), "new_cpu_limit": pr.get("new_cpu_limit")})
            status_label = "merged" if pr["status"] == "merged" else f"PR #{pr['pr_number']} opened"
            change_desc = f"maxReplicas {pr.get('old_max_replicas')} → {pr.get('new_max_replicas')}"
            sync_msg = "Remediation already applied — wait for Argo CD to sync the new maxReplicas"
            report.timeline.append(TimelineEntry(
                time=report.generated_at,
                event=f"remediation {status_label}: {change_desc} ({pr['file']})",
            ))
            if pr["status"] == "merged":
                report.how_to_fix.immediate.insert(0, f"{sync_msg}: {pr['pr_url']}")
            else:
                report.how_to_fix.immediate.insert(0, f"Review and merge the remediation PR: {pr['pr_url']}")
            report.rca_markdown = report.rca_markdown.rstrip("\n") + _remediation_section(pr)
    except Exception as exc:
        log.warning("remediation failed: %s", exc)
    report.rca_markdown = report.rca_markdown.rstrip("\n") + incident_section(report.incident)
    report.rca_markdown = report.rca_markdown.rstrip("\n") + alert_section(report.alert_details)
    return report.model_dump()


def incident_details(mode: str, now: datetime | None = None) -> dict:
    """The investigation: the Argo Workflow that ran it (its name and creation time come in through the environment) and how long it took.
    Argo Workflows keeps no history here, so this is stored in the report."""
    now = now or datetime.now(timezone.utc)
    created = os.environ.get("WORKFLOW_CREATED", "")
    took = None
    try:
        took = max(0, round((now - datetime.fromisoformat(created.replace("Z", "+00:00"))).total_seconds()))
    except ValueError:
        pass
    return {"id": os.environ.get("WORKFLOW_NAME", ""), "started_at": created, "finished_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "took_seconds": took, "analysis_mode": mode}


def _remediation_section(pr: dict) -> str:
    status = pr.get("status", "pr_opened")
    status_note = "auto-merged" if status == "merged" else "opened — requires review"
    change = f"`{pr['file']}` — `maxReplicas` raised from `{pr.get('old_max_replicas')}` to `{pr.get('new_max_replicas')}`"
    action = "Argo CD will apply the new maxReplicas; KEDA will scale out pods and VPA Auto will right-size them."
    return (
        f"\n\n## Remediation taken\n"
        f"A PR was automatically {status_note} while this issue is being investigated.\n"
        f"- **PR:** [{pr['pr_url']}]({pr['pr_url']})\n"
        f"- **Change:** {change}\n"
        f"- **Action:** {action}\n"
    )


def incident_section(d: dict) -> str:
    if not d.get("id"):
        return ""
    took = f", took {d['took_seconds']} s" if d.get("took_seconds") is not None else ""
    return f"\n## Incident\n- **Investigation:** `{d['id']}`, started {d.get('started_at', '')}{took}, analysis {d.get('analysis_mode', '')}\n"


def alert_details(alert: dict) -> dict:
    """What the alert said: the labels (cluster, namespace, pod, ...), the annotations (summary, description), the times and the state."""
    labels = alert.get("labels", {}) or {}
    ann = alert.get("annotations", {}) or {}
    return {
        "alertname": labels.get("alertname", ""),
        "status": alert.get("status", ""),
        "severity": labels.get("severity", ""),
        "cluster": labels.get("cluster", ""),
        "namespace": labels.get("namespace", ""),
        "starts_at": alert.get("startsAt", ""),
        "labels": {k: str(v)[:200] for k, v in labels.items()},
        "annotations": {k: str(v)[:600] for k, v in ann.items() if k in ("summary", "description", "runbook_url")},
        "fingerprint": alert.get("fingerprint", ""),
    }


def alert_section(d: dict) -> str:
    """The same details at the end of rca.md."""
    if not d.get("labels"):
        return ""
    lines = ["", "", "## Alert details", f"- **Alert:** `{d.get('alertname', '')}` ({d.get('status', '')}, severity {d.get('severity') or 'unknown'}), started {d.get('starts_at', '')}"]
    for k in ("summary", "description"):
        if d.get("annotations", {}).get(k):
            lines.append(f"- **{k.capitalize()}:** {d['annotations'][k]}")
    lines.append("- **Labels:** " + ", ".join(f"`{k}={v}`" for k, v in sorted(d["labels"].items()) if k != "alertname"))
    return "\n".join(lines) + "\n"


def store_report(report: dict) -> str:
    """Writes report.json and rca.md under <REPORTS_DIR>/<report id>/ (a volume) and logs one json line (searchable in Loki)."""
    r = RcaReport.model_validate(report)
    try:
        _start = datetime.fromisoformat(r.started_at.replace("Z", "+00:00"))
        _end = datetime.fromisoformat(r.generated_at.replace("Z", "+00:00"))
        investigation_took = max(0, round((_end - _start).total_seconds()))
    except Exception:
        investigation_took = None
    log.info("rca report", extra={"report_id": r.id, "alert": r.alert, "target_service": r.service, "priority": r.priority.level, "layer": r.fault_domain.layer, "root_cause": r.root_cause.category, "confidence": r.root_cause.confidence, "mode": r.mode, "investigation_took": investigation_took, "report": r.model_dump()})
    folder = Path(config.REPORTS_DIR) / r.id
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in (("report.json", json.dumps(report, indent=2)), ("rca.md", r.rca_markdown)):
        tmp = folder / f".{name}.tmp"
        tmp.write_text(text)
        tmp.replace(folder / name)  # a retry overwrites, and a reader never sees a half written file
    return r.id


def notify(report: dict) -> bool:
    """Posts a short summary to a slack compatible webhook when one is configured."""
    if not config.NOTIFY_WEBHOOK_URL:
        return False
    text = (
        f"*{report['priority']['level']}* {report['alert']} on `{report['service']}`: {report['fault_domain']['layer']} level, "
        f"{report['root_cause']['category']} ({report['root_cause']['confidence']})\n{report['summary']}\nRespond {report['priority']['respond_within']}."
    )
    httpx.post(config.NOTIFY_WEBHOOK_URL, json={"text": text}, timeout=10).raise_for_status()
    return True
