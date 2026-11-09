"""Automated remediation: vertical resource limit changes only.

Conditions (all must hold):
  - root_cause.category == "resource_exhaustion"
  - GITHUB_TOKEN is configured
  - The workload has an Argo CD source directory with a YAML that has the target field

Memory path  (OOMKill evidence):
  Raise memory to max_over_time(container_memory_working_set_bytes[6h]) × 1.25, capped at
  MAX_MEMORY_LIMIT_MIB.

CPU vertical scale-up  (CPU throttling):
  New CPU limit = P95 cpu_usage × 1.5, rounded to nearest 50/100/500m milestone.
  Falls back to 1.5× the current limit when Mimir has no data.

Horizontal scaling (replica changes) is handled by the HPA — the agent does not touch replicas.
"""

import base64
import logging
import math
import re
import time
from typing import Any

import httpx

from agent import config

log = logging.getLogger("sre-agent")

_API = "https://api.github.com"
MAX_CPU_LIMIT_M = int(getattr(config, "MAX_CPU_LIMIT_M", 2000))
MAX_MEMORY_LIMIT_MIB = int(getattr(config, "MAX_MEMORY_LIMIT_MIB", 4096))


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get(path: str, **kw) -> Any:
    r = httpx.get(f"{_API}/{path}", headers=_headers(), timeout=15, **kw)
    r.raise_for_status()
    return r.json()


def _post(path: str, body: dict) -> Any:
    r = httpx.post(f"{_API}/{path}", headers=_headers(), json=body, timeout=15)
    r.raise_for_status()
    return r.json()


def _put(path: str, body: dict) -> Any:
    r = httpx.put(f"{_API}/{path}", headers=_headers(), json=body, timeout=15)
    r.raise_for_status()
    return r.json()


# ── OOM helpers ──────────────────────────────────────────────────────────────

def _query_peak_bytes(ns: str, wl: str) -> int | None:
    """Query Mimir for the 6h peak working-set bytes of the workload. Returns bytes or None."""
    query = f'max(max_over_time(container_memory_working_set_bytes{{namespace="{ns}",pod=~"{wl}-[a-z0-9]+-[a-z0-9]+"}}[6h]))'
    try:
        r = httpx.get(f"{config.MIMIR_URL}/api/v1/query", params={"query": query}, timeout=10)
        r.raise_for_status()
        results = r.json().get("data", {}).get("result", [])
        if results:
            return int(float(results[0]["value"][1]))
    except Exception as exc:
        log.debug("remediate: peak memory query failed: %s", exc)
    return None


def _bytes_to_limit(peak_bytes: int, headroom: float = 1.25) -> str:
    """Convert peak bytes → limit string with headroom, rounded to 128Mi or 0.5Gi boundary."""
    target = int(peak_bytes * headroom)
    if target >= 1024 * 1024 * 1024:
        gib_half = int(target / (512 * 1024 * 1024) + 0.999)
        return f"{gib_half / 2:.1f}Gi".replace(".0Gi", "Gi")
    mib = int(target / (128 * 1024 * 1024) + 0.999) * 128
    return f"{mib}Mi"


def _bump_memory_formula(limit: str) -> str | None:
    """Fallback: 1.5× the current limit string when Mimir has no data."""
    m = re.fullmatch(r"(\d+)(Mi|Gi)", limit)
    if not m:
        return None
    val, unit = int(m.group(1)), m.group(2)
    if unit == "Gi":
        return f"{max(val + 1, int(val * 1.5 + 0.5))}Gi"
    new = int(val * 1.5 / 128 + 0.999) * 128
    if new <= val:
        new += 128
    return f"{new}Mi"


def _memory_limit_mib(limit: str) -> int | None:
    m = re.fullmatch(r"(\d+)(Mi|Gi)", limit)
    if not m:
        return None
    value = int(m.group(1))
    return value * 1024 if m.group(2) == "Gi" else value


def _cap_memory_limit(limit: str) -> str | None:
    mib = _memory_limit_mib(limit)
    if mib is None:
        return None
    return f"{MAX_MEMORY_LIMIT_MIB}Mi" if mib > MAX_MEMORY_LIMIT_MIB else limit


