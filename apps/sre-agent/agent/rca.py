"""Evidence gathering and the rule based analysis.

gather() runs a fixed playbook of read-only queries around the alert. analyze_rules() turns that
evidence into an RcaReport without a model. The LLM path (agent/llm.py) starts from the same evidence.
"""
import re
import time
from datetime import datetime, timezone

from agent import tools
from agent.models import Evidence, Impact, RcaReport, RootCause, TimelineEntry

DEFAULT_NAMESPACE = "demo"


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def service_of(alert: dict) -> str:
    labels = alert.get("labels", {})
    if labels.get("service"):
        return labels["service"]
    if labels.get("deployment"):
        return labels["deployment"]
    if labels.get("pod"):
        return re.sub(r"-[a-z0-9]{8,10}-[a-z0-9]{5}$|-\d+$", "", labels["pod"])
    return "orders-api"


def _first(result: dict, fn=max, key="max", default=None):
    series = result.get("series") or []
    vals = [s[key] for s in series if key in s]
    return fn(vals) if vals else default


def gather(alert: dict, now: float | None = None, lookback_minutes: int = 45) -> dict:
    """Run the playbook. Returns {'service', 'namespace', 'signals', 'evidence', 'raw'}."""
    now = now or time.time()
    svc = service_of(alert)
    ns = alert.get("labels", {}).get("namespace", DEFAULT_NAMESPACE)
    started = parse_ts(alert["startsAt"]) if alert.get("startsAt") else now
    evidence: list[dict] = []
    raw: dict = {}
    sig: dict = {}

    def add(source, query, finding):
        evidence.append({"source": source, "query": query, "finding": finding})

    # --- metrics
    q_bad = (
        f'1 - (sum(rate(http_request_duration_seconds_bucket{{service="{svc}", le="0.3", status!~"5.."}}[1m]))'
        f' / sum(rate(http_request_duration_seconds_count{{service="{svc}"}}[1m])))'
    )
    bad = tools.call_tool("query_promql", {"query": q_bad, "minutes": lookback_minutes})
    sig["bad_ratio_peak"] = _first(bad, max, "max", 0.0) or 0.0
    add("mimir", q_bad, f"bad request ratio (5xx or >300ms) peaked at {sig['bad_ratio_peak']:.1%}, last value {(_first(bad, max, 'last', 0) or 0):.1%}")

    q_p99 = f'histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket{{service="{svc}"}}[2m])))'
    p99 = tools.call_tool("query_promql", {"query": q_p99, "minutes": lookback_minutes})
    sig["p99_peak"] = _first(p99, max, "max", 0.0) or 0.0
    sig["p99_baseline"] = _first(p99, min, "min", 0.0) or 0.0
    add("mimir", q_p99, f"p99 latency ranged from {sig['p99_baseline'] * 1000:.0f}ms to {sig['p99_peak'] * 1000:.0f}ms")

    q_5xx = f'sum(rate(http_requests_total{{service="{svc}", status=~"5.."}}[1m])) / sum(rate(http_requests_total{{service="{svc}"}}[1m]))'
    e5 = tools.call_tool("query_promql", {"query": q_5xx, "minutes": lookback_minutes})
    sig["error_share_peak"] = _first(e5, max, "max", 0.0) or 0.0
    add("mimir", q_5xx, f"5xx share of responses peaked at {sig['error_share_peak']:.1%}")

    q_rep = f'min(kube_deployment_status_replicas_available{{namespace="{ns}", deployment="{svc}"}})'
    rep = tools.call_tool("query_promql", {"query": q_rep, "minutes": lookback_minutes})
    sig["replicas_min"] = _first(rep, min, "min", None)
    add("mimir", q_rep, f"available replicas dropped to {sig['replicas_min']}" if sig["replicas_min"] is not None else "no replica data")

    q_rst = f'sum(increase(kube_pod_container_status_restarts_total{{namespace="{ns}", pod=~"{svc}-.*"}}[{lookback_minutes}m]))'
    rst = tools.call_tool("query_promql", {"query": q_rst, "minutes": 5})
    sig["restarts"] = _first(rst, max, "last", 0.0) or 0.0
    add("mimir", q_rst, f"{sig['restarts']:.0f} container restarts in the last {lookback_minutes}m")

    q_chaos = f'max(chaos_active{{service="{svc}"}})'
    ch = tools.call_tool("query_promql", {"query": q_chaos, "minutes": lookback_minutes})
    sig["fault_injection"] = (_first(ch, max, "max", 0.0) or 0.0) >= 1

    # --- logs
    q_logs = f'{{namespace="{ns}", service="{svc}"}} | json | level=~"error|warning"'
    logs = tools.call_tool("query_logql", {"query": q_logs, "minutes": lookback_minutes, "limit": 150})
    raw["logs"] = logs
    top = logs.get("top_messages", [])
    sig["top_log_messages"] = top
    sig["log_trace_ids"] = [l["trace_id"] for l in logs.get("lines", []) if l.get("trace_id")]
    add("loki", q_logs, "top warning/error messages: " + ("; ".join(f"{m!r} x{c}" for m, c in top[:4]) if top else "none"))

    # --- traces
    q_slow = f'{{ resource.service.name = "{svc}" && duration > 500ms }}'
    slow = tools.call_tool("search_traces", {"traceql": q_slow, "minutes": lookback_minutes, "limit": 5})
    q_err = f'{{ resource.service.name = "{svc}" && status = error }}'
    errs = tools.call_tool("search_traces", {"traceql": q_err, "minutes": lookback_minutes, "limit": 5})
    sig["slow_traces"] = len(slow.get("traces", []))
    sig["error_traces"] = len(errs.get("traces", []))
    sample = (slow.get("traces") or errs.get("traces") or [{}])[0].get("trace_id") or (sig["log_trace_ids"] or [None])[0]
    sig["slow_span"] = None
    if sample:
        tr = tools.call_tool("get_trace", {"trace_id": sample})
        raw["trace"] = tr
        spans = [s for s in tr.get("slowest", []) if "." in s["name"]]  # child spans are named dependency.operation
        if tr.get("errors"):
            sig["error_span"] = tr["errors"][0]["name"]
        if spans:
            sig["slow_span"] = spans[0]
            add("tempo", f"trace {sample}", f"slowest child span is {spans[0]['name']} at {spans[0]['duration_ms']}ms of the request")
    add("tempo", f"{q_slow} / {q_err}", f"{sig['slow_traces']} traces slower than 500ms, {sig['error_traces']} error traces in the window")

    # --- kubernetes and deploys
    pods = tools.call_tool("get_pod_status", {"namespace": ns, "name_prefix": svc})
    raw["pods"] = pods
    sig["oom"] = any((p.get("last_terminated") or {}).get("reason") == "OOMKilled" for p in pods.get("pods", []))
    sig["not_ready"] = [p["pod"] for p in pods.get("pods", []) if not p["ready"]]
    sig["pod_restarts_total"] = sum(p["restarts"] for p in pods.get("pods", []))
    add("kubernetes", f"pods {ns}/{svc}-*", f"{len(pods.get('pods', []))} pods, restarts={sig['pod_restarts_total']}, oom_killed={sig['oom']}, not_ready={sig['not_ready']}")

    events = tools.call_tool("get_k8s_events", {"namespace": ns, "minutes": lookback_minutes})
    raw["events"] = events
    sig["kill_events"] = [e for e in events.get("events", []) if e["reason"] in ("Killing", "OOMKilling", "BackOff", "Unhealthy")]
    if sig["kill_events"]:
        add("kubernetes", f"events {ns}", "; ".join(f"{e['reason']} {e['object']}" for e in sig["kill_events"][:4]))

    deploys = tools.call_tool("get_recent_deploys", {"application": svc})
    raw["deploys"] = deploys
    recent = [d for d in deploys.get("history", []) if d.get("deployed_at") and parse_ts(d["deployed_at"]) > started - 3600]
    sig["recent_deploy"] = recent[0] if recent else None
    add("argocd", f"history {svc}", f"deploy {recent[0]['revision']} at {recent[0]['deployed_at']}" if recent else "no deploy in the hour before the alert")

    return {"service": svc, "namespace": ns, "started": started, "now": now, "signals": sig, "evidence": evidence, "raw": raw}


