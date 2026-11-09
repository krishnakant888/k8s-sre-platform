"""Weekly health reports: restart storm, HPA saturation, change risk.

Each report is read-only (no Git mutations) and writes a report.json + rca.md
to REPORTS_DIR using the same layout as capacity reports.
"""
import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx

from agent import config
from agent.models import Evidence, FaultDomain, HowToFix, Impact, Priority, RcaReport, RootCause, TimelineEntry
from agent.config import github_headers as _headers

log = logging.getLogger("sre-agent")

WINDOW = os.getenv("HEALTH_WINDOW", "7d")
CLUSTER = os.getenv("HEALTH_CLUSTER", "platform")
WORKLOADS_ENV = os.getenv(
    "HEALTH_WORKLOADS",
    "orders-api:demo,dummy-shop:sandbox",
)

# Restart storm: flag a workload when weekly restarts exceed this per container
RESTART_STORM_THRESHOLD = int(os.getenv("RESTART_STORM_THRESHOLD", "5"))

# HPA saturation: flag when HPA was at maxReplicas for this fraction of the week
HPA_SATURATION_RATIO = float(os.getenv("HPA_SATURATION_RATIO", "0.10"))

# Change risk: look back this many hours for deploys to correlate with error spikes
CHANGE_RISK_DEPLOY_HOURS = int(os.getenv("CHANGE_RISK_DEPLOY_HOURS", "168"))  # 7 days
# Error-rate spike: flag deploy when post-deploy p95 error rate rises by this factor
CHANGE_RISK_ERROR_FACTOR = float(os.getenv("CHANGE_RISK_ERROR_FACTOR", "2.0"))


# ---------------------------------------------------------------------------
# Mimir helpers
# ---------------------------------------------------------------------------

def _query(q: str) -> float | None:
    try:
        r = httpx.get(f"{config.MIMIR_URL}/api/v1/query", params={"query": q}, timeout=15)
        r.raise_for_status()
        values = r.json().get("data", {}).get("result", [])
        if values:
            v = float(values[0]["value"][1])
            return v if math.isfinite(v) else None
    except Exception as exc:
        log.warning("weekly-health: mimir instant query failed: %s", exc)
    return None


def _query_range(q: str, start: float, end: float, step: int = 300) -> list[tuple[float, float]]:
    try:
        r = httpx.get(
            f"{config.MIMIR_URL}/api/v1/query_range",
            params={"query": q, "start": start, "end": end, "step": step},
            timeout=20,
        )
        r.raise_for_status()
        results = r.json().get("data", {}).get("result", [])
        points = []
        for series in results:
            for ts, val in series.get("values", []):
                try:
                    v = float(val)
                    if math.isfinite(v):
                        points.append((float(ts), v))
                except (ValueError, TypeError):
                    pass
        return points
    except Exception as exc:
        log.warning("weekly-health: mimir range query failed: %s", exc)
    return []


# ---------------------------------------------------------------------------
# GitHub helpers (for change risk deploy listing)
# ---------------------------------------------------------------------------

def _gh_get(path: str, **kw):
    r = httpx.get(f"https://api.github.com/{path}", headers=_headers(), timeout=15, **kw)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Report writer (mirrors scale_down._write_capacity_report)
# ---------------------------------------------------------------------------

def _write_report(report: dict, report_type: str, extra: dict | None = None) -> str:
    report["report_type"] = report_type
    if extra:
        report[report_type] = extra

    markdown = report.get("rca_markdown", "")
    report_id = report["id"]
    folder = Path(config.REPORTS_DIR) / report_id
    folder.mkdir(parents=True, exist_ok=True)
    for name, content in (("report.json", json.dumps(report, indent=2)), ("rca.md", markdown)):
        tmp = folder / f".{name}.tmp"
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(folder / name)
    return report_id


# ---------------------------------------------------------------------------
# Restart storm
# ---------------------------------------------------------------------------