def _memory_scaling_mode(current_limit: str, cpu_throttle: float | None) -> str:
    """Use vertical memory headroom first; at its cap, scale out only with CPU load evidence."""
    current_mib = _memory_limit_mib(current_limit)
    if current_mib is None:
        return "review"
    if current_mib < MAX_MEMORY_LIMIT_MIB:
        return "vertical"
    return "horizontal" if cpu_throttle is not None and cpu_throttle >= 0.25 else "review"


# ── CPU vertical scale-up helpers ────────────────────────────────────────────

def _query_cpu_p95_cores(ns: str, wl: str) -> float | None:
    """Return 6h P95 CPU usage in cores for the workload, or None."""
    query = (
        f'quantile_over_time(0.95, rate(container_cpu_usage_seconds_total{{'
        f'namespace="{ns}",pod=~"{wl}-[a-z0-9]+-[a-z0-9]+"}}[5m])[6h:1m])'
    )
    try:
        r = httpx.get(f"{config.MIMIR_URL}/api/v1/query", params={"query": query}, timeout=10)
        r.raise_for_status()
        results = r.json().get("data", {}).get("result", [])
        if results:
            return float(results[0]["value"][1])
    except Exception as exc:
        log.debug("remediate: cpu p95 query failed: %s", exc)
    return None


def _hpa_is_saturated(ns: str, wl: str) -> bool | None:
    """Return True if the HPA for wl is at maxReplicas, False if it has room, None if no HPA / query error."""
    sel = f'namespace="{ns}",horizontalpodautoscaler="{wl}"'
    try:
        cur_r = httpx.get(f"{config.MIMIR_URL}/api/v1/query",
                          params={"query": f"max(kube_horizontalpodautoscaler_status_current_replicas{{{sel}}})"},
                          timeout=10)
        max_r = httpx.get(f"{config.MIMIR_URL}/api/v1/query",
                          params={"query": f"max(kube_horizontalpodautoscaler_spec_max_replicas{{{sel}}})"},
                          timeout=10)
        cur_r.raise_for_status(); max_r.raise_for_status()
        cur_res = cur_r.json().get("data", {}).get("result", [])
        max_res = max_r.json().get("data", {}).get("result", [])
        if not cur_res or not max_res:
            return None  # no HPA found
        cur = float(cur_res[0]["value"][1])
        mx = float(max_res[0]["value"][1])
        return cur >= mx
    except Exception as exc:
        log.debug("remediate: hpa saturation query failed for %s/%s: %s", ns, wl, exc)
        return None


def _cores_to_cpu_limit(p95_cores: float, headroom: float = 1.5) -> str:
    """Convert P95 cores → CPU limit string with headroom, rounded to nearest milestone."""
    target_m = int(p95_cores * 1000 * headroom)
    if target_m <= 100:
        return f"{math.ceil(target_m / 50) * 50}m"
    if target_m <= 500:
        return f"{math.ceil(target_m / 100) * 100}m"
    if target_m <= 2000:
        return f"{math.ceil(target_m / 500) * 500}m"
    return f"{math.ceil(target_m / 1000) * 1000}m"


def _bump_cpu_formula(limit: str) -> str | None:
    """Fallback: 1.5× the current CPU limit string when Mimir has no data."""
    m = re.fullmatch(r"(\d+)(m?)", limit)
    if not m:
        return None
    val, unit = int(m.group(1)), m.group(2)
    val_m = val if unit == "m" else val * 1000
    new_m = int(val_m * 1.5 / 50 + 0.999) * 50
    if new_m <= val_m:
        new_m += 50
    return f"{new_m}m"




# ── shared file helpers ───────────────────────────────────────────────────────

def _find_resource_file(repo: str, source_dir: str, default_branch: str, field: str) -> tuple[str, str, str] | None:
    """Return (file_path, raw_content, blob_sha) for the first YAML in source_dir that has `field`."""
    try:
        items = _get(f"repos/{repo}/contents/{source_dir}", params={"ref": default_branch})
    except Exception:
        return None
    if not isinstance(items, list):
        return None
    for item in items:
        if item.get("type") != "file" or not item["name"].endswith(".yaml"):
            continue
        try:
            data = _get(f"repos/{repo}/contents/{item['path']}", params={"ref": default_branch})
        except Exception:
            continue
        content = base64.b64decode(data["content"]).decode()
        if re.search(field, content):
            return item["path"], content, data["sha"]
    return None


