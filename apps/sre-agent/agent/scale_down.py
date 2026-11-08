"""Daily, guarded capacity reductions based on a full day of Mimir metrics.

Changes go through a PR and are auto-merged when repository rules allow it.
Missing data, active alerts, recent deployments, restarts, or an unstable
deployment all fail closed.
"""
import base64
import json
import logging
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from agent import config
from agent.models import Evidence, FaultDomain, HowToFix, Impact, Priority, RcaReport, RootCause, TimelineEntry
from agent.remediate import _headers

log = logging.getLogger("sre-agent")
WINDOW = os.getenv("SCALE_DOWN_WINDOW", "24h")
CPU_P95_MAX = 0.50
CPU_THROTTLE_MAX = 0.01
MEMORY_P99_MAX = 0.60
MIN_COVERAGE_SECONDS = int(os.getenv("SCALE_DOWN_MIN_COVERAGE_SECONDS", str(24 * 60 * 60)))
COOLDOWN_SECONDS = int(os.getenv("SCALE_DOWN_COOLDOWN_SECONDS", str(24 * 60 * 60)))
POST_MERGE_WINDOW = os.getenv("SCALE_DOWN_POST_MERGE_WINDOW", "15m")
POST_MERGE_WAIT_SECONDS = int(os.getenv("SCALE_DOWN_POST_MERGE_WAIT_SECONDS", "900"))
POST_MERGE_MIN_COVERAGE_SECONDS = int(os.getenv("SCALE_DOWN_POST_MERGE_MIN_COVERAGE_SECONDS", "720"))
WEEKLY_FLOOR_WINDOW = os.getenv("WEEKLY_FLOOR_WINDOW", "7d")
WEEKLY_FLOOR_MIN_COVERAGE_SECONDS = int(os.getenv("WEEKLY_FLOOR_MIN_COVERAGE_SECONDS", str(6 * 24 * 60 * 60)))
GRAFANA_URL = getattr(config, "GRAFANA_URL", "http://grafana.observability.svc.cluster.local:3000")
GRAFANA_API_TOKEN = getattr(config, "GRAFANA_API_TOKEN", "")


def _duration_seconds(value: str) -> float | None:
    if not re.fullmatch(r"(?:\d+(?:ms|s|m|h|d|w|y))+", value):
        return None
    units = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400,
             "w": 604800, "y": 31536000}
    return sum(int(amount) * units[unit] for amount, unit in re.findall(r"(\d+)(ms|s|m|h|d|w|y)", value))


def _subquery_step(window: str, short_step: str, long_step: str) -> str:
    seconds = _duration_seconds(window)
    return short_step if seconds is not None and seconds < 300 else long_step


def _coverage_window(window: str, step: str) -> str:
    window_seconds = _duration_seconds(window)
    step_seconds = _duration_seconds(step)
    if window_seconds is None or step_seconds is None:
        return f"{window}{step}"
    return f"{window_seconds + step_seconds:g}s"


CPU_SUBQUERY_STEP = _subquery_step(WINDOW, "15s", "5m")
COVERAGE_SUBQUERY_STEP = _subquery_step(WINDOW, "15s", "1m")


def _get(path: str, **kw) -> Any:
    r = httpx.get(f"https://api.github.com/{path}", headers=_headers(), timeout=15, **kw)
    r.raise_for_status()
    return r.json()


def _post(path: str, body: dict) -> Any:
    r = httpx.post(f"https://api.github.com/{path}", headers=_headers(), json=body, timeout=15)
    r.raise_for_status()
    return r.json()


def _put(path: str, body: dict) -> Any:
    r = httpx.put(f"https://api.github.com/{path}", headers=_headers(), json=body, timeout=15)
    r.raise_for_status()
    return r.json()


def _query(query: str) -> float | None:
    try:
        r = httpx.get(f"{config.MIMIR_URL}/api/v1/query", params={"query": query}, timeout=12)
        r.raise_for_status()
        values = r.json().get("data", {}).get("result", [])
        if values:
            value = float(values[0]["value"][1])
            return value if math.isfinite(value) else None
    except Exception as exc:
        log.warning("scale-down: Mimir query failed: %s", exc)
    return None


def _selectors(cluster: str, namespace: str, workload: str) -> str:
    return (f'cluster="{cluster}",namespace="{namespace}",'
            f'pod=~"{workload}-[a-z0-9]+-[a-z0-9]+",container!=""')


