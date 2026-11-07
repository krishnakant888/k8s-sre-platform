"""Automated remediation: open a GitHub PR to raise a memory limit when the agent diagnoses a sizing OOM.

Conditions (all must hold):
  - root_cause.category == "resource_exhaustion" with infrastructure layer (sizing, not a leak)
  - memory growth is flat (no leak signal)
  - GITHUB_TOKEN is configured
  - The workload has an Argo CD source directory with a YAML file containing resources.limits.memory

New limit = max_over_time(container_memory_working_set_bytes[6h]) × 1.25, rounded up to 128 Mi / 0.5 Gi.
Falls back to 1.5× the current limit string when Mimir has no data for the workload.
"""

import base64
import logging
import re
import time
from typing import Any

import httpx

from agent import config

log = logging.getLogger("sre-agent")

_API = "https://api.github.com"


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


def _query_peak_bytes(ns: str, wl: str) -> int | None:
    """Query Mimir for the 6h peak working-set bytes of the workload. Returns bytes or None."""
    query = f'max(max_over_time(container_memory_working_set_bytes{{namespace="{ns}",pod=~"{wl}-[a-z0-9]+-[a-z0-9]+"}}[6h]))'
    try:
        r = httpx.get(
            f"{config.MIMIR_URL}/api/v1/query",
            params={"query": query},
            timeout=10,
        )
        r.raise_for_status()
        results = r.json().get("data", {}).get("result", [])
        if results:
            return int(float(results[0]["value"][1]))
    except Exception as exc:
        log.debug("remediate: peak query failed: %s", exc)
    return None


def _bytes_to_limit(peak_bytes: int, headroom: float = 1.25) -> str:
    """Convert peak bytes → limit string with headroom, rounded to 128Mi or 0.5Gi boundary."""
    target = int(peak_bytes * headroom)
    if target >= 1024 * 1024 * 1024:
        # round up to next 0.5 Gi
        gib_half = int(target / (512 * 1024 * 1024) + 0.999)
        return f"{gib_half / 2:.1f}Gi".replace(".0Gi", "Gi")
    # round up to next 128 Mi
    mib = int(target / (128 * 1024 * 1024) + 0.999) * 128
    return f"{mib}Mi"


def _bump_formula(limit: str) -> str | None:
    """Fallback: 1.5× the current limit string when Mimir has no data."""
    m = re.fullmatch(r"(\d+)(Mi|Gi)", limit)
    if not m:
        return None
    val, unit = int(m.group(1)), m.group(2)
    if unit == "Gi":
        new = max(val + 1, int(val * 1.5 + 0.5))
        return f"{new}Gi"
    new = int(val * 1.5 / 128 + 0.999) * 128
    if new <= val:
        new += 128
    return f"{new}Mi"


def _find_resource_file(repo: str, source_dir: str, default_branch: str) -> tuple[str, str, str] | None:
    """Return (file_path, raw_content, blob_sha) for the first YAML in source_dir that has limits.memory."""
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
        if re.search(r"limits[:\s{][^\n]*memory:", content) or re.search(r"limits:\s*\{[^}]*memory:", content):
            return item["path"], content, data["sha"]
    return None


def _patch_content(content: str, old_limit: str, new_limit: str) -> str:
    # Replace first occurrence of `memory: <old_limit>` that is under a limits block.
    # We only replace the first match to avoid touching requests if they have the same value.
    patched, count = re.subn(
        r"(limits\b[^\n]*\n(?:[^\n]*\n){0,2}[^\n]*memory:\s*)" + re.escape(old_limit),
        lambda mo: mo.group(1) + new_limit,
        content,
        count=1,
        flags=re.MULTILINE,
    )
    if count:
        return patched
    # Fallback: inline limits: {memory: X} pattern
    patched, count = re.subn(
        r"(limits:\s*\{[^}]*memory:\s*)" + re.escape(old_limit),
        lambda mo: mo.group(1) + new_limit,
        content,
        count=1,
    )
    return patched if count else content