def classify(sig: dict) -> tuple[str, str, str, list[str]]:
    """Returns (category, description, confidence, contributing factors)."""
    contrib: list[str] = []
    msgs = " ".join(m for m, _ in sig.get("top_log_messages", [])).lower()
    slow = sig.get("slow_span")
    dep_slow = bool(slow and slow["duration_ms"] > 300)
    errors = sig["error_share_peak"] > 0.05
    latency = sig["p99_peak"] > 0.5
    dep_errors = "unavailable" in msgs or "lookup failed" in msgs or sig.get("error_span", "").startswith("inventory")

    if sig.get("fault_injection"):
        contrib.append("a fault injection flag (chaos_active) is set on the service, this looks like a drill")
    if sig.get("recent_deploy"):
        contrib.append(f"a deploy ({sig['recent_deploy']['revision']}) landed within the hour before the alert")

    if sig["oom"]:
        return ("resource_exhaustion", "A container was OOMKilled: memory use exceeded its limit and the kernel killed it, causing restarts and a burst of failed requests.", "high", contrib)
    if sig["restarts"] >= 1 or (sig["replicas_min"] is not None and sig["replicas_min"] < 1):
        why = "Pods were restarted or unavailable"
        if sig["kill_events"]:
            why += f" ({sig['kill_events'][0]['reason']} on {sig['kill_events'][0]['object']})"
        return ("pod_crash", why + ", which removed serving capacity and failed in-flight requests.", "medium" if sig["not_ready"] else "high", contrib)
    if errors and dep_errors:
        return ("dependency_errors", "Requests are failing with 5xx because the downstream dependency (inventory lookup) returns errors; the error spans and error logs both point at it.", "high", contrib)
    if latency and dep_slow:
        return ("dependency_latency", f"Latency rose because the downstream call {slow['name']} became slow ({slow['duration_ms']}ms inside the request); the service itself is healthy.", "high", contrib)
    if sig.get("recent_deploy") and (errors or latency):
        return ("deploy_regression", f"Errors/latency started shortly after deploy {sig['recent_deploy']['revision']}.", "medium", contrib)
    if errors or latency:
        return ("unknown", "The SLO is burning but no single cause stands out in the metrics, logs or traces.", "low", contrib)
    return ("unknown", "The signal has already recovered or is too small to attribute.", "low", contrib)