def _metrics(cluster: str, namespace: str, workload: str, window: str | None = None) -> dict[str, float | None]:
    sel = _selectors(cluster, namespace, workload)
    window = window or WINDOW
    cpu_step = _subquery_step(window, "15s", "5m")
    coverage_step = _subquery_step(window, "15s", "1m")
    cpu = f'container_cpu_usage_seconds_total{{{sel}}}'
    memory = f'container_memory_working_set_bytes{{{sel}}}'
    coverage_window = _coverage_window(window, coverage_step)
    return {
        "cpu_p95_cores": _query(
            f"max(quantile_over_time(0.95, rate({cpu}[5m])[{window}:{cpu_step}]))"
        ),
        "cpu_throttle": _query(
            f"max(max_over_time((rate(container_cpu_cfs_throttled_periods_total{{{sel}}}[5m]) / "
            f"rate(container_cpu_cfs_periods_total{{{sel}}}[5m]))[{window}:{cpu_step}]))"
        ),
        "memory_p99_bytes": _query(
            f"max(quantile_over_time(0.99, {memory}[{window}]))"
        ),
        "cpu_coverage_seconds": _query(
            f"min(time() - min_over_time((timestamp(rate({cpu}[5m])))[{coverage_window}:{coverage_step}]))"
        ),
        "memory_coverage_seconds": _query(
            f"min(time() - min_over_time((timestamp({memory}))[{coverage_window}:{coverage_step}]))"
        ),
        "restarts_in_window": _query(
            f"sum(increase(kube_pod_container_status_restarts_total{{{sel}}}[{window}]))"
        ),
        "available_replicas": _query(
            f'min(kube_deployment_status_replicas_available{{cluster="{cluster}",'
            f'namespace="{namespace}",deployment="{workload}"}})'
        ),
    }


def _verify_post_merge(workload: str, namespace: str, cluster: str,
                       expected_replicas: int, expected_memory_mib: int | None,
                       expected_cpu_millicores: int | None) -> dict:
    """Wait for and collect sustained health evidence after a capacity PR merges."""
    wait_seconds = max(0, POST_MERGE_WAIT_SECONDS)
    if wait_seconds:
        time.sleep(wait_seconds)
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    evidence = {"status": "inconclusive", "checked_at": checked_at,
                "window": POST_MERGE_WINDOW, "wait_seconds": wait_seconds,
                "expected_replicas": expected_replicas,
                "expected_memory_limit_mib": expected_memory_mib,
                "expected_cpu_limit_millicores": expected_cpu_millicores,
                "checks": [], "metrics": {}}
    try:
        metrics = _metrics(cluster, namespace, workload, POST_MERGE_WINDOW)
        evidence["metrics"] = metrics
        coverage_ok = all(metrics.get(key) is not None and metrics[key] >= POST_MERGE_MIN_COVERAGE_SECONDS
                          for key in ("cpu_coverage_seconds", "memory_coverage_seconds"))
        available = metrics.get("available_replicas")
        replicas_ok = available is not None and available >= expected_replicas
        restarts = metrics.get("restarts_in_window")
        restarts_ok = restarts is not None and restarts == 0
        cpu = metrics.get("cpu_p95_cores")
        cpu_ok = cpu is not None and (expected_cpu_millicores is None or
                                     cpu <= expected_cpu_millicores / 1000 * 0.8)
        throttle = metrics.get("cpu_throttle")
        throttle_ok = throttle is not None and throttle < CPU_THROTTLE_MAX
        memory = metrics.get("memory_p99_bytes")
        memory_ok = memory is not None and (expected_memory_mib is None or
                                           memory <= expected_memory_mib * 1024 * 1024 * 0.8)
        try:
            alert_ok = not _active_alert(workload, namespace)
            alert_value = "none active" if alert_ok else "matching alert active"
        except Exception as exc:
            alert_ok, alert_value = False, f"alert state unavailable: {exc}"
        checks = [
            ("metric coverage", coverage_ok, f"minimum {POST_MERGE_MIN_COVERAGE_SECONDS}s"),
            ("available replicas", replicas_ok, f"{available} available; expected at least {expected_replicas}"),
            ("restarts", restarts_ok, f"{restarts} in {POST_MERGE_WINDOW}"),
            ("CPU usage", cpu_ok, f"{cpu} cores P95; expected limit {expected_cpu_millicores}m"),
            ("CPU throttling", throttle_ok, f"{throttle} ratio"),
            ("memory usage", memory_ok, f"{memory} bytes P99; expected limit {expected_memory_mib}MiB"),
            ("matching alerts", alert_ok, alert_value),
        ]
        evidence["checks"] = [{"name": name, "passed": passed, "evidence": value}
                              for name, passed, value in checks]
        if not coverage_ok or alert_value.startswith("alert state unavailable") or any(metrics.get(key) is None for key in
                                  ("available_replicas", "restarts_in_window", "cpu_p95_cores",
                                   "cpu_throttle", "memory_p99_bytes")):
            evidence["status"] = "inconclusive"
        else:
            evidence["status"] = "healthy" if all(passed for _, passed, _ in checks) else "needs_attention"
    except Exception as exc:
        evidence["error"] = str(exc)
        evidence["checks"] = [{"name": "post-merge health snapshot", "passed": False,
                               "evidence": f"verification failed: {exc}"}]
    return evidence