def _restart_storm_for_workload(workload: str, namespace: str, cluster: str) -> dict:
    """Return per-workload restart metrics over WINDOW."""
    sel = f'cluster="{cluster}",namespace="{namespace}",pod=~"{workload}-[a-z0-9]+-[a-z0-9]+"'
    total = _query(f"sum(increase(kube_pod_container_status_restarts_total{{{sel}}}[{WINDOW}]))")
    oom = _query(
        f'sum(increase(kube_pod_container_status_last_terminated_reason{{{sel},reason="OOMKilled"}}[{WINDOW}]))'
    )
    crash = _query(
        f'sum(increase(kube_pod_container_status_last_terminated_reason{{{sel},reason="Error"}}[{WINDOW}]))'
    )
    return {
        "workload": workload,
        "namespace": namespace,
        "cluster": cluster,
        "total_restarts": total,
        "oom_kills": oom,
        "crash_exits": crash,
        "threshold": RESTART_STORM_THRESHOLD,
        "flagged": total is not None and total >= RESTART_STORM_THRESHOLD,
    }


def run_restart_storm_report() -> None:
    workloads = [w.strip() for w in WORKLOADS_ENV.split(",") if w.strip()]
    cluster = CLUSTER
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    results = []
    for item in workloads:
        workload, sep, namespace = item.partition(":")
        if not sep:
            log.warning("weekly-health: invalid HEALTH_WORKLOADS entry %r", item)
            continue
        try:
            results.append(_restart_storm_for_workload(workload, namespace, cluster))
        except Exception as exc:
            log.exception("restart-storm: failed for %s: %s", workload, exc)

    flagged = [r for r in results if r.get("flagged")]
    all_ok = not flagged
    summary = (
        "No workload exceeded the restart threshold this week."
        if all_ok
        else f"{len(flagged)} workload(s) exceeded {RESTART_STORM_THRESHOLD} restarts: "
             + ", ".join(f"{r['workload']} ({int(r['total_restarts'] or 0)} restarts)" for r in flagged)
    )

    evidence = []
    for r in results:
        evidence.append(Evidence(
            source="mimir",
            query=f"kube_pod_container_status_restarts_total[{WINDOW}] for {r['workload']}",
            finding=(
                f"{r['workload']}: {int(r['total_restarts'] or 0)} total restarts "
                f"(OOMKills={int(r['oom_kills'] or 0)}, crashes={int(r['crash_exits'] or 0)})"
            ),
        ))

    markdown = f"# Restart Storm Review\n\n**Window:** {WINDOW}  **Cluster:** {cluster}\n\n## Summary\n\n{summary}\n\n"
    markdown += "## Per-workload\n\n| Workload | Namespace | Restarts | OOMKills | Crashes | Status |\n"
    markdown += "|---|---|---|---|---|---|\n"
    for r in results:
        flag = "⚠ flagged" if r.get("flagged") else "ok"
        markdown += (
            f"| {r['workload']} | {r['namespace']} | {int(r['total_restarts'] or 0)} "
            f"| {int(r['oom_kills'] or 0)} | {int(r['crash_exits'] or 0)} | {flag} |\n"
        )

    report_id = f"rca-restart-storm-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
    report = RcaReport(
        id=report_id,
        alert="RestartStormReview",
        service="platform",
        severity="info" if all_ok else "warning",
        started_at=now_iso,
        generated_at=now_iso,
        mode="rule_based",
        summary=summary,
        what_happened=summary,
        priority=Priority(
            level="P4",
            respond_within="routine review",
            ongoing=False,
            rationale=["Scheduled weekly restart-storm review; no incident raised."],
        ),
        fault_domain=FaultDomain(
            layer="infrastructure",
            component="pod restarts across platform workloads",
            reasoning=[r["workload"] + f": {int(r['total_restarts'] or 0)} restarts" for r in results],
            confidence="high" if all(r["total_restarts"] is not None for r in results) else "low",
        ),
        impact=Impact(
            slo="not evaluated",
            summary=summary,
            availability_replicas=None,
        ),
        timeline=[TimelineEntry(time=now_iso, event="Weekly restart-storm report generated")],
        root_cause=RootCause(
            category="restart_storm_review",
            description=summary,
            confidence="high" if results else "low",
        ),
        evidence=evidence,
        contributing_factors=[],
        how_to_fix=HowToFix(
            immediate=["Investigate flagged workloads for OOMKills (increase memory limit) or crash loops (check logs)."],
            permanent=["Add PodDisruptionBudget, tune memory limits, and add liveness probe backoff."],
            verify=["Watch kube_pod_container_status_restarts_total for 24h after any fix."],
        ),
        observability_gaps=[] if results else ["kube-state-metrics may be unavailable; no data returned."],
        rca_markdown=markdown,
        incident={"workflow": os.getenv("WORKFLOW_NAME", "weekly-health-review")},
    ).model_dump(mode="json")

    rid = _write_report(report, "restart_storm", {"results": results, "flagged_count": len(flagged), "window": WINDOW})
    log.info("restart-storm report stored", extra={"report_id": rid, "flagged": len(flagged)})


