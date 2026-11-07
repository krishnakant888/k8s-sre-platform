"""Automated remediation: raise memory limits (OOMKill) or replicas (CPU saturation).

Conditions (all must hold):
  - root_cause.category == "resource_exhaustion"
  - GITHUB_TOKEN is configured
  - The workload has an Argo CD source directory with a YAML that has the target field

OOM path  (OOMKill evidence):
  New limit = max_over_time(container_memory_working_set_bytes[6h]) × 1.25, rounded to
  128Mi / 0.5Gi. Falls back to 1.5× the current limit when Mimir has no data.

Scale-out path  (CPU throttling, no OOM evidence):
  New replicas = ceil(current × 1.5), capped at MAX_REPLICAS (default 10).
  Falls back to current + 1 when Mimir has no CPU data.
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
MAX_REPLICAS = int(getattr(config, "MAX_REPLICAS", 10))


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


# ── scale-out helpers ─────────────────────────────────────────────────────────

def _query_cpu_throttle_ratio(ns: str, wl: str) -> float | None:
    """Return the 6h average CPU throttle ratio (0–1) for the workload, or None."""
    query = (
        f'avg(rate(container_cpu_cfs_throttled_seconds_total{{namespace="{ns}",'
        f'pod=~"{wl}-[a-z0-9]+-[a-z0-9]+"}}[6h])) / '
        f'avg(rate(container_cpu_cfs_periods_total{{namespace="{ns}",'
        f'pod=~"{wl}-[a-z0-9]+-[a-z0-9]+"}}[6h]))'
    )
    try:
        r = httpx.get(f"{config.MIMIR_URL}/api/v1/query", params={"query": query}, timeout=10)
        r.raise_for_status()
        results = r.json().get("data", {}).get("result", [])
        if results:
            return float(results[0]["value"][1])
    except Exception as exc:
        log.debug("remediate: cpu throttle query failed: %s", exc)
    return None


def _new_replicas(current: int, throttle_ratio: float | None) -> int:
    """Scale replicas to absorb the observed throttling, capped at MAX_REPLICAS."""
    if throttle_ratio and throttle_ratio > 0:
        # If 40% of CPU time is throttled the workload needs ~1.67× as many pods
        needed = math.ceil(current / (1 - min(throttle_ratio, 0.9)))
    else:
        needed = current + 1
    return min(needed, MAX_REPLICAS)


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

    sizing_method = "observed_peak"
    peak_bytes = _query_peak_bytes(ns, wl)
    if peak_bytes and peak_bytes > 0:
        new_limit = _bytes_to_limit(peak_bytes)
        peak_mib = peak_bytes / (1024 * 1024)
        sizing_note = f"Observed 6h peak: {peak_mib:.0f} Mi × 1.25 headroom = {new_limit}."
    else:
        new_limit = _bump_memory_formula(old_limit)
        sizing_method = "formula_1.5x"
        peak_mib = None
        sizing_note = f"No Mimir data for `{wl}` in `{ns}`; used 1.5× formula."
    if not new_limit:
        return None

    # patch content
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

    branch = f"remediate/oom-{wl}-{ns}-{int(time.time())}"
    commit_msg = (
        f"fix(ops): raise memory limit of {wl} in {ns} from {old_limit} to {new_limit}\n\n"
        f"Automated remediation triggered by RCA {report.id}.\n"
        f"Sizing: {sizing_note}\n"
        f"The pod was OOMKilled; raising the limit to stop the restarts.\n"
    )
    pr_title = f"fix(ops): raise {wl} memory limit {old_limit} → {new_limit} ({ns})"
    pr_body = (
        f"## Automated remediation — OOMKill\n\n"
        f"**RCA:** `{report.id}`  \n"
        f"**Alert:** `{report.alert}` on `{wl}` in `{ns}`  \n"
        f"**Change:** `{file_path}` — `limits.memory` `{old_limit}` → `{new_limit}`\n\n"
        f"**Sizing:** {sizing_note}\n\n"
        f"The agent diagnosed the pod as OOMKilled. Merge to let Argo CD apply the new limit.\n\n"
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


# ── scale-out remediation ─────────────────────────────────────────────────────

def _remediate_scaleout(ctx: dict, report, repo: str, default_branch: str, head_sha: str) -> dict | None:
    sig = ctx["signals"]
    wl, ns = sig["workload"], sig["namespace"]
    source_files = sig.get("source_files") or []
    if not source_files:
        return None

    result = _find_resource_file(repo, source_files[0], default_branch, r"\breplicas:\s*\d+")
    if not result:
        log.info("remediate: no YAML with replicas field found in %s", source_files[0])
        return None
    file_path, content, blob_sha = result

    m = re.search(r"\breplicas:\s*(\d+)", content)
    if not m:
        return None
    old_replicas = int(m.group(1))

    throttle = _query_cpu_throttle_ratio(ns, wl)
    new_replicas = _new_replicas(old_replicas, throttle)
    if new_replicas <= old_replicas:
        log.info("remediate: scale-out already at max (%d) for %s", MAX_REPLICAS, wl)
        return None

    throttle_pct = f"{throttle * 100:.0f}%" if throttle else "unknown"
    sizing_note = (
        f"CPU throttle ratio {throttle_pct} → needed {new_replicas} replicas to absorb load."
        if throttle else
        f"No Mimir CPU data; added 1 replica as a conservative step."
    )

    patched, count = re.subn(
        r"(\breplicas:\s*)" + str(old_replicas) + r"\b",
        lambda mo: mo.group(1) + str(new_replicas),
        content, count=1,
    )
    if not count or patched == content:
        log.warning("remediate: scale-out patch produced no change for %s", file_path)
        return None

    branch = f"remediate/scaleout-{wl}-{ns}-{int(time.time())}"
    commit_msg = (
        f"fix(ops): scale {wl} in {ns} from {old_replicas} to {new_replicas} replicas\n\n"
        f"Automated remediation triggered by RCA {report.id}.\n"
        f"Sizing: {sizing_note}\n"
    )
    pr_title = f"fix(ops): scale {wl} {old_replicas} → {new_replicas} replicas ({ns})"
    pr_body = (
        f"## Automated remediation — CPU saturation\n\n"
        f"**RCA:** `{report.id}`  \n"
        f"**Alert:** `{report.alert}` on `{wl}` in `{ns}`  \n"
        f"**Change:** `{file_path}` — `replicas` `{old_replicas}` → `{new_replicas}`\n\n"
        f"**Sizing:** {sizing_note}\n\n"
        f"The agent diagnosed CPU throttling (not an OOM). Merge to let Argo CD scale the deployment.\n\n"
        f"**Verify:** `container_cpu_cfs_throttled_seconds_total` rate drops after the rollout. "
        f"If throttling persists, also raise `resources.limits.cpu`.\n\n"
        f"**Cap:** `MAX_REPLICAS={MAX_REPLICAS}`. If this is insufficient, raise the cap or add CPU limits."
    )

    try:
        pr = _open_and_merge_pr(
            repo, default_branch, head_sha, branch, file_path, blob_sha, patched,
            commit_msg, pr_title, pr_body,
        )
    except Exception as exc:
        log.warning("remediate: scale-out PR failed: %s", exc)
        return None

    return {
        "type": "scaleout",
        "pr_url": pr["html_url"],
        "pr_number": pr["number"],
        "old_replicas": old_replicas,
        "new_replicas": new_replicas,
        "throttle_pct": throttle_pct,
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

    # OOM: pod was killed by the kernel — fix is vertical (more memory)
    evidence_text = " ".join(
        str(e.get("finding", "")) for e in ctx.get("evidence", []) if isinstance(e, dict)
    ).lower()
    is_oom = "oomkill" in evidence_text or "oom" in evidence_text or "exit code 137" in evidence_text

    if is_oom:
        return _remediate_oom(ctx, report, config.GITOPS_REPO, default_branch, head_sha)

    # CPU saturation: throttled under load — fix is horizontal (more replicas)
    cpu_throttled = (
        sig.get("cpu_throttled")
        or "throttl" in evidence_text
        or "cpu" in evidence_text
    )
    if cpu_throttled:
        return _remediate_scaleout(ctx, report, config.GITOPS_REPO, default_branch, head_sha)

    return None