def _active_alert(workload: str, namespace: str) -> bool:
    if not GRAFANA_API_TOKEN:
        raise RuntimeError("GRAFANA_API_TOKEN is missing; refusing scale-down")
    r = httpx.get(
        f"{GRAFANA_URL}/api/alertmanager/grafana/api/v2/alerts/groups",
        headers={"Authorization": f"Bearer {GRAFANA_API_TOKEN}"}, timeout=15,
    )
    r.raise_for_status()
    groups = r.json()
    for group in groups:
        for alert in group.get("alerts", []):
            labels = alert.get("labels", {})
            if labels.get("service") == workload:
                return True
            if labels.get("namespace") == namespace and (
                labels.get("pod", "").startswith(workload + "-")
                or labels.get("deployment") == workload
            ):
                return True
    return False


def _write_capacity_report(review: dict) -> str:
    """Write a capacity review in the same report format and volume as incident RCAs."""
    now = datetime.now(timezone.utc)
    generated_at = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    started_at = review.get("started_at", generated_at)
    workload = review["workload"]
    namespace = review["namespace"]
    decision = review.get("decision", "skipped")
    metrics = review.get("metrics", {})
    capacity = {
        key: value for key, value in review.items()
        if key not in {"started_at", "workload", "namespace", "cluster", "metrics"}
    }
    capacity.update({"cluster": review.get("cluster"), "namespace": namespace,
                     "workload": workload, "window": review.get("window", WINDOW), "metrics": metrics})
    report_id = f"rca-capacity-{now:%Y%m%dT%H%M%SZ}-{workload}-{uuid4().hex[:8]}"
    proposed = review.get("proposed_change")
    post_merge = review.get("post_merge")
    review_type = review.get("review_type", "daily")
    report_title = "Weekly replica-floor review" if review_type == "weekly_floor" else "Capacity plan"
    displayed_decision = "recommendation only" if review_type == "weekly_floor" else decision
    if review_type == "weekly_floor":
        summary = (f"Weekly replica-floor review for {workload}: {proposed}. "
                   f"Recommendation only; no scaling change or pull request was made. {review.get('reason', '')}")
    elif decision == "proposal":
        summary = f"Capacity review for {workload} proposed: {proposed}."
        if review.get("pr_url"):
            summary += f" PR #{review.get('pr_number')} was {'merged' if review.get('merged') else 'opened'}: {review['pr_url']}"
        if review.get("merged"):
            status = (post_merge or {}).get("status", "inconclusive")
            summary += f" Post-merge health verification: {status.replace('_', ' ')}."
        else:
            summary += " Post-merge health verification has not run because the PR is still open."
    elif decision == "error":
        summary = f"Capacity review for {workload} failed: {review.get('reason', 'unknown error')}"
    else:
        summary = f"Capacity review for {workload} made no change: {review.get('reason', 'safety checks did not support a change')}"

    def fmt(key: str, suffix: str = "") -> str:
        value = metrics.get(key)
        return "unavailable" if value is None else f"{value:.3f}{suffix}"

    memory_mib = metrics.get("memory_p99_bytes")
    memory_text = "unavailable" if memory_mib is None else f"{memory_mib / 1024 / 1024:.1f} MiB"
    evidence_text = (
        f"CPU P95 {fmt('cpu_p95_cores', ' cores')}; CPU throttle {fmt('cpu_throttle', ' ratio')}; "
        f"memory P99 {memory_text}; CPU coverage {fmt('cpu_coverage_seconds', ' s')}; "
        f"memory coverage {fmt('memory_coverage_seconds', ' s')}; restarts in window {fmt('restarts_in_window')}; "
        f"available replicas {fmt('available_replicas')}."
    )
    post_merge_text = ""
    if review.get("merged"):
        verification = post_merge or {"status": "inconclusive", "checks": []}
        check_lines = "\n".join(
            f"- {'PASS' if check.get('passed') else 'CHECK'} **{check.get('name')}:** {check.get('evidence')}"
            for check in verification.get("checks", [])
        ) or "- No post-merge checks were recorded."
        post_merge_text = (
            "\n\n## Post-merge verification\n\n"
            f"**Status:** {verification.get('status', 'inconclusive')}  \n"
            f"**PR merged at:** {review.get('merged_at', 'not recorded')}  \n"
            f"**Checked at:** {verification.get('checked_at', 'not recorded')}  \n"
            f"**Observation window:** {verification.get('window', POST_MERGE_WINDOW)}  \n"
            f"**Wait after merge:** {verification.get('wait_seconds', 0)} seconds\n\n"
            f"{check_lines}\n\n"
            "A healthy result requires metric coverage, enough available replicas, no recent restarts or matching alerts, "
            "CPU P95 and memory P99 below 80% of the proposed limits, and CPU throttling below its threshold."
        )
    markdown = (
        f"# {report_title}: {workload}\n\n"
        f"**Decision:** {displayed_decision}  \n"
        f"**Window:** {review.get('window', WINDOW)}  \n"
        f"**Configured minimum floor:** {review.get('configured_floor', 'not evaluated')}  \n"
        f"**Candidate replica floor:** {review.get('candidate_floor', 'not evaluated')}  \n"
        f"**Proposed change:** {proposed or 'none'}  \n"
        f"**Reason:** {review.get('reason', 'metrics satisfied the configured safety checks')}\n\n"
        "## Metrics\n\n"
        f"{evidence_text}\n\n"
        ("## Weekly recommendation\n\n" if review_type == "weekly_floor" else "## Proposal\n\n") +
        f"{summary}\n\n"
        ("Weekly reports are advisory only; missing or insufficient evidence withholds a floor recommendation. No replica change is performed."
         if review_type == "weekly_floor" else
         "Scale-down requires adequate metric coverage, no active matching alert, no recent restarts, and all desired replicas available.") +
        f"{post_merge_text}"
    )
    report_evidence = [Evidence(source="mimir", query=f"SCALE_DOWN_WINDOW={review.get('window', WINDOW)} for {review.get('cluster', 'platform')}",
                                finding=evidence_text)]
    if review.get("merged") and post_merge:
        post_checks = "; ".join(f"{item['name']}: {'passed' if item['passed'] else 'not passed'} ({item['evidence']})"
                                 for item in post_merge.get("checks", []))
        report_evidence.append(Evidence(source="mimir and Grafana", query=f"post-merge {post_merge.get('window', POST_MERGE_WINDOW)} health snapshot",
                                        finding=f"Status: {post_merge.get('status')}. {post_checks}"))
    report = RcaReport(
        id=report_id,
        alert="CapacityReview",
        service=workload,
        severity="info",
        started_at=started_at,
        generated_at=generated_at,
        mode="rule_based",
        summary=summary,
        what_happened=summary,
        priority=Priority(level="P4", respond_within="routine review", ongoing=False,
                          rationale=["Scheduled capacity review; no incident was raised."]),
        fault_domain=FaultDomain(layer="infrastructure", component=f"{workload} capacity",
                                 reasoning=[review.get("reason", "Capacity metrics reviewed.")], confidence="medium"),
        impact=Impact(slo="not evaluated", summary=(
                          f"Capacity PR merged; post-merge health verification is {(post_merge or {}).get('status', 'inconclusive')}."
                          if review.get("merged") else
                          "Weekly replica-floor recommendation only; no scaling change was applied."
                          if review_type == "weekly_floor" else
                          "Capacity review; service health was checked before any proposal."),
                      availability_replicas=metrics.get("available_replicas")),
        timeline=[TimelineEntry(time=started_at, event="Capacity review started"),
                  TimelineEntry(time=generated_at, event=summary)],
        root_cause=RootCause(category="capacity_review", description=summary, confidence="medium"),
        evidence=report_evidence,
        contributing_factors=[],
        how_to_fix=HowToFix(immediate=["No immediate action required."],
                            permanent=["Keep the workload at the reviewed replica floor and resource settings."],
                            verify=["Confirm Argo CD is synced and all desired replicas are available."]),
        observability_gaps=[],
        rca_markdown=markdown,
        remediation={"pr_number": review.get("pr_number"), "pr_url": review.get("pr_url"),
                     "merged": review.get("merged", False), "proposed_change": proposed,
                     "merged_at": review.get("merged_at"),
                     "post_merge_verification": post_merge},
        incident={"workflow": os.getenv("WORKFLOW_NAME", "capacity-review")},
    ).model_dump(mode="json")
    report["report_type"] = "capacity"
    report["capacity"] = capacity

    folder = Path(config.REPORTS_DIR) / report_id
    folder.mkdir(parents=True, exist_ok=True)
    for name, content in (("report.json", json.dumps(report, indent=2)), ("rca.md", markdown)):
        temporary = folder / f".{name}.tmp"
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(folder / name)
    log.info("capacity report stored", extra={"report_id": report_id, "workload": workload,
                                               "decision": decision, "candidate_floor": review.get("candidate_floor")})
    return report_id