# ---------------------------------------------------------------------------
# HPA saturation
# ---------------------------------------------------------------------------

def _hpa_saturation_for_workload(workload: str, namespace: str, cluster: str) -> dict:
    """Fraction of WINDOW where HPA was at maxReplicas."""
    now = time.time()
    window_seconds = 7 * 24 * 3600
    start = now - window_seconds
    sel_hpa = f'cluster="{cluster}",namespace="{namespace}",horizontalpodautoscaler="{workload}"'
    max_replicas = _query(f"kube_horizontalpodautoscaler_spec_max_replicas{{{sel_hpa}}}")
    current_replicas = _query(f"kube_horizontalpodautoscaler_status_current_replicas{{{sel_hpa}}}")

    if max_replicas is None:
        return {"workload": workload, "namespace": namespace, "cluster": cluster,
                "has_hpa": False, "flagged": False, "saturation_ratio": None,
                "max_replicas": None, "current_replicas": None}

    # fraction of time current == max
    at_max_points = _query_range(
        f"kube_horizontalpodautoscaler_status_current_replicas{{{sel_hpa}}} "
        f">= kube_horizontalpodautoscaler_spec_max_replicas{{{sel_hpa}}}",
        start, now, step=300,
    )
    total_points = _query_range(
        f"kube_horizontalpodautoscaler_status_current_replicas{{{sel_hpa}}}",
        start, now, step=300,
    )
    ratio = len(at_max_points) / len(total_points) if total_points else None
    return {
        "workload": workload,
        "namespace": namespace,
        "cluster": cluster,
        "has_hpa": True,
        "max_replicas": int(max_replicas),
        "current_replicas": int(current_replicas) if current_replicas is not None else None,
        "saturation_ratio": ratio,
        "threshold": HPA_SATURATION_RATIO,
        "flagged": ratio is not None and ratio >= HPA_SATURATION_RATIO,
    }