def _open_and_merge_pr(
    repo: str, default_branch: str, head_sha: str,
    branch: str, file_path: str, blob_sha: str, new_content: str,
    commit_msg: str, pr_title: str, pr_body: str,
) -> dict:
    """Create branch + commit + PR, attempt immediate squash merge. Returns pr dict with _merged key."""
    _post(f"repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": head_sha})
    _put(
        f"repos/{repo}/contents/{file_path}",
        {"message": commit_msg, "content": base64.b64encode(new_content.encode()).decode(),
         "sha": blob_sha, "branch": branch},
    )
    pr = _post(
        f"repos/{repo}/pulls",
        {"title": pr_title, "body": pr_body, "head": branch, "base": default_branch, "draft": False},
    )
    try:
        _put(
            f"repos/{repo}/pulls/{pr['number']}/merge",
            {"merge_method": "squash", "commit_title": pr_title,
             "commit_message": f"Automated remediation. {commit_msg.split(chr(10))[0]}"},
        )
        pr["_merged"] = True
    except Exception as exc:
        log.info("remediate: auto-merge skipped (branch protection or checks pending): %s", exc)
        pr["_merged"] = False
    return pr


# ── OOM remediation ───────────────────────────────────────────────────────────

def _remediate_oom(ctx: dict, report, repo: str, default_branch: str, head_sha: str) -> dict | None:
    sig = ctx["signals"]
    wl, ns = sig["workload"], sig["namespace"]
    growth = sig.get("mem_growth")
    if growth is not None and growth > 1.3:
        log.warning("remediate: memory grows %.1fx for %s; likely leak, refusing a sizing change", growth, wl)
        return None
    source_files = sig.get("source_files") or []
    if not source_files:
        return None

    result = _find_resource_file(
        repo, source_files[0], default_branch,
        r"limits[:\s{][^\n]*memory:|limits:\s*\{[^}]*memory:",
    )
    if not result:
        log.info("remediate: no YAML with limits.memory found in %s", source_files[0])
        return None
    file_path, content, blob_sha = result

    m = re.search(
        r"limits[^\n]*\n(?:[^\n]*\n){0,2}[^\n]*memory:\s*(\d+(?:Mi|Gi))"
        r"|limits:\s*\{[^}]*memory:\s*(\d+(?:Mi|Gi))",
        content,
    )
    if not m:
        return None
    old_limit = m.group(1) or m.group(2)

    old_limit_mib = _memory_limit_mib(old_limit)
    if old_limit_mib is None:
        return None
    if old_limit_mib >= MAX_MEMORY_LIMIT_MIB:
        throttle = _query_cpu_throttle_ratio(ns, wl)
        if _memory_scaling_mode(old_limit, throttle) == "horizontal":
            log.info("remediate: memory cap reached for %s and CPU throttling is %.0f%%; scaling out", wl, throttle * 100)
            return _remediate_scaleout(ctx, report, repo, default_branch, head_sha,
                                       reason="memory pressure at memory limit cap", escalate_cpu=False)
        log.warning("remediate: memory cap reached for %s without CPU evidence for load-driven pressure; human review needed", wl)
        return None

    sizing_method = "observed_peak"
    peak_bytes = _query_peak_bytes(ns, wl)
    if peak_bytes and peak_bytes > 0:
        new_limit = _cap_memory_limit(_bytes_to_limit(peak_bytes))
        peak_mib = peak_bytes / (1024 * 1024)
        sizing_note = f"Observed 6h peak: {peak_mib:.0f} Mi × 1.25 headroom = {new_limit}."
    else:
        new_limit = _cap_memory_limit(_bump_memory_formula(old_limit) or "")
        sizing_method = "formula_1.5x"
        peak_mib = None
        sizing_note = f"No Mimir data for `{wl}` in `{ns}`; used 1.5× formula."
    if not new_limit:
        return None

    # patch limits.memory
    patched, count = re.subn(
        r"(limits\b[^\n]*\n(?:[^\n]*\n){0,2}[^\n]*memory:\s*)" + re.escape(old_limit),
        lambda mo: mo.group(1) + new_limit, content, count=1, flags=re.MULTILINE,
    )
    if not count:
        patched, count = re.subn(
            r"(limits:\s*\{[^}]*memory:\s*)" + re.escape(old_limit),
            lambda mo: mo.group(1) + new_limit, content, count=1,
        )
    if not count or patched == content:
        log.warning("remediate: OOM patch produced no change for %s", file_path)
        return None

    # also patch requests.memory to match (keeps requests == limits for Guaranteed QoS)
    req_m = re.search(
        r"requests\b[^\n]*\n(?:[^\n]*\n){0,2}[^\n]*memory:\s*(\d+(?:Mi|Gi))"
        r"|requests:\s*\{[^}]*memory:\s*(\d+(?:Mi|Gi))",
        patched,
    )
    old_req = (req_m.group(1) or req_m.group(2)) if req_m else None
    if old_req:
        patched, _ = re.subn(
            r"(requests\b[^\n]*\n(?:[^\n]*\n){0,2}[^\n]*memory:\s*)" + re.escape(old_req),
            lambda mo: mo.group(1) + new_limit, patched, count=1, flags=re.MULTILINE,
        ) or re.subn(
            r"(requests:\s*\{[^}]*memory:\s*)" + re.escape(old_req),
            lambda mo: mo.group(1) + new_limit, patched, count=1,
        )
    req_change = f", `requests.memory` `{old_req}` → `{new_limit}`" if old_req else ""

    branch = f"remediate/oom-{wl}-{ns}-{int(time.time())}"
    commit_msg = (
        f"fix(ops): raise memory request+limit of {wl} in {ns} to {new_limit}\n\n"
        f"Automated remediation triggered by RCA {report.id}.\n"
        f"Sizing: {sizing_note}\n"
        f"Memory pressure was diagnosed; raising request and limit to give the workload measured headroom.\n"
    )
    pr_title = f"fix(ops): raise {wl} memory {old_limit} → {new_limit} ({ns})"
    cap_note = f" Capped at MAX_MEMORY_LIMIT_MIB={MAX_MEMORY_LIMIT_MIB}Mi." if _memory_limit_mib(new_limit) == MAX_MEMORY_LIMIT_MIB else ""
    pr_body = (
        f"## Automated remediation — memory pressure\n\n"
        f"**RCA:** `{report.id}`  \n"
        f"**Alert:** `{report.alert}` on `{wl}` in `{ns}`  \n"
        f"**Change:** `{file_path}` — `limits.memory` `{old_limit}` → `{new_limit}`{req_change}\n\n"
        f"**Sizing:** {sizing_note}{cap_note}\n\n"
        f"Merge to let Argo CD apply the measured memory request and limit.\n\n"
        f"**Verify:** `container_memory_working_set_bytes` for `{wl}` stays below 80% of `{new_limit}`."
    )

    try:
        pr = _open_and_merge_pr(
            repo, default_branch, head_sha, branch, file_path, blob_sha, patched,
            commit_msg, pr_title, pr_body,
        )
    except Exception as exc:
        log.warning("remediate: OOM PR failed: %s", exc)
        return None

    return {
        "type": "oom",
        "pr_url": pr["html_url"],
        "pr_number": pr["number"],
        "old_limit": old_limit,
        "new_limit": new_limit,
        "peak_mib": round(peak_mib) if peak_mib else None,
        "sizing_method": sizing_method,
        "file": file_path,
        "branch": branch,
        "status": "merged" if pr.get("_merged") else "pr_opened",
    }