def _quantity_values(content: str, section: str) -> dict[str, str]:
    """Read cpu/memory from the first requests or limits block in a deployment manifest."""
    match = re.search(rf"(?m)^\s*{section}:\s*\{{([^}}]*)\}}", content)
    if match:
        block = match.group(1)
    else:
        match = re.search(rf"(?ms)^\s*{section}:\s*\n((?:\s+[^\n]+\n?)*)", content)
        if not match:
            return {}
        block = match.group(1)
    return {key: value for key, value in re.findall(r"\b(cpu|memory):\s*([^\s,}]+)", block)}


def _cpu_millicores(value: str | None) -> int | None:
    if not value:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(m?)", value)
    if not m:
        return None
    return int(float(m.group(1)) * (1 if m.group(2) else 1000))


def _memory_mib(value: str | None) -> int | None:
    if not value:
        return None
    m = re.fullmatch(r"(\d+)(Mi|Gi)", value)
    if not m:
        return None
    n = int(m.group(1))
    return n * 1024 if m.group(2) == "Gi" else n


def _replace_replicas(content: str, old: int, new: int) -> str | None:
    updated, count = re.subn(r"(\breplicas:\s*)" + str(old) + r"\b", rf"\g<1>{new}", content, count=1)
    return updated if count and updated != content else None