def run_hpa_saturation_report() -> None:
    workloads = [w.strip() for w in WORKLOADS_ENV.split(",") if w.strip()]
    cluster = CLUSTER
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    results = []
    for item in workloads:
        workload, sep, namespace = item.partition(":")
        if not sep:
            continue
        try:
            results.append(_hpa_saturation_for_workload(workload, namespace, cluster))
        except Exception as exc:
            log.exception("hpa-saturation: failed for %s: %s", workload, exc)

    hpa_results = [r for r in results if r.get("has_hpa")]
    flagged = [r for r in hpa_results if r.get("flagged")]
    all_ok = not flagged

    summary = (
        "No HPA was at maxReplicas for more than "
        f"{int(HPA_SATURATION_RATIO * 100)}% of the week."
        if all_ok
        else f"{len(flagged)} HPA(s) were ceiling-bound: "
             + ", ".join(
                 f"{r['workload']} ({int((r['saturation_ratio'] or 0) * 100)}% of week at max={r['max_replicas']})"
                 for r in flagged
             )
    )

    evidence = []
    for r in hpa_results:
        evidence.append(Evidence(
            source="mimir",
            query=f"kube_horizontalpodautoscaler_status_current_replicas[{WINDOW}] for {r['workload']}",
            finding=(
                f"{r['workload']}: saturation {int((r['saturation_ratio'] or 0) * 100)}% "
                f"(max={r['max_replicas']}, current={r['current_replicas']})"
            ),
        ))

    markdown = f"# HPA Saturation Review\n\n**Window:** {WINDOW}  **Cluster:** {cluster}\n\n## Summary\n\n{summary}\n\n"
    markdown += "## Per-workload\n\n| Workload | Has HPA | maxReplicas | Saturation | Status |\n"
    markdown += "|---|---|---|---|---|\n"
    for r in results:
        if not r.get("has_hpa"):
            markdown += f"| {r['workload']} | no | — | — | no HPA |\n"
        else:
            pct = f"{int((r['saturation_ratio'] or 0) * 100)}%" if r["saturation_ratio"] is not None else "n/a"
            flag = "⚠ ceiling-bound" if r.get("flagged") else "ok"
            markdown += f"| {r['workload']} | yes | {r['max_replicas']} | {pct} | {flag} |\n"

    report_id = f"rca-hpa-saturation-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
    report = RcaReport(
        id=report_id,
        alert="HpaSaturationReview",
        service="platform",
        severity="info" if all_ok else "warning",
        started_at=now_iso,
        generated_at=now_iso,
        mode="rule_based",
        summary=summary,
        what_happened=summary,
        priority=Priority(
            level="P4",
            respond_within="routine review",
            ongoing=False,
            rationale=["Scheduled weekly HPA saturation review; no incident raised."],
        ),
        fault_domain=FaultDomain(
            layer="infrastructure",
            component="HPA maxReplicas ceiling across platform workloads",
            reasoning=[f"{r['workload']}: {int((r['saturation_ratio'] or 0)*100)}% of week at ceiling" for r in hpa_results],
            confidence="high" if hpa_results else "low",
        ),
        impact=Impact(
            slo="not evaluated",
            summary=summary,
            availability_replicas=None,
        ),
        timeline=[TimelineEntry(time=now_iso, event="Weekly HPA saturation report generated")],
        root_cause=RootCause(
            category="hpa_saturation_review",
            description=summary,
            confidence="high" if hpa_results else "low",
        ),
        evidence=evidence,
        contributing_factors=[],
        how_to_fix=HowToFix(
            immediate=["Raise maxReplicas in the HPA manifest for flagged workloads."],
            permanent=["Set maxReplicas based on load-test headroom + 30% buffer; review monthly."],
            verify=["Confirm saturation_ratio drops below threshold after the next weekly report."],
        ),
        observability_gaps=[] if hpa_results else ["No HPAs found via kube-state-metrics; check kube-state-metrics is running."],
        rca_markdown=markdown,
        incident={"workflow": os.getenv("WORKFLOW_NAME", "weekly-health-review")},
    ).model_dump(mode="json")

    rid = _write_report(report, "hpa_saturation", {"results": results, "flagged_count": len(flagged), "window": WINDOW})
    log.info("hpa-saturation report stored", extra={"report_id": rid, "flagged": len(flagged)})


# ---------------------------------------------------------------------------
# Change risk
# ---------------------------------------------------------------------------

def _get_recent_merges(repo: str, hours: int) -> list[dict]:
    """Return merged PRs from the last N hours."""
    since = datetime.fromtimestamp(time.time() - hours * 3600, tz=timezone.utc).isoformat(timespec="seconds")
    try:
        pulls = _gh_get(f"repos/{repo}/pulls", params={"state": "closed", "sort": "updated",
                                                        "direction": "desc", "per_page": 50})
        merged = []
        for p in pulls:
            if not p.get("merged_at"):
                continue
            merged_ts = datetime.fromisoformat(p["merged_at"].replace("Z", "+00:00")).timestamp()
            if merged_ts < time.time() - hours * 3600:
                continue
            merged.append({
                "pr_number": p["number"],
                "title": p.get("title", ""),
                "merged_at": p["merged_at"],
                "merged_at_ts": merged_ts,
                "url": p.get("html_url", ""),
                "author": (p.get("user") or {}).get("login", ""),
            })
        return merged
    except Exception as exc:
        log.warning("change-risk: failed to fetch PRs: %s", exc)
        return []


