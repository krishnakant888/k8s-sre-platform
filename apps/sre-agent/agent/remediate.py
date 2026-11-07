"""Automated remediation: open a GitHub PR to raise a memory limit when the agent diagnoses a sizing OOM.

Conditions (all must hold):
  - root_cause.category == "resource_exhaustion" with infrastructure layer (sizing, not a leak)
  - memory growth is flat (no leak signal)
  - GITHUB_TOKEN is configured
  - The workload has an Argo CD source directory with a YAML file containing resources.limits.memory

The PR is a 1.5× bump rounded up to the next 128 Mi / 0.5 Gi boundary. It is opened as a draft so a
human reviews it before Argo CD applies it.
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


def _bump(limit: str) -> str | None:
    m = re.fullmatch(r"(\d+)(Mi|Gi)", limit)
    if not m:
        return None
    val, unit = int(m.group(1)), m.group(2)
    if unit == "Gi":
        new = max(val + 1, int(val * 1.5 + 0.5))
        return f"{new}Gi"
    # round 1.5× up to the next 128 Mi boundary; guarantee at least one boundary higher
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
    fd = report.fault_domain
    if rc.category != "resource_exhaustion" or fd.layer != "infrastructure":
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
    new_limit = _bump(old_limit)
    if not new_limit:
        return None

    new_content = _patch_content(content, old_limit, new_limit)
    if new_content == content:
        log.warning("remediate: patch produced no change for %s", file_path)
        return None

    branch = f"remediate/oom-{wl}-{ns}-{int(time.time())}"
    commit_msg = f"fix(ops): raise memory limit of {wl} in {ns} from {old_limit} to {new_limit}\n\nAutomated remediation triggered by RCA {report.id}.\nThe pod was OOMKilled; memory usage was flat (not a leak) so raising the limit is safe.\nReview and merge to let Argo CD apply it."

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
                    f"The agent diagnosed the pod as OOMKilled with flat memory usage (sizing, not a leak). "
                    f"This PR raises the limit by ~50%. Review the peak usage before merging; "
                    f"revert or adjust if the new limit seems too high or too low.\n\n"
                    f"**Verify:** after merge and sync, check `container_memory_working_set_bytes` stays below 80% of the new limit."
                ),
                "head": branch,
                "base": default_branch,
                "draft": True,
            },
        )
    except Exception as exc:
        log.warning("remediate: PR creation failed: %s", exc)
        return None

    return {
        "pr_url": pr["html_url"],
        "pr_number": pr["number"],
        "old_limit": old_limit,
        "new_limit": new_limit,
        "file": file_path,
        "branch": branch,
        "status": "pr_opened",
    }