def _replace_resource(content: str, section: str, resource: str, old: str, new: str) -> str | None:
    inline = rf"({section}:\s*\{{[^}}]*?\b{resource}:\s*){re.escape(old)}"
    updated, count = re.subn(inline, lambda m: m.group(1) + new, content, count=1)
    if count:
        return updated
    multiline = rf"(?m)(^\s*{section}:\s*\n(?:\s+[^\n]*\n)*?\s+{resource}:\s*){re.escape(old)}"
    updated, count = re.subn(multiline, lambda m: m.group(1) + new, content, count=1)
    return updated if count else None


def _load_manifest(repo: str, branch: str, path: str) -> tuple[str, str] | None:
    try:
        data = _get(f"repos/{repo}/contents/{path}", params={"ref": branch})
        return base64.b64decode(data["content"]).decode(), data["sha"]
    except Exception as exc:
        log.info("scale-down: cannot read %s: %s", path, exc)
        return None


def _recent_commit(repo: str, path: str) -> bool:
    commits = _get(f"repos/{repo}/commits", params={"path": path, "per_page": 1})
    if not commits:
        return True
    committed = commits[0]["commit"]["committer"]["date"]
    at = datetime.fromisoformat(committed.replace("Z", "+00:00")).timestamp()
    return time.time() - at < COOLDOWN_SECONDS


def _has_open_recommendation(repo: str, workload: str, branch: str) -> bool:
    pulls = _get(f"repos/{repo}/pulls", params={"state": "open", "per_page": 100})
    prefix = f"recommend/scale-down-{workload}-"
    return any(p.get("base", {}).get("ref") == branch and p.get("head", {}).get("ref", "").startswith(prefix)
               and p.get("head", {}).get("repo", {}).get("full_name") == repo for p in pulls)


def _open_recommendation(repo: str, branch: str, head_sha: str, path: str, blob_sha: str,
                         content: str, workload: str, operation: str, evidence: dict) -> dict:
    ref = f"recommend/scale-down-{workload}-{int(time.time())}"
    _post(f"repos/{repo}/git/refs", {"ref": f"refs/heads/{ref}", "sha": head_sha})
    _put(f"repos/{repo}/contents/{path}", {
        "message": f"chore(capacity): recommend scale-down for {workload}",
        "content": base64.b64encode(content.encode()).decode(), "sha": blob_sha, "branch": ref,
    })
    candidate_floor_line = (
        f"**Candidate replica floor:** `{evidence['candidate_floor']}`\n"
        if "candidate_floor" in evidence else ""
    )
    body = "".join([
        "## Scale-down recommendation\n\n"
        f"**Workload:** `{workload}`\n"
        f"**Proposed change:** {operation}\n",
        candidate_floor_line,
        f"**Evidence window:** {WINDOW}; CPU P95 `{evidence['cpu_pct']:.0f}%` of capacity, "
        f"CPU throttling `{evidence['throttle_pct']:.2f}%`, memory P99 `{evidence['memory_pct']:.0f}%` of limit.\n\n"
        "The workload had no active matching alerts, no restarts in the evidence window, and all replicas were available. "
        "A replica proposal targets the configured floor when the evidence supports it; resource limits reduce one step per review. "
        "The agent attempts to merge it immediately; "
        "repository branch protection may leave it open for review."
    ])
    pr = _post(f"repos/{repo}/pulls", {
        "title": f"chore(capacity): scale down {workload}",
        "body": body, "head": ref, "base": branch, "draft": False,
    })
    try:
        result = _put(f"repos/{repo}/pulls/{pr['number']}/merge", {
            "merge_method": "squash",
            "commit_title": pr["title"],
            "commit_message": f"Automatic scale-down after {WINDOW} low usage for {workload}",
        })
        pr["_merged"] = bool(result.get("merged"))
        if not pr["_merged"]:
            log.info("scale-down: auto-merge unavailable for PR #%s: %s", pr["number"], result.get("message", "unknown reason"))
    except Exception as exc:
        pr["_merged"] = False
        log.info("scale-down: branch protection or checks prevented merging PR #%s: %s", pr["number"], exc)
    return pr