def _error_rate_around(workload: str, namespace: str, cluster: str,
                       deploy_ts: float, pre_minutes: int = 30, post_minutes: int = 30) -> dict:
    """Return mean error rate in the pre and post windows around a deploy."""
    sel = f'cluster="{cluster}",namespace="{namespace}",job=~"{workload}.*"'
    pre_start, pre_end = deploy_ts - pre_minutes * 60, deploy_ts
    post_start, post_end = deploy_ts, deploy_ts + post_minutes * 60

    def _mean_error_rate(start: float, end: float) -> float | None:
        pts = _query_range(
            f'sum(rate(http_requests_total{{{sel},status=~"5.."}}[2m])) / '
            f'sum(rate(http_requests_total{{{sel}}}[2m]))',
            start, end, step=60,
        )
        if not pts:
            return None
        vals = [v for _, v in pts if math.isfinite(v)]
        return sum(vals) / len(vals) if vals else None

    pre = _mean_error_rate(pre_start, pre_end)
    post = _mean_error_rate(post_start, post_end)
    ratio = (post / pre) if (pre is not None and post is not None and pre > 0) else None
    return {"pre_error_rate": pre, "post_error_rate": post,
            "ratio": ratio, "flagged": ratio is not None and ratio >= CHANGE_RISK_ERROR_FACTOR}