ACTIONS = {
    "dependency_latency": [
        "Check the health and saturation of the downstream dependency named in the slow span.",
        "Consider a timeout and circuit breaker on that call so slow dependencies do not consume request time.",
        "If this was a fault injection drill, reset it (POST /chaos/reset) and confirm p99 returns under 300ms.",
    ],
    "dependency_errors": [
        "Check the downstream dependency's own logs and availability.",
        "Add retries with backoff and a fallback for the failing call.",
        "If this was a fault injection drill, reset it (POST /chaos/reset) and confirm the error ratio recovers.",
    ],
    "pod_crash": [
        "Inspect the pod events and previous container logs (kubectl logs --previous).",
        "Check liveness/readiness probe settings and recent changes to the deployment.",
        "Keep at least 2 replicas with a PodDisruptionBudget and spread them across nodes.",
    ],
    "resource_exhaustion": [
        "Raise the memory limit or fix the leak: compare container_memory_working_set_bytes with the limit.",
        "Add a memory based alert at 80% of the limit so this is caught before the kill.",
    ],
    "deploy_regression": [
        "Roll back by reverting the image tag commit in Git and let Argo CD sync.",
        "Add a canary or progressive rollout so regressions affect fewer users.",
    ],
    "unknown": [
        "Widen the time window and compare against the same time yesterday.",
        "Check the dashboard for correlated changes in traffic volume.",
    ],
}