def _propose_for_workload(repo: str, branch: str, head_sha: str, workload: str,
                          namespace: str, cluster: str, review: dict | None = None) -> dict | None:
    review = review if review is not None else {}
    review.update(workload=workload, namespace=namespace, cluster=cluster, window=WINDOW)

    def skip(reason: str, metrics: dict | None = None) -> None:
        review.update(decision="skipped", reason=reason)
        if metrics is not None:
            review["metrics"] = metrics
        return None

    path = f"gitops/workloads/{workload}/deployment.yaml"
    if _has_open_recommendation(repo, workload, branch):
        return skip("An earlier scale-down recommendation is still open.")
    if _recent_commit(repo, path):
        return skip(f"The {COOLDOWN_SECONDS}s deployment-change cooldown has not elapsed.")
    loaded = _load_manifest(repo, branch, path)
    if not loaded:
        return skip(f"Could not read {path} from the GitOps repository.")
    content, blob_sha = loaded
    replicas_match = re.search(r"\breplicas:\s*(\d+)", content)
    if not replicas_match:
        return skip("The deployment manifest has no replica count to review.")
    replicas = int(replicas_match.group(1))
    min_replicas = 2 if workload in config.USER_FACING_SERVICES else 1
    review.update(current_replicas=replicas, candidate_floor=min_replicas)
    requests, limits = _quantity_values(content, "requests"), _quantity_values(content, "limits")
    cpu_capacity = _cpu_millicores(limits.get("cpu") or requests.get("cpu"))
    cpu_request = _cpu_millicores(requests.get("cpu"))
    memory_limit, memory_request = _memory_mib(limits.get("memory")), _memory_mib(requests.get("memory"))
    if not cpu_capacity or not memory_limit or memory_request is None or cpu_request is None:
        return skip("CPU or memory requests/limits could not be parsed from the manifest.")

    metrics = _metrics(cluster, namespace, workload)
    review["metrics"] = metrics
    missing = [key for key, value in metrics.items() if value is None]
    if missing:
        return skip("Required metrics are unavailable: " + ", ".join(missing), metrics)
    if _active_alert(workload, namespace):
        return skip("A matching alert is active.", metrics)
    if (metrics["cpu_coverage_seconds"] < MIN_COVERAGE_SECONDS
            or metrics["memory_coverage_seconds"] < MIN_COVERAGE_SECONDS):
        return skip(f"Metric coverage is below the {MIN_COVERAGE_SECONDS}s minimum.", metrics)
    if metrics["restarts_in_window"] > 0:
        return skip("The workload restarted during the evidence window.", metrics)
    if metrics["available_replicas"] < replicas:
        return skip("Not all desired replicas are currently available.", metrics)

    cpu_pct = 100 * metrics["cpu_p95_cores"] * 1000 / cpu_capacity
    memory_pct = 100 * metrics["memory_p99_bytes"] / (memory_limit * 1024 * 1024)
    throttle_pct = 100 * metrics["cpu_throttle"]
    low_cpu = cpu_pct < CPU_P95_MAX * 100 and throttle_pct < CPU_THROTTLE_MAX * 100
    low_memory = memory_pct < MEMORY_P99_MAX * 100
    review.update(cpu_pct=cpu_pct, memory_pct=memory_pct, throttle_pct=throttle_pct,
                  low_cpu=low_cpu, low_memory=low_memory)

    changed = None
    if replicas > min_replicas and low_cpu and low_memory:
        changed = _replace_replicas(content, replicas, min_replicas)
        operation = f"replicas `{replicas}` → `{min_replicas}` (candidate floor)"
    elif low_memory and memory_limit - memory_request >= 128:
        step = 512 if memory_limit >= 2048 else 128
        new_mib = max(memory_request, memory_limit - step)
        if new_mib < memory_limit:
            new_value = f"{new_mib}Mi"
            changed = _replace_resource(content, "limits", "memory", limits["memory"], new_value)
            operation = f"memory limit `{limits['memory']}` → `{new_value}`"
    elif low_cpu and limits.get("cpu") and (_cpu_millicores(limits["cpu"]) or 0) - cpu_request >= 50:
        old_cpu = _cpu_millicores(limits["cpu"])
        new_cpu = max(cpu_request, old_cpu - 50)
        changed = _replace_resource(content, "limits", "cpu", limits["cpu"], f"{new_cpu}m")
        operation = f"CPU limit `{limits['cpu']}` → `{new_cpu}m`"
    if not changed:
        if replicas <= min_replicas and not low_memory and not low_cpu:
            reason = "At the replica floor; CPU and memory evidence do not support reducing resource limits."
        elif replicas <= min_replicas and not low_memory:
            reason = "At the replica floor; memory P99 is too high to reduce the memory limit."
        elif replicas <= min_replicas and not low_cpu:
            reason = "At the replica floor; CPU P95 or throttling is too high to reduce the CPU limit."
        else:
            reason = "Metrics do not support a safe replica-floor or resource-limit proposal."
        return skip(reason, metrics)

    if replicas > min_replicas and low_cpu and low_memory:
        decision_reason = (
            f"CPU P95 {cpu_pct:.1f}% and throttling {throttle_pct:.2f}% are below their thresholds, "
            f"and memory P99 is {memory_pct:.1f}%; proposing the configured floor."
        )
    elif operation.startswith("memory limit"):
        decision_reason = f"Memory P99 is {memory_pct:.1f}% of the current limit; reducing one memory step at the replica floor."
    else:
        decision_reason = f"CPU P95 {cpu_pct:.1f}% and throttling {throttle_pct:.2f}% support reducing one CPU step at the replica floor."
    review.update(decision="proposal", reason=decision_reason, proposed_change=operation,
                  **({"candidate_floor": min_replicas} if replicas > min_replicas and low_cpu and low_memory else {}))
    pr = _open_recommendation(repo, branch, head_sha, path, blob_sha, changed,
                              workload, operation, {
                                  "cpu_pct": cpu_pct, "throttle_pct": throttle_pct,
                                  "memory_pct": memory_pct,
                                  **({"candidate_floor": min_replicas} if replicas > min_replicas and low_cpu and low_memory else {}),
                              })
    review.update(pr_number=pr.get("number"), pr_url=pr.get("html_url"), merged=pr.get("_merged", False))
    if review["merged"]:
        target_replicas = re.search(r"\breplicas:\s*(\d+)", changed)
        target_requests = _quantity_values(changed, "requests")
        target_limits = _quantity_values(changed, "limits")
        review["merged_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        review["post_merge"] = _verify_post_merge(
            workload, namespace, cluster,
            expected_replicas=int(target_replicas.group(1)) if target_replicas else replicas,
            expected_memory_mib=_memory_mib(target_limits.get("memory")),
            expected_cpu_millicores=_cpu_millicores(target_limits.get("cpu") or target_requests.get("cpu")),
        )
    else:
        review["post_merge"] = {"status": "not_merged", "checks": [],
                                "reason": "The pull request is open; post-merge verification will run only after merge."}
    return pr