def run_change_risk_report() -> None:
    workloads = [w.strip() for w in WORKLOADS_ENV.split(",") if w.strip()]
    cluster = CLUSTER
    repo = config.GITOPS_REPO
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    merges = _get_recent_merges(repo, CHANGE_RISK_DEPLOY_HOURS)
    findings = []

    for item in workloads:
        workload, sep, namespace = item.partition(":")
        if not sep:
            continue
        for merge in merges:
            # heuristic: PR title or branch contains workload name
            if workload.lower() not in merge["title"].lower():
                continue
            try:
                er = _error_rate_around(workload, namespace, cluster, merge["merged_at_ts"])
                findings.append({
                    "workload": workload,
                    "namespace": namespace,
                    "pr_number": merge["pr_number"],
                    "pr_title": merge["title"],
                    "pr_url": merge["url"],
                    "merged_at": merge["merged_at"],
                    "author": merge["author"],
                    **er,
                })
            except Exception as exc:
                log.exception("change-risk: error rate check failed for %s PR#%s: %s",
                              workload, merge["pr_number"], exc)

    flagged = [f for f in findings if f.get("flagged")]
    all_ok = not flagged
    no_data = not findings

    if no_data:
        summary = f"No deployments matched platform workloads in the last {CHANGE_RISK_DEPLOY_HOURS // 24} days."
    elif all_ok:
        summary = f"{len(findings)} deploy(s) checked; none caused a detectable error-rate spike."
    else:
        summary = (
            f"{len(flagged)} deploy(s) correlated with an error-rate spike: "
            + ", ".join(
                f"PR#{f['pr_number']} {f['workload']} "
                f"(×{f['ratio']:.1f} error rate)" for f in flagged
            )
        )

    evidence = []
    for f in findings:
        pre = f"pre={f['pre_error_rate']:.4f}" if f["pre_error_rate"] is not None else "pre=n/a"
        post = f"post={f['post_error_rate']:.4f}" if f["post_error_rate"] is not None else "post=n/a"
        evidence.append(Evidence(
            source="mimir",
            query=f"http_requests_total 5xx ratio ±30m around PR#{f['pr_number']} merge for {f['workload']}",
            finding=f"PR#{f['pr_number']} '{f['pr_title']}' merged {f['merged_at']}: {pre} → {post}"
                    + (f" (×{f['ratio']:.1f} — ⚠ spike)" if f.get("flagged") else ""),
        ))

    markdown = (
        f"# Change Risk Report\n\n"
        f"**Window:** last {CHANGE_RISK_DEPLOY_HOURS // 24} days  "
        f"**Cluster:** {cluster}  "
        f"**Spike threshold:** ×{CHANGE_RISK_ERROR_FACTOR}\n\n"
        f"## Summary\n\n{summary}\n\n"
    )
    if findings:
        markdown += "## Deploys checked\n\n| PR | Workload | Merged | Pre error rate | Post error rate | Ratio | Status |\n"
        markdown += "|---|---|---|---|---|---|---|\n"
        for f in findings:
            pre = f"{f['pre_error_rate']:.4f}" if f["pre_error_rate"] is not None else "n/a"
            post = f"{f['post_error_rate']:.4f}" if f["post_error_rate"] is not None else "n/a"
            ratio = f"×{f['ratio']:.1f}" if f["ratio"] is not None else "n/a"
            flag = "⚠ spike" if f.get("flagged") else "ok"
            markdown += f"| [#{f['pr_number']}]({f['pr_url']}) | {f['workload']} | {f['merged_at']} | {pre} | {post} | {ratio} | {flag} |\n"

    report_id = f"rca-change-risk-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
    report = RcaReport(
        id=report_id,
        alert="ChangeRiskReview",
        service="platform",
        severity="info" if all_ok or no_data else "warning",
        started_at=now_iso,
        generated_at=now_iso,
        mode="rule_based",
        summary=summary,
        what_happened=summary,
        priority=Priority(
            level="P4",
            respond_within="routine review",
            ongoing=False,
            rationale=["Scheduled weekly change-risk review; no incident raised."],
        ),
        fault_domain=FaultDomain(
            layer="application",
            component="deploy/error-rate correlation across platform workloads",
            reasoning=[f"PR#{f['pr_number']} {f['workload']}: ratio={f['ratio']}" for f in flagged] or ["No spikes detected."],
            confidence="medium" if findings else "low",
        ),
        impact=Impact(
            slo="not evaluated",
            summary=summary,
            availability_replicas=None,
        ),
        timeline=[TimelineEntry(time=now_iso, event="Weekly change-risk report generated")],
        root_cause=RootCause(
            category="change_risk_review",
            description=summary,
            confidence="medium" if findings else "low",
        ),
        evidence=evidence,
        contributing_factors=[],
        how_to_fix=HowToFix(
            immediate=["Review flagged PRs; consider rollback if error rate remains elevated."],
            permanent=["Add canary deployments or feature flags for high-risk changes; set error-rate SLO alerts."],
            verify=["Check http_requests_total 5xx ratio for 24h after any rollback or fix."],
        ),
        observability_gaps=(
            ["http_requests_total with status label is not present; error-rate correlation unavailable."]
            if findings and all(f["pre_error_rate"] is None for f in findings) else []
        ),
        rca_markdown=markdown,
        incident={"workflow": os.getenv("WORKFLOW_NAME", "weekly-health-review")},
    ).model_dump(mode="json")

    rid = _write_report(report, "change_risk", {
        "findings": findings, "flagged_count": len(flagged),
        "deploy_hours": CHANGE_RISK_DEPLOY_HOURS, "error_factor": CHANGE_RISK_ERROR_FACTOR,
    })
    log.info("change-risk report stored", extra={"report_id": rid, "flagged": len(flagged)})


# ---------------------------------------------------------------------------
# Entry point: run all three
# ---------------------------------------------------------------------------

def run_weekly_health_review() -> None:
    errors = []
    for name, fn in (
        ("restart-storm", run_restart_storm_report),
        ("hpa-saturation", run_hpa_saturation_report),
        ("change-risk", run_change_risk_report),
    ):
        try:
            fn()
        except Exception as exc:
            log.exception("weekly-health: %s failed: %s", name, exc)
            errors.append(name)
    if errors:
        raise RuntimeError("weekly health review failed for: " + ", ".join(errors))