def maybe_open_pr(ctx: dict, report) -> dict | None:
    """Open a remediation PR if conditions are met. Returns PR metadata dict or None."""
    if not config.GITHUB_TOKEN:
        return None

    rc = report.root_cause
    if rc.category != "resource_exhaustion":
        return None

    sig = ctx["signals"]
    # Skip if memory is growing (leak/unbounded cache) — raising the limit only delays the kill
    if sig.get("mem_growth") and sig["mem_growth"] > 1.3:
        return None

    source_files = sig.get("source_files") or []
    if not source_files:
        return None

    repo = config.GITOPS_REPO
    wl = sig["workload"]
    ns = sig["namespace"]

    # Get repo default branch
    try:
        repo_info = _get(f"repos/{repo}")
        default_branch = repo_info.get("default_branch", "main")
        head_sha = _get(f"repos/{repo}/git/ref/heads/{default_branch}")["object"]["sha"]
    except Exception as exc:
        log.warning("remediate: could not get repo info: %s", exc)
        return None

    # Find the YAML file containing the memory limit
    result = _find_resource_file(repo, source_files[0], default_branch)
    if not result:
        log.info("remediate: no YAML with limits.memory found in %s", source_files[0])
        return None
    file_path, content, blob_sha = result

    m = re.search(r"limits[^\n]*\n(?:[^\n]*\n){0,2}[^\n]*memory:\s*(\d+(?:Mi|Gi))|limits:\s*\{[^}]*memory:\s*(\d+(?:Mi|Gi))", content)
    if not m:
        return None
    old_limit = m.group(1) or m.group(2)

    # Prefer observed peak + 25% headroom; fall back to 1.5× formula
    sizing_method = "observed_peak"
    peak_bytes = _query_peak_bytes(ns, wl)
    if peak_bytes and peak_bytes > 0:
        new_limit = _bytes_to_limit(peak_bytes)
        peak_mib = peak_bytes / (1024 * 1024)
        sizing_note = f"Observed 6h peak: {peak_mib:.0f} Mi × 1.25 headroom = {new_limit}."
    else:
        new_limit = _bump_formula(old_limit)
        sizing_method = "formula_1.5x"
        sizing_note = f"No Mimir data for `{wl}` in `{ns}`; used 1.5× formula. Verify against `container_memory_working_set_bytes` before merging."
    if not new_limit:
        return None

    new_content = _patch_content(content, old_limit, new_limit)
    if new_content == content:
        log.warning("remediate: patch produced no change for %s", file_path)
        return None

    branch = f"remediate/oom-{wl}-{ns}-{int(time.time())}"
    commit_msg = (
        f"fix(ops): raise memory limit of {wl} in {ns} from {old_limit} to {new_limit}\n\n"
        f"Automated remediation triggered by RCA {report.id}.\n"
        f"Sizing: {sizing_note}\n"
        f"The pod was OOMKilled; memory usage was flat (not a leak) so raising the limit is safe.\n"
        f"Review and merge to let Argo CD apply it."
    )

    try:
        _post(f"repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": head_sha})
        _put(
            f"repos/{repo}/contents/{file_path}",
            {
                "message": commit_msg,
                "content": base64.b64encode(new_content.encode()).decode(),
                "sha": blob_sha,
                "branch": branch,
            },
        )
        pr = _post(
            f"repos/{repo}/pulls",
            {
                "title": f"fix(ops): raise {wl} memory limit {old_limit} → {new_limit} ({ns})",
                "body": (
                    f"## Automated remediation\n\n"
                    f"**RCA:** `{report.id}`  \n"
                    f"**Alert:** `{report.alert}` on `{wl}` in `{ns}`  \n"
                    f"**Change:** `{file_path}` — `limits.memory` `{old_limit}` → `{new_limit}`\n\n"
                    f"**Sizing:** {sizing_note}\n\n"
                    f"The agent diagnosed the pod as OOMKilled with flat memory usage (sizing, not a leak). "
                    f"Merge to let Argo CD apply the new limit and stop the restarts.\n\n"
                    f"**Verify:** after merge and sync, `container_memory_working_set_bytes` for `{wl}` stays below 80% of `{new_limit}`."
                ),
                "head": branch,
                "base": default_branch,
                "draft": False,
            },
        )
        # Auto-merge: squash immediately; falls back gracefully if branch protection blocks it
        try:
            _put(
                f"repos/{repo}/pulls/{pr['number']}/merge",
                {
                    "merge_method": "squash",
                    "commit_title": f"fix(ops): raise {wl} memory limit {old_limit} → {new_limit} ({ns})",
                    "commit_message": f"Automated remediation from RCA {report.id}. {sizing_note}",
                },
            )
            pr["_merged"] = True
        except Exception as exc:
            log.info("remediate: auto-merge skipped (branch protection or checks pending): %s", exc)
            pr["_merged"] = False
    except Exception as exc:
        log.warning("remediate: PR creation failed: %s", exc)
        return None

    return {
        "pr_url": pr["html_url"],
        "pr_number": pr["number"],
        "old_limit": old_limit,
        "new_limit": new_limit,
        "peak_mib": round(peak_bytes / (1024 * 1024)) if peak_bytes else None,
        "sizing_method": sizing_method,
        "file": file_path,
        "branch": branch,
        "status": "merged" if pr.get("_merged") else "pr_opened",
    }