def run_daily_review() -> None:
    if not config.GITHUB_TOKEN or not GRAFANA_API_TOKEN:
        raise RuntimeError("scale-down requires GITHUB_TOKEN and a read-only Grafana token; refusing to proceed")
    reports_dir = Path(config.REPORTS_DIR)
    reports_dir.mkdir(parents=True, exist_ok=True)
    probe = reports_dir / f".capacity-review-probe-{uuid4().hex}"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    workloads = [item.strip() for item in os.getenv("SCALE_DOWN_WORKLOADS", "orders-api:demo,frontend:demo").split(",") if item.strip()]
    cluster = os.getenv("SCALE_DOWN_CLUSTER", "platform")
    repo = config.GITOPS_REPO
    info = _get(f"repos/{repo}")
    branch = info.get("default_branch", "main")
    head_sha = _get(f"repos/{repo}/git/ref/heads/{branch}")["object"]["sha"]
    errors = []
    for item in workloads:
        workload, sep, namespace = item.partition(":")
        if not sep or not workload or not namespace:
            raise ValueError(f"invalid SCALE_DOWN_WORKLOADS entry: {item!r}")
        started_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        review = {"workload": workload, "namespace": namespace, "cluster": cluster,
                  "window": WINDOW, "started_at": started_at, "decision": "error",
                  "reason": "Review did not complete.", "metrics": {}}
        try:
            pr = _propose_for_workload(repo, branch, head_sha, workload, namespace, cluster, review)
            if pr:
                log.info("scale-down recommendation processed", extra={"workload": workload, "pr_number": pr["number"],
                                                                        "pr_url": pr["html_url"], "merged": pr.get("_merged", False)})
            else:
                log.info("scale-down skipped", extra={"workload": workload, "reason": review.get("reason")})
        except Exception as exc:
            log.exception("scale-down review failed for %s: %s", workload, exc)
            review.update(decision="error", reason=str(exc))
            errors.append(workload)
        try:
            report_id = _write_capacity_report(review)
            log.info("capacity report available", extra={"workload": workload, "review_type": "daily", "report_id": report_id,
                                                           "decision": review.get("decision"),
                                                           "candidate_floor": review.get("candidate_floor"),
                                                           "pr_number": review.get("pr_number"),
                                                           "pr_url": review.get("pr_url"),
                                                           "post_merge_status": (review.get("post_merge") or {}).get("status", "not_applicable"),
                                                           "report_url": f"https://reports.localhost:9443/#{report_id}"})
        except Exception as exc:
            log.exception("capacity report could not be written for %s: %s", workload, exc)
            errors.append(workload)
    if errors:
        raise RuntimeError("scale-down review failed for: " + ", ".join(errors))


