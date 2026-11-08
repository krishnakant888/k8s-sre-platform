"""Daily, guarded capacity reductions based on a full day of Mimir metrics.

Changes go through a PR and are auto-merged when repository rules allow it.
Missing data, active alerts, recent deployments, restarts, or an unstable
deployment all fail closed.
"""
import base64
import logging
import math
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from agent import config
from agent.remediate import _headers

log = logging.getLogger("sre-agent")
WINDOW = os.getenv("SCALE_DOWN_WINDOW", "24h")
CPU_P95_MAX = 0.50
CPU_THROTTLE_MAX = 0.01
MEMORY_P99_MAX = 0.60
MIN_COVERAGE_SECONDS = int(os.getenv("SCALE_DOWN_MIN_COVERAGE_SECONDS", str(24 * 60 * 60)))
COOLDOWN_SECONDS = int(os.getenv("SCALE_DOWN_COOLDOWN_SECONDS", str(24 * 60 * 60)))
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


def _metrics(cluster: str, namespace: str, workload: str) -> dict[str, float | None]:
    sel = _selectors(cluster, namespace, workload)
    cpu = f'container_cpu_usage_seconds_total{{{sel}}}'
    memory = f'container_memory_working_set_bytes{{{sel}}}'
    coverage_window = _coverage_window(WINDOW, COVERAGE_SUBQUERY_STEP)
    return {
        "cpu_p95_cores": _query(
            f"max(quantile_over_time(0.95, rate({cpu}[5m])[{WINDOW}:{CPU_SUBQUERY_STEP}]))"
        ),
        "cpu_throttle": _query(
            f"max(max_over_time((rate(container_cpu_cfs_throttled_periods_total{{{sel}}}[5m]) / "
            f"rate(container_cpu_cfs_periods_total{{{sel}}}[5m]))[{WINDOW}:{CPU_SUBQUERY_STEP}]))"
        ),
        "memory_p99_bytes": _query(
            f"max(quantile_over_time(0.99, {memory}[{WINDOW}]))"
        ),
        "cpu_coverage_seconds": _query(
            f"min(time() - min_over_time((timestamp(rate({cpu}[5m])))[{coverage_window}:{COVERAGE_SUBQUERY_STEP}]))"
        ),
        "memory_coverage_seconds": _query(
            f"min(time() - min_over_time((timestamp({memory}))[{coverage_window}:{COVERAGE_SUBQUERY_STEP}]))"
        ),
        "restarts_24h": _query(
            f"sum(increase(kube_pod_container_status_restarts_total{{{sel}}}[{WINDOW}]))"
        ),
        "available_replicas": _query(
            f'min(kube_deployment_status_replicas_available{{cluster="{cluster}",'
            f'namespace="{namespace}",deployment="{workload}"}})'
        ),
    }


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
    body = (
        "## Scale-down recommendation\n\n"
        f"**Workload:** `{workload}`\n"
        f"**Proposed change:** {operation}\n"
        f"**Evidence window:** {WINDOW}; CPU P95 `{evidence['cpu_pct']:.0f}%` of capacity, "
        f"CPU throttling `{evidence['throttle_pct']:.2f}%`, memory P99 `{evidence['memory_pct']:.0f}%` of limit.\n\n"
        "The workload had no active matching alerts, no restarts in the evidence window, and all replicas were available. "
        "This changes one replica or one resource step after the 24-hour safety checks. The agent attempts to merge it immediately; "
        "repository branch protection may leave it open for review."
    )
    pr = _post(f"repos/{repo}/pulls", {
        "title": f"chore(capacity): scale down {workload}",
        "body": body, "head": ref, "base": branch, "draft": False,
    })
    try:
        result = _put(f"repos/{repo}/pulls/{pr['number']}/merge", {
            "merge_method": "squash",
            "commit_title": pr["title"],
            "commit_message": f"Automatic scale-down after 24h low usage for {workload}",
        })
        pr["_merged"] = bool(result.get("merged"))
        if not pr["_merged"]:
            log.info("scale-down: auto-merge unavailable for PR #%s: %s", pr["number"], result.get("message", "unknown reason"))
    except Exception as exc:
        pr["_merged"] = False
        log.info("scale-down: branch protection or checks prevented merging PR #%s: %s", pr["number"], exc)
    return pr


def _propose_for_workload(repo: str, branch: str, head_sha: str, workload: str,
                          namespace: str, cluster: str) -> dict | None:
    path = f"gitops/workloads/{workload}/deployment.yaml"
    if _has_open_recommendation(repo, workload, branch) or _recent_commit(repo, path):
        return None
    loaded = _load_manifest(repo, branch, path)
    if not loaded:
        return None
    content, blob_sha = loaded
    replicas_match = re.search(r"\breplicas:\s*(\d+)", content)
    if not replicas_match:
        return None
    replicas = int(replicas_match.group(1))
    min_replicas = 2 if workload in config.USER_FACING_SERVICES else 1
    requests, limits = _quantity_values(content, "requests"), _quantity_values(content, "limits")
    cpu_capacity = _cpu_millicores(limits.get("cpu") or requests.get("cpu"))
    cpu_request = _cpu_millicores(requests.get("cpu"))
    memory_limit, memory_request = _memory_mib(limits.get("memory")), _memory_mib(requests.get("memory"))
    if not cpu_capacity or not memory_limit or memory_request is None or cpu_request is None:
        return None

    metrics = _metrics(cluster, namespace, workload)
    if any(metrics[k] is None for k in metrics) or _active_alert(workload, namespace):
        return None
    if (metrics["cpu_coverage_seconds"] < MIN_COVERAGE_SECONDS
            or metrics["memory_coverage_seconds"] < MIN_COVERAGE_SECONDS):
        return None
    if metrics["restarts_24h"] > 0 or metrics["available_replicas"] < replicas:
        return None

    cpu_pct = 100 * metrics["cpu_p95_cores"] * 1000 / cpu_capacity
    memory_pct = 100 * metrics["memory_p99_bytes"] / (memory_limit * 1024 * 1024)
    throttle_pct = 100 * metrics["cpu_throttle"]
    low_cpu = cpu_pct < CPU_P95_MAX * 100 and throttle_pct < CPU_THROTTLE_MAX * 100
    low_memory = memory_pct < MEMORY_P99_MAX * 100

    changed = None
    if replicas > min_replicas and low_cpu and low_memory:
        changed = _replace_replicas(content, replicas, replicas - 1)
        operation = f"replicas `{replicas}` → `{replicas - 1}` (minimum {min_replicas})"
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
        return None
    return _open_recommendation(repo, branch, head_sha, path, blob_sha, changed,
                                workload, operation, {
                                    "cpu_pct": cpu_pct, "throttle_pct": throttle_pct,
                                    "memory_pct": memory_pct,
                                })


def run_daily_review() -> None:
    if not config.GITHUB_TOKEN or not GRAFANA_API_TOKEN:
        raise RuntimeError("scale-down requires GITHUB_TOKEN and a read-only Grafana token; refusing to proceed")
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
        try:
            pr = _propose_for_workload(repo, branch, head_sha, workload, namespace, cluster)
            if pr:
                log.info("scale-down recommendation processed", extra={"workload": workload, "pr_number": pr["number"],
                                                                        "pr_url": pr["html_url"], "merged": pr.get("_merged", False)})
            else:
                log.info("scale-down skipped", extra={"workload": workload})
        except Exception as exc:
            log.exception("scale-down review failed for %s: %s", workload, exc)
            errors.append(workload)
    if errors:
        raise RuntimeError("scale-down review failed for: " + ", ".join(errors))