# ── CPU vertical scale-up remediation ────────────────────────────────────────

def _remediate_cpu_scaleup(ctx: dict, report, repo: str, default_branch: str, head_sha: str) -> dict | None:
    """Raise limits.cpu based on observed P95 usage."""
    sig = ctx["signals"]
    wl, ns = sig["workload"], sig["namespace"]
    source_files = sig.get("source_files") or []
    if not source_files:
        return None

    result = _find_resource_file(
        repo, source_files[0], default_branch,
        r"limits[:\s{][^\n]*cpu:|limits:\s*\{[^}]*cpu:",
    )
    if not result:
        log.info("remediate: no YAML with limits.cpu found in %s", source_files[0])
        return None
    file_path, content, blob_sha = result

    m = re.search(
        r"limits[^\n]*\n(?:[^\n]*\n){0,2}[^\n]*cpu:\s*(\d+m?)"
        r"|limits:\s*\{[^}]*cpu:\s*(\d+m?)",
        content,
    )
    if not m:
        log.info("remediate: no existing cpu limit found in %s; skipping (prefer requests-only)", file_path)
        return None
    old_cpu = m.group(1) or m.group(2)

    p95_cores = _query_cpu_p95_cores(ns, wl)
    if p95_cores and p95_cores > 0:
        new_cpu = _cores_to_cpu_limit(p95_cores)
        sizing_note = f"Observed 6h P95 CPU: {p95_cores * 1000:.0f}m × 1.5 headroom = {new_cpu}."
        sizing_method = "observed_p95"
    else:
        new_cpu = _bump_cpu_formula(old_cpu)
        sizing_note = f"No Mimir data; used 1.5× formula on current limit {old_cpu}."
        sizing_method = "formula_1.5x"
    if not new_cpu:
        return None

    _m = re.fullmatch(r"(\d+)(m?)", new_cpu)
    new_cpu_m = int(_m.group(1)) if _m and _m.group(2) == "m" else int(_m.group(1)) * 1000 if _m else 0
    at_cpu_cap = new_cpu_m >= MAX_CPU_LIMIT_M
    if at_cpu_cap:
        new_cpu = f"{MAX_CPU_LIMIT_M}m"
        sizing_note += f" Capped at MAX_CPU_LIMIT_M={MAX_CPU_LIMIT_M}m — human review recommended."

    # patch limits.cpu
    patched, count = re.subn(
        r"(limits\b[^\n]*\n(?:[^\n]*\n){0,2}[^\n]*cpu:\s*)" + re.escape(old_cpu),
        lambda mo: mo.group(1) + new_cpu, content, count=1, flags=re.MULTILINE,
    )
    if not count:
        patched, count = re.subn(
            r"(limits:\s*\{[^}]*cpu:\s*)" + re.escape(old_cpu),
            lambda mo: mo.group(1) + new_cpu, content, count=1,
        )
    if not count or patched == content:
        log.warning("remediate: cpu scale-up patch produced no change for %s", file_path)
        return None

    # also patch requests.cpu — set to 50% of new limit so HPA has accurate utilization baseline
    req_m = re.search(
        r"requests\b[^\n]*\n(?:[^\n]*\n){0,2}[^\n]*cpu:\s*(\d+m?)"
        r"|requests:\s*\{[^}]*cpu:\s*(\d+m?)",
        patched,
    )
    old_req_cpu = (req_m.group(1) or req_m.group(2)) if req_m else None
    if old_req_cpu:
        _rm = re.fullmatch(r"(\d+)(m?)", new_cpu)
        new_cpu_m = int(_rm.group(1)) if _rm and _rm.group(2) == "m" else int(_rm.group(1)) * 1000 if _rm else 0
        new_req_cpu = f"{max(new_cpu_m // 2, 10)}m"
        patched, _ = re.subn(
            r"(requests\b[^\n]*\n(?:[^\n]*\n){0,2}[^\n]*cpu:\s*)" + re.escape(old_req_cpu),
            lambda mo: mo.group(1) + new_req_cpu, patched, count=1, flags=re.MULTILINE,
        ) or re.subn(
            r"(requests:\s*\{[^}]*cpu:\s*)" + re.escape(old_req_cpu),
            lambda mo: mo.group(1) + new_req_cpu, patched, count=1,
        )
    else:
        new_req_cpu = None
    req_change = f", `requests.cpu` `{old_req_cpu}` → `{new_req_cpu}`" if old_req_cpu else ""

    branch = f"remediate/cpu-scaleup-{wl}-{ns}-{int(time.time())}"
    commit_msg = (
        f"fix(ops): raise CPU request+limit of {wl} in {ns} to {new_cpu}\n\n"
        f"Automated remediation triggered by RCA {report.id}.\n"
        f"Sizing: {sizing_note}\n"
    )
    pr_title = f"fix(ops): raise {wl} CPU {old_cpu} → {new_cpu} ({ns})"
    cap_warning = (
        f"\n\n> **Warning:** CPU ceiling `MAX_CPU_LIMIT_M={MAX_CPU_LIMIT_M}m` reached. "
        f"Human review required."
    ) if at_cpu_cap else ""
    pr_body = (
        f"## Automated remediation — CPU vertical scale-up\n\n"
        f"**RCA:** `{report.id}`  \n"
        f"**Alert:** `{report.alert}` on `{wl}` in `{ns}`  \n"
        f"**Change:** `{file_path}` — `limits.cpu` `{old_cpu}` → `{new_cpu}`{req_change}\n\n"
        f"**Sizing:** {sizing_note}\n\n"
        f"**Verify:** CPU throttle ratio drops after the rollout.{cap_warning}"
    )

    try:
        pr = _open_and_merge_pr(
            repo, default_branch, head_sha, branch, file_path, blob_sha, patched,
            commit_msg, pr_title, pr_body,
        )
    except Exception as exc:
        log.warning("remediate: cpu scale-up PR failed: %s", exc)
        return None

    return {
        "type": "cpu_scaleup",
        "pr_url": pr["html_url"],
        "pr_number": pr["number"],
        "old_cpu_limit": old_cpu,
        "new_cpu_limit": new_cpu,
        "old_cpu_request": old_req_cpu,
        "new_cpu_request": new_req_cpu,
        "sizing_method": sizing_method,
        "file": file_path,
        "branch": branch,
        "status": "merged" if pr.get("_merged") else "pr_opened",
    }


