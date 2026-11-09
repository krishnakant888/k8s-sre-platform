"""Automated remediation: raises HPA maxReplicas when the autoscaler is ceiling-bound.

VPA Auto manages CPU and memory limits+requests at runtime — the agent does not touch them.
KEDA manages pod count via RPS metric — the agent raises maxReplicas only when KEDA is saturated.

Conditions (all must hold):
  - root_cause.category == "resource_exhaustion" with CPU throttling
  - HPA/KEDA is at maxReplicas (saturated)
  - GITHUB_TOKEN is configured
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


def _hpa_is_saturated(ns: str, wl: str) -> bool | None:
    """Return True if HPA is at maxReplicas, False if it has room, None if no HPA / query error."""
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
            return None
        return float(cur_res[0]["value"][1]) >= float(max_res[0]["value"][1])
    except Exception as exc:
        log.debug("remediate: hpa saturation query failed for %s/%s: %s", ns, wl, exc)
        return None


def _load_hpa_manifest(repo: str, workload: str, default_branch: str) -> tuple[str, str, str] | None:
    """Return (file_path, content, blob_sha) for the workload's hpa.yaml, or None."""
    path = f"gitops/workloads/{workload}/hpa.yaml"
    try:
        data = _get(f"repos/{repo}/contents/{path}", params={"ref": default_branch})
        content = base64.b64decode(data["content"]).decode()
        return path, content, data["sha"]
    except Exception:
        return None


def _open_and_merge_pr(
    repo: str, default_branch: str, head_sha: str,
    branch: str, file_path: str, blob_sha: str, new_content: str,
    commit_msg: str, pr_title: str, pr_body: str,
) -> dict:
    """Create branch + commit + PR, attempt immediate squash merge."""
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


def _remediate_hpa_maxreplicas(ctx: dict, report, repo: str, default_branch: str, head_sha: str) -> dict | None:
    """Raise maxReplicaCount in the KEDA ScaledObject when HPA is saturated."""
    sig = ctx["signals"]
    wl, ns = sig["workload"], sig["namespace"]

    result = _load_hpa_manifest(repo, wl, default_branch)
    if not result:
        log.info("remediate: no hpa.yaml found for %s", wl)
        return None
    file_path, content, blob_sha = result

    # KEDA ScaledObject uses maxReplicaCount; fallback to HPA maxReplicas
    m = re.search(r"(maxReplicaCount|maxReplicas):\s*(\d+)", content)
    if not m:
        return None
    field, old_max = m.group(1), int(m.group(2))
    new_max = max(old_max + 2, math.ceil(old_max * 1.5))

    patched = re.sub(rf"({re.escape(field)}:\s*)\d+", lambda mo: mo.group(1) + str(new_max), content, count=1)

    branch = f"remediate/hpa-maxreplicas-{wl}-{ns}-{int(time.time())}"
    commit_msg = (
        f"fix(ops): raise {wl} maxReplicas {old_max} → {new_max} in {ns}\n\n"
        f"Automated remediation triggered by RCA {report.id}.\n"
        f"KEDA/HPA was at ceiling while service was overloaded; raising the ceiling to let it scale out.\n"
    )
    pr_title = f"fix(ops): raise {wl} maxReplicas {old_max} → {new_max} ({ns})"
    pr_body = (
        f"## Automated remediation — maxReplicas raised\n\n"
        f"**RCA:** `{report.id}`  \n"
        f"**Alert:** `{report.alert}` on `{wl}` in `{ns}`  \n"
        f"**Change:** `{file_path}` — `{field}` `{old_max}` → `{new_max}`\n\n"
        f"**Why:** KEDA was at its ceiling (`{old_max}` replicas) while the service was overloaded. "
        f"VPA Auto handles per-pod sizing; this PR gives KEDA room to scale out further.\n\n"
        f"**Verify:** replica count rises above `{old_max}` and error rate drops.\n"
    )

    try:
        pr = _open_and_merge_pr(
            repo, default_branch, head_sha, branch, file_path, blob_sha, patched,
            commit_msg, pr_title, pr_body,
        )
    except Exception as exc:
        log.warning("remediate: hpa maxreplicas PR failed: %s", exc)
        return None

    return {
        "type": "hpa_maxreplicas",
        "pr_url": pr["html_url"],
        "pr_number": pr["number"],
        "old_max_replicas": old_max,
        "new_max_replicas": new_max,
        "file": file_path,
        "branch": branch,
        "status": "merged" if pr.get("_merged") else "pr_opened",
    }


# ── entry point ───────────────────────────────────────────────────────────────

def maybe_open_pr(ctx: dict, report) -> dict | None:
    """Raise maxReplicas when KEDA/HPA is saturated and the service is overloaded.

    OOM and CPU throttle sizing are handled by VPA Auto — no limit PRs here.
    """
    if not config.GITHUB_TOKEN:
        return None
    if report.root_cause.category != "resource_exhaustion":
        return None

    sig = ctx["signals"]
    if not sig.get("source_files"):
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

    # OOM or CPU throttle: VPA Auto handles sizing. Only act when HPA is also saturated.
    is_resource_exhaustion = (
        "oomkill" in evidence_text or "oom" in evidence_text or "exit code 137" in evidence_text
        or sig.get("cpu_throttled") or "throttl" in evidence_text
        or report.alert in ("ContainerMemoryNearLimit", "PodOOMKilled", "ContainerCpuThrottled")
    )
    if not is_resource_exhaustion:
        return None

    saturated = _hpa_is_saturated(sig["namespace"], sig["workload"])
    if saturated is False:
        log.info("remediate: resource exhaustion on %s but HPA/KEDA has room — VPA will handle sizing", sig["workload"])
        return None
    if saturated is None:
        log.info("remediate: no HPA/KEDA found for %s — VPA will handle sizing", sig["workload"])
        return None

    return _remediate_hpa_maxreplicas(ctx, report, config.GITOPS_REPO, default_branch, head_sha)
