"""Read-only access to the observability stack and the cluster.

Every function here is safe to hand to a model: it only reads, clamps its time range and result size,
and returns small json friendly dicts.
"""
import calendar
import collections
import json
import re
import time
from pathlib import Path

import httpx

from agent import config

_client = httpx.Client(timeout=20)

_SECRET = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|authorization)([\"'=: ]+)([^\s\"',}]+)")


def redact(text: str) -> str:
    return _SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", text)


def _clamp_minutes(minutes: int) -> int:
    return max(1, min(int(minutes), config.MAX_RANGE_MINUTES))


def _size(obj) -> int:
    return len(json.dumps(obj, default=str))


def _trim(obj):
    """Keeps a tool result under the output limit. The summary fields (counts, top messages, breakdowns) are what
    the analysis needs, so it is the long lists of samples that get shortened, never the whole result thrown away."""
    limit = config.MAX_TOOL_OUTPUT_CHARS
    if _size(obj) <= limit:
        return obj
    original = json.dumps(obj, default=str)
    obj = dict(obj)
    lists = [k for k, v in obj.items() if isinstance(v, list) and len(v) > 1]
    while _size(obj) > limit and lists:
        biggest = max(lists, key=lambda k: _size(obj[k]))
        obj[biggest] = obj[biggest][: max(1, len(obj[biggest]) // 2)]
        if len(obj[biggest]) <= 1:
            lists.remove(biggest)
        obj["truncated"] = True
    if _size(obj) > limit:  # even the summary does not fit: last resort
        return {"truncated": True, "preview": original[:limit]}
    return obj


def query_promql(query: str, minutes: int = 30, step_seconds: int = 30, end: float | None = None) -> dict:
    """Range query against mimir. Returns series with min/max/avg/last and a few sampled points."""
    end = end or time.time()
    minutes = _clamp_minutes(minutes)
    r = _client.get(
        f"{config.MIMIR_URL}/api/v1/query_range",
        params={"query": query, "start": end - minutes * 60, "end": end, "step": max(step_seconds, 15)},
    )
    r.raise_for_status()
    series = []
    for s in r.json()["data"]["result"][:20]:
        values = [(float(t), float(v)) for t, v in s["values"] if v not in ("NaN", "+Inf", "-Inf")]
        if not values:
            continue
        nums = [v for _, v in values]
        series.append(
            {
                "labels": s["metric"],
                "min": min(nums),
                "max": max(nums),
                "avg": sum(nums) / len(nums),
                "last": nums[-1],
                "points": [(int(t), round(v, 4)) for t, v in values[:: max(1, len(values) // 12)]],
            }
        )
    return _trim({"query": query, "minutes": minutes, "series": series})


def query_logql(query: str, minutes: int = 30, limit: int = 100, end: float | None = None) -> dict:
    """Log query against loki. Returns the newest lines, with secrets redacted, plus the count per message."""
    end = end or time.time()
    minutes = _clamp_minutes(minutes)
    limit = max(1, min(int(limit), config.MAX_LOG_LINES))
    r = _client.get(
        f"{config.LOKI_URL}/loki/api/v1/query_range",
        params={
            "query": query,
            "start": int((end - minutes * 60) * 1e9),
            "end": int(end * 1e9),
            "limit": limit,
            "direction": "backward",
        },
    )
    r.raise_for_status()
    lines, counts = [], {}
    # who logged it: tallies of the metadata in the log lines show if a problem is tied to one pod, node or release
    breakdown = {k: collections.Counter() for k in ("pod", "node", "version", "error_type", "dependency")}
    fields_present = set()
    for stream in r.json()["data"]["result"]:
        for ts, line in stream["values"]:
            try:
                entry = json.loads(line)
            except ValueError:
                entry = {"msg": line[:300]}
            fields_present.update(entry)
            msg = entry.get("msg", "")
            counts[msg] = counts.get(msg, 0) + 1
            for key, tally in breakdown.items():
                value = entry.get(key) or (stream["stream"].get(key) if key == "pod" else None)
                if value:
                    tally[str(value)] += 1
            lines.append(
                {
                    "ts": int(ts) // 1_000_000_000,
                    "pod": stream["stream"].get("pod"),
                    "line": redact(json.dumps(entry)[:400]),
                    "trace_id": entry.get("trace_id"),
                    "request_id": entry.get("request_id"),
                }
            )
    lines.sort(key=lambda x: -x["ts"])
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:8]
    return _trim(
        {
            "query": query,
            "total": len(lines),
            "top_messages": top,
            "breakdown": {k: dict(c.most_common(5)) for k, c in breakdown.items() if c},
            "fields_present": sorted(fields_present),
            "lines": lines[:15],
        }
    )


def search_traces(traceql: str, minutes: int = 30, limit: int = 5, end: float | None = None) -> dict:
    """TraceQL search in tempo, returns trace ids with root name and duration."""
    end = end or time.time()
    minutes = _clamp_minutes(minutes)
    r = _client.get(
        f"{config.TEMPO_URL}/api/search",
        params={"q": traceql, "limit": max(1, min(int(limit), 20)), "start": int(end - minutes * 60), "end": int(end)},
    )
    r.raise_for_status()
    found = [
        {
            "trace_id": t["traceID"],
            "root": t.get("rootTraceName"),
            "service": t.get("rootServiceName"),
            "duration_ms": t.get("durationMs"),
        }
        for t in r.json().get("traces", [])
    ]
    return _trim({"query": traceql, "traces": found})


def get_trace(trace_id: str) -> dict:
    """Condensed view of one trace: spans sorted by duration, errors flagged."""
    r = _client.get(f"{config.TEMPO_URL}/api/traces/{trace_id}")
    r.raise_for_status()
    spans = []
    for batch in r.json().get("batches", []):
        for scope in batch.get("scopeSpans", []):
            for sp in scope.get("spans", []):
                dur = (int(sp["endTimeUnixNano"]) - int(sp["startTimeUnixNano"])) / 1e6
                attrs = {a["key"]: next(iter(a["value"].values())) for a in sp.get("attributes", [])}
                spans.append(
                    {
                        "name": sp["name"],
                        "duration_ms": round(dur, 1),
                        "error": sp.get("status", {}).get("code") in ("STATUS_CODE_ERROR", 2),
                        "attributes": {k: v for k, v in attrs.items() if k.startswith(("http.", "inventory.", "error"))},
                    }
                )
    spans.sort(key=lambda s: -s["duration_ms"])
    return _trim({"trace_id": trace_id, "span_count": len(spans), "slowest": spans[:6], "errors": [s for s in spans if s["error"]][:5]})


_k8s_transport = None  # tests inject an httpx.MockTransport here


def _k8s_get(path: str, cluster: str = "") -> dict:
    """Reads one cluster's API: "platform" and "gitops" through their read-only tokens, "observability" (or empty) is the
    cluster the agent runs in. A cluster whose URL is not set falls back to the local one (single cluster setups, tests)."""
    if cluster == "platform" and config.PLATFORM_K8S_URL:
        base, creds = config.PLATFORM_K8S_URL, config.PLATFORM_DIR
    elif cluster == "gitops" and config.GITOPS_K8S_URL:
        base, creds = config.GITOPS_K8S_URL, config.GITOPS_DIR
    else:
        base, creds = config.K8S_URL, config.SA_DIR
    # httpx only takes `verify` when the client is built, not per request, so the API server CA needs its own client
    token = Path(f"{creds}/token").read_text().strip()  # re-read every time, projected tokens rotate
    kwargs = {"transport": _k8s_transport} if _k8s_transport else {"verify": f"{creds}/ca.crt"}
    with httpx.Client(timeout=20, **kwargs) as client:
        r = client.get(base + path, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json()


def get_pod_status(namespace: str, name_prefix: str, cluster: str = "platform") -> dict:
    """Phase, restarts, waiting reason and termination of pods whose name starts with name_prefix, in the named cluster.
    A pod that was never scheduled has no container statuses; it still gets an entry, with the scheduler's reason in `waiting`."""
    pods = []
    for p in _k8s_get(f"/api/v1/namespaces/{namespace}/pods", cluster=cluster)["items"]:
        if not p["metadata"]["name"].startswith(name_prefix):
            continue
        limits = {c["name"]: c.get("resources", {}).get("limits", {}).get("memory") for c in p["spec"].get("containers", [])}
        common = {
            "pod": p["metadata"]["name"],
            "phase": p["status"].get("phase"),
            "started_at": p["status"].get("startTime"),
            "node": p["spec"].get("nodeName"),
            "app_instance": p["metadata"].get("labels", {}).get("app.kubernetes.io/instance"),
        }
        statuses = p["status"].get("containerStatuses", [])
        if not statuses:
            cond = next((c for c in p["status"].get("conditions", []) if c.get("type") == "PodScheduled" and c.get("status") == "False"), {})
            pods.append({**common, "ready": False, "restarts": 0, "waiting": cond.get("reason") or p["status"].get("reason"), "waiting_message": (cond.get("message") or p["status"].get("message") or "")[:200],
                         "last_terminated": None, "memory_limit": next(iter(limits.values()), None)})
            continue
        for cs in statuses:
            # a container that was restarted shows its last exit in lastState; one that is not restarted (a Job pod) shows it in state
            term = cs.get("lastState", {}).get("terminated") or cs.get("state", {}).get("terminated") or {}
            waiting = cs["state"].get("waiting", {})
            pods.append(
                {
                    **common,
                    "ready": cs["ready"],
                    "restarts": cs["restartCount"],
                    "waiting": waiting.get("reason"),
                    "waiting_message": (waiting.get("message") or "")[:200],
                    "last_terminated": {"reason": term.get("reason"), "exit_code": term.get("exitCode"), "at": term.get("finishedAt")} if term else None,
                    "memory_limit": limits.get(cs.get("name")),
                }
            )
    return _trim({"cluster": cluster, "namespace": namespace, "pods": pods})


def get_k8s_events(namespace: str, minutes: int = 30, cluster: str = "platform", name_prefix: str = "") -> dict:
    """Recent events of a namespace, newest first. With name_prefix only events about objects of that name are kept, before the list is
    cut: in a busy namespace the newest 30 events of everything would otherwise push out the ones about the workload under investigation."""
    cutoff = time.time() - _clamp_minutes(minutes) * 60
    events = []
    for e in _k8s_get(f"/api/v1/namespaces/{namespace}/events?limit=1000", cluster=cluster)["items"]:
        obj = e["involvedObject"]["name"]
        if name_prefix and not (obj.startswith(name_prefix) or name_prefix.startswith(obj)):
            continue
        ts = e.get("lastTimestamp") or (e.get("series") or {}).get("lastObservedTime") or e.get("eventTime") or e["metadata"]["creationTimestamp"]
        epoch = calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
        if epoch < cutoff:
            continue
        events.append({"ts": ts, "type": e["type"], "reason": e["reason"], "object": obj, "message": e["message"][:500], "count": e.get("count", 1)})
    events.sort(key=lambda e: e["ts"], reverse=True)
    return _trim({"cluster": cluster, "namespace": namespace, "events": events[:30]})


def _deployed_revision(entry: dict) -> str:
    """The commit one Argo CD sync deployed. An application with one source records `revision`; one with several sources (a
    Helm chart plus the values from this repo) records `revisions`, one per source: the chart version and the git commit.
    The commit is what changes with a deploy, so it is the one reported."""
    revisions = [r for r in (entry.get("revisions") or [entry.get("revision")]) if r]
    commits = [r for r in revisions if re.fullmatch(r"[0-9a-f]{40}", r)]
    return (commits or revisions or [""])[0][:10]


def get_github_deploy_info(revision: str, repo: str = "") -> dict:
    """Commit message and associated pull request for a deployed git revision."""
    if not config.GITHUB_TOKEN:
        return {"error": "GITHUB_TOKEN not configured"}
    r = repo or config.GITOPS_REPO
    try:
        resp = _client.get(
            f"https://api.github.com/repos/{r}/commits/{revision}",
            headers=config.github_headers(),
            timeout=10,
        )
        if resp.status_code == 404:
            return {"error": f"commit {revision!r} not found in {r}"}
        resp.raise_for_status()
        data = resp.json()
        message = (data.get("commit", {}).get("message") or "").split("\n")[0][:200]
        sha = data.get("sha", revision)
        pr_num, pr_title, pr_url = None, None, None
        m = re.search(r"[Mm]erge pull request #(\d+)|#(\d+)\)", message)
        if m:
            pr_num = int(m.group(1) or m.group(2))
            pr_resp = _client.get(
                f"https://api.github.com/repos/{r}/pulls/{pr_num}",
                headers=config.github_headers(), timeout=10,
            )
            if pr_resp.status_code == 200:
                pr_data = pr_resp.json()
                pr_title = pr_data.get("title", "")[:200]
                pr_url = pr_data.get("html_url", "")
        else:
            pr_resp = _client.get(
                f"https://api.github.com/repos/{r}/commits/{sha}/pulls",
                headers=config.github_headers(), timeout=10,
            )
            if pr_resp.status_code == 200:
                prs = pr_resp.json()
                if prs:
                    pr_num = prs[0].get("number")
                    pr_title = prs[0].get("title", "")[:200]
                    pr_url = prs[0].get("html_url", "")
        result: dict = {"revision": sha[:10], "message": message}
        if pr_num:
            result.update({"pr_number": pr_num, "pr_title": pr_title, "pr_url": pr_url})
        return result
    except Exception as e:
        return {"error": str(e)[:120]}


def get_recent_deploys(application: str) -> dict:
    """Argo CD sync history for an application, newest first."""
    # every application is named <cluster>-<component> in the Argo CD that deploys the three clusters
    for name in (f"platform-{application}", f"observability-{application}", f"gitops-{application}", application):
        try:
            app = _k8s_get(f"/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/{name}", cluster="gitops")
            break
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 404:
                raise
    else:
        raise LookupError(f"no Argo CD application platform-{application}, observability-{application}, gitops-{application} or {application}")
    history = app.get("status", {}).get("history", [])[-20:]
    spec = app.get("spec", {})
    files = []
    for src in spec.get("sources") or [spec.get("source", {})]:
        if src.get("path"):
            files.append(src["path"])
        files += [f.replace("$values/", "") for f in src.get("helm", {}).get("valueFiles", [])]
    return _trim(
        {
            "application": application,
            "source_files": files,
            "history": [
                {"deployed_at": h.get("deployedAt"), "revision": _deployed_revision(h), "id": h.get("id")}
                for h in reversed(history)
            ],
        }
    )


def list_namespace_deploys(namespace: str, cluster: str = "platform", minutes: int = 120) -> dict:
    """All Argo CD applications that target a namespace and their recent sync history.

    Useful when the alert service has no matching named ArgoCD app, or when you want
    to find any deploy in the namespace (ingress, config, shared infra) that could
    have caused the incident.
    """
    cutoff_epoch = __import__("time").time() - minutes * 60
    all_apps = _k8s_get("/apis/argoproj.io/v1alpha1/namespaces/argocd/applications", cluster="gitops")
    results = []
    for app in all_apps.get("items", []):
        spec = app.get("spec", {})
        dest = spec.get("destination", {})
        dest_ns = dest.get("namespace", "")
        dest_cluster = dest.get("name") or dest.get("server", "")
        # match namespace directly, or apps that target the cluster and use the namespace in sources
        if dest_ns != namespace and cluster not in dest_cluster:
            continue
        if dest_ns and dest_ns != namespace:
            continue
        history = app.get("status", {}).get("history", [])[-20:]
        recent = []
        for h in reversed(history):
            ts = h.get("deployedAt", "")
            try:
                from datetime import datetime, timezone
                t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            if t >= cutoff_epoch:
                recent.append({"deployed_at": ts, "revision": _deployed_revision(h), "id": h.get("id")})
        if recent:
            app_name = app["metadata"]["name"]
            sources = spec.get("sources") or [spec.get("source", {})]
            paths = [s.get("path") for s in sources if s.get("path")]
            results.append({"application": app_name, "destination_namespace": dest_ns, "paths": paths, "history": recent})
    results.sort(key=lambda x: x["history"][0]["deployed_at"] if x["history"] else "", reverse=True)
    return _trim({"namespace": namespace, "cluster": cluster, "minutes": minutes, "apps": results})


def list_all_deploys(minutes: int = 1440) -> dict:
    """All Argo CD applications across the entire cluster with recent sync history (no namespace filter).

    Use as a last-resort fallback when targeted and namespace scans find nothing — scans every app
    ArgoCD manages and returns those with a deploy in the look-back window.
    """
    cutoff_epoch = __import__("time").time() - minutes * 60
    all_apps = _k8s_get("/apis/argoproj.io/v1alpha1/namespaces/argocd/applications", cluster="gitops")
    results = []
    for app in all_apps.get("items", []):
        history = app.get("status", {}).get("history", [])[-20:]
        recent = []
        for h in reversed(history):
            ts = h.get("deployedAt", "")
            try:
                from datetime import datetime, timezone as _tz
                t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            if t >= cutoff_epoch:
                recent.append({"deployed_at": ts, "revision": _deployed_revision(h), "id": h.get("id")})
        if recent:
            spec = app.get("spec", {})
            app_name = app["metadata"]["name"]
            dest_ns = spec.get("destination", {}).get("namespace", "")
            sources = spec.get("sources") or [spec.get("source", {})]
            paths = [s.get("path") for s in sources if s.get("path")]
            results.append({"application": app_name, "destination_namespace": dest_ns, "paths": paths, "history": recent})
    results.sort(key=lambda x: x["history"][0]["deployed_at"] if x["history"] else "", reverse=True)
    return _trim({"minutes": minutes, "apps": results})


def get_argocd_app(name: str) -> dict:
    """Sync and health of one Argo CD application: conditions, last operation message and the resources that are not healthy."""
    app = None
    for candidate in (name, f"platform-{name}", f"observability-{name}", f"gitops-{name}"):
        try:
            app = _k8s_get(f"/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/{candidate}", cluster="gitops")
            break
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 404:
                raise
    if app is None:
        raise LookupError(f"no Argo CD application {name}")
    status, spec = app.get("status", {}), app.get("spec", {})
    dest = spec.get("destination", {})
    unhealthy = [
        f"{r.get('kind')}/{r.get('name')}: {(r.get('health') or {}).get('status', r.get('status', ''))} {((r.get('health') or {}).get('message') or '')[:80]}".strip()
        for r in status.get("resources", [])
        if (r.get("health") or {}).get("status") not in (None, "Healthy") or r.get("status") == "OutOfSync"
    ]
    op = status.get("operationState") or {}
    # last deployed revision and time from sync history (newest entry)
    history = status.get("history") or []
    last_entry = history[-1] if history else {}
    last_revision = _deployed_revision(last_entry) if last_entry else ""
    last_deployed_at = last_entry.get("deployedAt", "")
    return _trim(
        {
            "name": app["metadata"]["name"],
            "sync_status": (status.get("sync") or {}).get("status"),
            "health_status": (status.get("health") or {}).get("status"),
            "destination": dest.get("name") or dest.get("server"),
            "destination_namespace": dest.get("namespace"),
            "paths": [s.get("path") for s in (spec.get("sources") or [spec.get("source", {})]) if s.get("path")],
            "last_revision": last_revision,
            "last_deployed_at": last_deployed_at,
            "conditions": [f"{c.get('type')}: {c.get('message', '')[:200]}" for c in status.get("conditions", [])],
            "operation": {"phase": op.get("phase"), "message": (op.get("message") or "")[:300]} if op else None,
            "unhealthy": unhealthy[:8],
        }
    )


# name -> (function, json schema) used by the model tool loop
TOOLS = {
    "query_promql": (
        query_promql,
        {
            "description": "Run a PromQL range query on Mimir. Use for rates, latency percentiles, restarts, replica counts.",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "minutes": {"type": "integer", "description": "look back window, max 120"}},
                "required": ["query"],
            },
        },
    ),
    "query_logql": (
        query_logql,
        {
            "description": "Run a LogQL query on Loki. Labels: namespace, service, pod, container. Logs are json with level, msg, trace_id.",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "minutes": {"type": "integer"}, "limit": {"type": "integer"}},
                "required": ["query"],
            },
        },
    ),
    "search_traces": (
        search_traces,
        {
            "description": 'Search Tempo with TraceQL, for example { resource.service.name = "orders-api" && duration > 500ms } or { status = error }.',
            "input_schema": {
                "type": "object",
                "properties": {"traceql": {"type": "string"}, "minutes": {"type": "integer"}, "limit": {"type": "integer"}},
                "required": ["traceql"],
            },
        },
    ),
    "get_trace": (
        get_trace,
        {
            "description": "Fetch one trace by id and return its slowest spans and any error spans.",
            "input_schema": {"type": "object", "properties": {"trace_id": {"type": "string"}}, "required": ["trace_id"]},
        },
    ),
    "get_pod_status": (
        get_pod_status,
        {
            "description": "Pod phase, restart count and last termination reason (for example OOMKilled) for pods with a name prefix.",
            "input_schema": {
                "type": "object",
                "properties": {"namespace": {"type": "string"}, "name_prefix": {"type": "string"}, "cluster": {"type": "string", "description": "platform (default), observability or gitops"}},
                "required": ["namespace", "name_prefix"],
            },
        },
    ),
    "get_k8s_events": (
        get_k8s_events,
        {
            "description": "Recent Kubernetes events in a namespace (kills, probe failures, scheduling).",
            "input_schema": {"type": "object", "properties": {"namespace": {"type": "string"}, "minutes": {"type": "integer"}, "cluster": {"type": "string", "description": "platform (default), observability or gitops"}, "name_prefix": {"type": "string", "description": "only events about objects whose name starts with this"}}, "required": ["namespace"]},
        },
    ),
    "get_argocd_app": (
        get_argocd_app,
        {
            "description": "Sync status, health, last operation message and unhealthy resources of an Argo CD application (apps of the platform cluster are named prod-<name>).",
            "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        },
    ),
    "get_recent_deploys": (
        get_recent_deploys,
        {
            "description": "Argo CD sync history for an application, to see whether a deploy preceded the incident.",
            "input_schema": {"type": "object", "properties": {"application": {"type": "string"}}, "required": ["application"]},
        },
    ),
    "list_namespace_deploys": (
        list_namespace_deploys,
        {
            "description": "All Argo CD applications that target a namespace and their recent sync history. Use when get_recent_deploys finds no deploy, or to find any related deploys (ingress, config, shared infra) in the same namespace.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "cluster": {"type": "string", "description": "platform (default), observability or gitops"},
                    "minutes": {"type": "integer", "description": "look-back window in minutes, default 120"},
                },
                "required": ["namespace"],
            },
        },
    ),
    "list_all_deploys": (
        list_all_deploys,
        {
            "description": "All Argo CD applications across the entire cluster with recent sync history (no namespace filter). Last-resort fallback when targeted and namespace scans find nothing.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "minutes": {"type": "integer", "description": "look-back window in minutes, default 1440 (24 h)"},
                },
            },
        },
    ),
}


def call_tool(name: str, args: dict) -> dict:
    if name not in TOOLS:
        return {"error": f"unknown tool {name}"}
    try:
        return TOOLS[name][0](**args)
    except Exception as exc:  # tool errors go back to the model as data
        return {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