def _review_replica_floor(workload: str, namespace: str, cluster: str) -> dict:
    """Review a service's configured replica floor from a week of telemetry; never mutate Git or scale."""
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    floor = 2 if workload in config.USER_FACING_SERVICES else 1
    review = {"review_type": "weekly_floor", "workload": workload, "namespace": namespace,
              "cluster": cluster, "window": WEEKLY_FLOOR_WINDOW, "started_at": started_at,
              "configured_floor": floor, "decision": "skipped", "metrics": {},
              "proposed_change": f"Maintain a minimum of {floor} replicas",
              "reason": "Weekly review did not complete; no scaling action is taken by this review."}
    try:
        loaded = _load_manifest(config.GITOPS_REPO, "main", f"gitops/workloads/{workload}/deployment.yaml")
        if not loaded:
            review["reason"] = "Deployment manifest unavailable; floor recommendation withheld."
            return review
        content, _ = loaded
        replicas_match = re.search(r"\breplicas:\s*(\d+)", content)
        if not replicas_match:
            review["reason"] = "Deployment replica count unavailable; floor recommendation withheld."
            return review
        replicas = int(replicas_match.group(1))
        review["current_replicas"] = replicas
        requests, limits = _quantity_values(content, "requests"), _quantity_values(content, "limits")
        cpu_capacity = _cpu_millicores(limits.get("cpu") or requests.get("cpu"))
        memory_limit = _memory_mib(limits.get("memory"))
        if not cpu_capacity or not memory_limit:
            review["reason"] = "CPU or memory capacity could not be parsed; floor recommendation withheld."
            return review
        metrics = _metrics(cluster, namespace, workload, WEEKLY_FLOOR_WINDOW)
        review["metrics"] = metrics
        missing = [key for key, value in metrics.items() if value is None]
        if missing:
            review["reason"] = "Insufficient telemetry; unavailable metrics: " + ", ".join(missing)
            return review
        if (metrics["cpu_coverage_seconds"] < WEEKLY_FLOOR_MIN_COVERAGE_SECONDS
                or metrics["memory_coverage_seconds"] < WEEKLY_FLOOR_MIN_COVERAGE_SECONDS):
            review["reason"] = (f"Insufficient seven-day history: at least {WEEKLY_FLOOR_MIN_COVERAGE_SECONDS}s "
                                 "of CPU and memory coverage is required; no lower floor is recommended.")
            return review
        if _active_alert(workload, namespace):
            review["reason"] = "A matching alert is active; retain the current floor and investigate first."
            return review
        if metrics["restarts_in_window"] > 0:
            review["reason"] = "Restarts occurred in the weekly window; retain the current floor."
            return review
        if metrics["available_replicas"] < replicas:
            review["reason"] = "Not all desired replicas are available; retain the current floor."
            return review
        cpu_pct = 100 * metrics["cpu_p95_cores"] * 1000 / cpu_capacity
        throttle_pct = 100 * metrics["cpu_throttle"]
        memory_pct = 100 * metrics["memory_p99_bytes"] / (memory_limit * 1024 * 1024)
        low_cpu = cpu_pct < CPU_P95_MAX * 100 and throttle_pct < CPU_THROTTLE_MAX * 100
        low_memory = memory_pct < MEMORY_P99_MAX * 100
        review.update(cpu_pct=cpu_pct, throttle_pct=throttle_pct, memory_pct=memory_pct)
        if replicas <= floor:
            review["decision"] = "recommendation"
            review["candidate_floor"] = floor
            review["reason"] = (f"The service is already at its configured {floor}-replica floor and the "
                                 "week-long availability and resource signals do not justify lowering it.")
            review["proposed_change"] = f"Retain the configured floor of {floor} replicas; do not scale below it"
            return review
        if low_cpu and low_memory:
            review["decision"] = "recommendation"
            review["candidate_floor"] = floor
            review["proposed_change"] = (f"Consider reducing replicas from {replicas} to the configured floor of {floor}; "
                                          "recommendation only, no reduction applied")
            review["reason"] = (f"Seven-day CPU P95 used {cpu_pct:.1f}% of capacity, throttling was {throttle_pct:.2f}%, "
                                f"and memory P99 used {memory_pct:.1f}% of its limit. All replicas remained available "
                                "with no restarts or matching alerts; evidence supports reviewing a change to the "
                                f"configured {floor}-replica floor.")
            return review
        review["decision"] = "recommendation"
        review["candidate_floor"] = replicas
        review["proposed_change"] = f"Retain {replicas} replicas; configured minimum remains {floor}"
        review["reason"] = (f"The weekly signals do not support reducing {replicas} replicas to the configured "
                            f"{floor}-replica floor (CPU P95 {cpu_pct:.1f}%, throttling {throttle_pct:.2f}%, "
                            f"memory P99 {memory_pct:.1f}%). Retain the current replica count and review again next week.")
        return review
    except Exception as exc:
        review["decision"] = "error"
        review["reason"] = f"Weekly floor review failed: {exc}"
        return review


def run_weekly_floor_review() -> None:
    """Publish read-only weekly floor reports for each configured service."""
    reports_dir = Path(config.REPORTS_DIR)
    reports_dir.mkdir(parents=True, exist_ok=True)
    workloads = [item.strip() for item in os.getenv("WEEKLY_FLOOR_WORKLOADS", "orders-api:demo,frontend:demo").split(",") if item.strip()]
    cluster = os.getenv("WEEKLY_FLOOR_CLUSTER", "platform")
    errors = []
    for item in workloads:
        workload, sep, namespace = item.partition(":")
        if not sep or not workload or not namespace:
            raise ValueError(f"invalid WEEKLY_FLOOR_WORKLOADS entry: {item!r}")
        review = _review_replica_floor(workload, namespace, cluster)
        try:
            report_id = _write_capacity_report(review)
            log.info("capacity report available", extra={"workload": workload, "review_type": "weekly_floor",
                                                           "report_id": report_id, "decision": review["decision"],
                                                           "candidate_floor": review["candidate_floor"],
                                                           "report_url": f"https://reports.localhost:9443/#{report_id}"})
            if review["decision"] == "error":
                errors.append(workload)
        except Exception as exc:
            log.exception("weekly capacity report could not be written for %s: %s", workload, exc)
            errors.append(workload)
    if errors:
        raise RuntimeError("weekly floor review/report failed for: " + ", ".join(errors))
