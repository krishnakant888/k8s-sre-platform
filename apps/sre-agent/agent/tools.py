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


def _k8s_get(path: str) -> dict:
    # httpx only takes `verify` when the client is built, not per request, so the API server CA needs its own client
    token = Path(f"{config.SA_DIR}/token").read_text()  # re-read every time, projected tokens rotate
    kwargs = {"transport": _k8s_transport} if _k8s_transport else {"verify": f"{config.SA_DIR}/ca.crt"}
    with httpx.Client(timeout=20, **kwargs) as client:
        r = client.get(config.K8S_URL + path, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json()


def get_pod_status(namespace: str, name_prefix: str) -> dict:
    """Phase, restarts and last termination reason of pods whose name starts with name_prefix."""
    pods = []
    for p in _k8s_get(f"/api/v1/namespaces/{namespace}/pods")["items"]:
        if not p["metadata"]["name"].startswith(name_prefix):
            continue
        limits = {c["name"]: c.get("resources", {}).get("limits", {}).get("memory") for c in p["spec"].get("containers", [])}
        for cs in p["status"].get("containerStatuses", []):
            last = cs.get("lastState", {}).get("terminated", {})
            pods.append(
                {
                    "pod": p["metadata"]["name"],
                    "phase": p["status"]["phase"],
                    "ready": cs["ready"],
                    "restarts": cs["restartCount"],
                    "waiting": cs["state"].get("waiting", {}).get("reason"),
                    "last_terminated": {"reason": last.get("reason"), "exit_code": last.get("exitCode"), "at": last.get("finishedAt")} if last else None,
                    "started_at": p["status"].get("startTime"),
                    "memory_limit": limits.get(cs.get("name")),
                    "node": p["spec"].get("nodeName"),
                    "app_instance": p["metadata"].get("labels", {}).get("app.kubernetes.io/instance"),
                }
            )
    return _trim({"namespace": namespace, "pods": pods})


def get_k8s_events(namespace: str, minutes: int = 30) -> dict:
    cutoff = time.time() - _clamp_minutes(minutes) * 60
    events = []
    for e in _k8s_get(f"/api/v1/namespaces/{namespace}/events")["items"]:
        ts = e.get("lastTimestamp") or e.get("eventTime") or e["metadata"]["creationTimestamp"]
        epoch = calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
        if epoch < cutoff:
            continue
        events.append({"ts": ts, "type": e["type"], "reason": e["reason"], "object": e["involvedObject"]["name"], "message": e["message"][:200]})
    events.sort(key=lambda e: e["ts"], reverse=True)
    return _trim({"namespace": namespace, "events": events[:30]})


def get_recent_deploys(application: str) -> dict:
    """Argo CD sync history for an application, newest first."""
    app = _k8s_get(f"/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/{application}")
    history = app.get("status", {}).get("history", [])[-6:]
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
                {"deployed_at": h.get("deployedAt"), "revision": (h.get("revision") or "")[:10], "id": h.get("id")}
                for h in reversed(history)
            ],
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
                "properties": {"namespace": {"type": "string"}, "name_prefix": {"type": "string"}},
                "required": ["namespace", "name_prefix"],
            },
        },
    ),
    "get_k8s_events": (
        get_k8s_events,
        {
            "description": "Recent Kubernetes events in a namespace (kills, probe failures, scheduling).",
            "input_schema": {"type": "object", "properties": {"namespace": {"type": "string"}, "minutes": {"type": "integer"}}, "required": ["namespace"]},
        },
    ),
    "get_recent_deploys": (
        get_recent_deploys,
        {
            "description": "Argo CD sync history for an application, to see whether a deploy preceded the incident.",
            "input_schema": {"type": "object", "properties": {"application": {"type": "string"}}, "required": ["application"]},
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