# ── entry point ───────────────────────────────────────────────────────────────

def maybe_open_pr(ctx: dict, report) -> dict | None:
    """Open a remediation PR if conditions are met. Returns PR metadata dict or None."""
    if not config.GITHUB_TOKEN:
        return None
    if report.root_cause.category != "resource_exhaustion":
        return None

    sig = ctx["signals"]
    source_files = sig.get("source_files") or []
    if not source_files:
        return None

    try:
        repo_info = _get(f"repos/{config.GITOPS_REPO}")
        default_branch = repo_info.get("default_branch", "main")
        head_sha = _get(f"repos/{config.GITOPS_REPO}/git/ref/heads/{default_branch}")["object"]["sha"]
    except Exception as exc:
        log.warning("remediate: could not get repo info: %s", exc)
        return None

    evidence_text = " ".join(
        str(e.get("finding", "")) for e in ctx.get("evidence", []) if isinstance(e, dict)
    ).lower()

    # OOM: pod killed by kernel — raise memory limit (vertical)
    is_oom = "oomkill" in evidence_text or "oom" in evidence_text or "exit code 137" in evidence_text
    is_memory_pressure = is_oom or report.alert in ("ContainerMemoryNearLimit", "PodOOMKilled")
    if is_memory_pressure:
        return _remediate_oom(ctx, report, config.GITOPS_REPO, default_branch, head_sha)

    # CPU throttling — raise CPU limit only when HPA is saturated (at maxReplicas).
    # If HPA still has room it will scale out pods and resolve throttling on its own.
    sig = ctx["signals"]
    cpu_throttled = (
        sig.get("cpu_throttled")
        or "throttl" in evidence_text
        or report.alert == "ContainerCpuThrottled"
    )
    if cpu_throttled:
        saturated = _hpa_is_saturated(sig["namespace"], sig["workload"])
        if saturated is False:
            log.info("remediate: CPU throttled on %s but HPA has room — skipping vertical scale-up", sig["workload"])
            return None
        # saturated is True (at max) or None (no HPA / query failed) — raise the limit
        return _remediate_cpu_scaleup(ctx, report, config.GITOPS_REPO, default_branch, head_sha)

    return None