def analyze_rules(alert: dict, ctx: dict, now: float | None = None) -> RcaReport:
    now = now or time.time()
    sig = ctx["signals"]
    svc = ctx["service"]
    category, description, confidence, contrib = classify(sig)
    started = ctx["started"]
    alertname = alert.get("labels", {}).get("alertname", "unknown")
    severity = alert.get("labels", {}).get("severity", "unknown")

    timeline = [TimelineEntry(time=iso(started), event=f"alert {alertname} started firing")]
    if sig.get("recent_deploy"):
        timeline.insert(0, TimelineEntry(time=sig["recent_deploy"]["deployed_at"], event=f"deploy {sig['recent_deploy']['revision']} synced by Argo CD"))
    for e in sig.get("kill_events", [])[:3]:
        timeline.append(TimelineEntry(time=e["ts"], event=f"{e['reason']}: {e['object']}"))
    timeline.append(TimelineEntry(time=iso(now), event="investigation completed"))

    impact = Impact(
        slo="99.5% of requests succeed and finish within 300ms over 28 days",
        bad_request_ratio=round(sig["bad_ratio_peak"], 4),
        p99_latency_seconds=round(sig["p99_peak"], 3),
        availability_replicas=sig["replicas_min"],
        summary=f"Up to {sig['bad_ratio_peak']:.1%} of requests were bad (budget allows 0.5%), p99 peaked at {sig['p99_peak'] * 1000:.0f}ms.",
    )
    summary = f"{alertname} on {svc}: {description}"
    report = RcaReport(
        id=f"rca-{svc}-{datetime.fromtimestamp(started, timezone.utc).strftime('%Y%m%d-%H%M%S')}",
        alert=alertname,
        service=svc,
        severity=severity,
        started_at=iso(started),
        generated_at=iso(now),
        mode="rule_based",
        summary=summary,
        impact=impact,
        timeline=timeline,
        root_cause=RootCause(category=category, description=description, confidence=confidence),
        evidence=[Evidence(**e) for e in ctx["evidence"]],
        contributing_factors=contrib,
        recommended_actions=ACTIONS[category],
        postmortem_markdown="",
    )
    report.postmortem_markdown = render_postmortem(report)
    return report


def render_postmortem(r: RcaReport) -> str:
    timeline = "\n".join(f"- {t.time}: {t.event}" for t in r.timeline)
    evidence = "\n".join(f"- **{e.source}**: {e.finding}\n  - `{e.query}`" for e in r.evidence)
    actions = "\n".join(f"- [ ] {a}" for a in r.recommended_actions)
    factors = "\n".join(f"- {c}" for c in r.contributing_factors) or "- none identified"
    return f"""# Postmortem: {r.alert} on {r.service}

**Report:** {r.id} | **Severity:** {r.severity} | **Analysis mode:** {r.mode}

## Summary
{r.summary}

## Impact
{r.impact.summary}
SLO: {r.impact.slo}.

## Timeline
{timeline}

## Root cause ({r.root_cause.confidence} confidence)
Category: `{r.root_cause.category}`

{r.root_cause.description}

## Contributing factors
{factors}

## Detection
The alert `{r.alert}` fired in Grafana alerting and was sent by webhook to the SRE agent, which started this investigation as an Argo Workflow automatically.

## Evidence
{evidence}

## Action items
{actions}

_This postmortem is blameless and was generated automatically. Review it before sharing._
"""
