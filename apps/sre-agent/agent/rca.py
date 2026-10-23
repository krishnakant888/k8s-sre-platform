"""Evidence gathering and the rule based analysis.

gather() runs a fixed playbook of read-only queries around the alert. analyze_rules() turns that
evidence into an RcaReport without a model. The LLM path (agent/llm.py) starts from the same evidence.
"""
import re
import time
from datetime import datetime, timezone

from agent import config, tools
from agent.models import Evidence, FaultDomain, HowToFix, Impact, Priority, RcaReport, RootCause, TimelineEntry

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
        f'1 - (sum(rate(http_request_duration_seconds_bucket{{service="{svc}", le="0.3", status!~"5.."}}[2m]))'
        f' / sum(rate(http_request_duration_seconds_count{{service="{svc}"}}[2m])))'
    )
    bad = tools.call_tool("query_promql", {"query": q_bad, "minutes": lookback_minutes})
    sig["bad_ratio_peak"] = _first(bad, max, "max", 0.0) or 0.0
    sig["bad_ratio_last"] = _first(bad, max, "last", 0.0) or 0.0
    add("mimir", q_bad, f"bad request ratio (5xx or >300ms) peaked at {sig['bad_ratio_peak']:.1%}, last value {(_first(bad, max, 'last', 0) or 0):.1%}")

    q_p99 = f'histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket{{service="{svc}"}}[2m])))'
    p99 = tools.call_tool("query_promql", {"query": q_p99, "minutes": lookback_minutes})
    sig["p99_peak"] = _first(p99, max, "max", 0.0) or 0.0
    sig["p99_baseline"] = _first(p99, min, "min", 0.0) or 0.0
    add("mimir", q_p99, f"p99 latency ranged from {sig['p99_baseline'] * 1000:.0f}ms to {sig['p99_peak'] * 1000:.0f}ms")

    q_5xx = f'sum(rate(http_requests_total{{service="{svc}", status=~"5.."}}[2m])) / sum(rate(http_requests_total{{service="{svc}"}}[2m]))'
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

    q_oom = f'max(kube_pod_container_status_last_terminated_reason{{namespace="{ns}", pod=~"{svc}-.*", reason="OOMKilled"}})'
    oom_m = tools.call_tool("query_promql", {"query": q_oom, "minutes": lookback_minutes})
    sig["oom_metric"] = (_first(oom_m, max, "max", 0.0) or 0.0) >= 1
    add("mimir", q_oom, "a container was last terminated as OOMKilled" if sig["oom_metric"] else "no OOMKilled termination recorded")

    # infrastructure level signals: node health, CPU throttling, memory trend
    q_node = 'min(kube_node_status_condition{condition="Ready", status="true"})'
    node = tools.call_tool("query_promql", {"query": q_node, "minutes": lookback_minutes})
    sig["node_not_ready"] = bool(node.get("series")) and (_first(node, min, "min", 1.0) < 1)
    q_press = 'max(kube_node_status_condition{condition=~"MemoryPressure|DiskPressure|PIDPressure", status="true"})'
    press = tools.call_tool("query_promql", {"query": q_press, "minutes": lookback_minutes})
    sig["node_pressure"] = (_first(press, max, "max", 0.0) or 0.0) >= 1
    add("mimir", f"{q_node} ; {q_press}", "a node was NotReady" if sig["node_not_ready"] else ("a node reported resource pressure" if sig["node_pressure"] else "all nodes Ready, no memory/disk/pid pressure"))

    sel = f'namespace="{ns}", pod=~"{svc}-.*"'
    q_thr = f'max(rate(container_cpu_cfs_throttled_periods_total{{{sel}}}[2m]) / rate(container_cpu_cfs_periods_total{{{sel}}}[2m]))'
    thr = tools.call_tool("query_promql", {"query": q_thr, "minutes": lookback_minutes})
    sig["cpu_throttle"] = _first(thr, max, "max", 0.0) or 0.0
    add("mimir", q_thr, f"CPU throttling peaked at {sig['cpu_throttle']:.0%} of periods")

    q_mem = f'sum(container_memory_working_set_bytes{{{sel}, container!=""}})'
    mem = tools.call_tool("query_promql", {"query": q_mem, "minutes": lookback_minutes})
    pts = [v for _, v in ((mem.get("series") or [{}])[0].get("points") or [])]
    sig["mem_growth"] = (pts[-1] / pts[0]) if len(pts) >= 4 and pts[0] > 0 else None
    add("mimir", q_mem, f"memory grew {sig['mem_growth']:.1f}x over the window" if sig["mem_growth"] else "no memory series for this workload")

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
    sig["log_breakdown"] = logs.get("breakdown", {})
    sig["log_fields"] = logs.get("fields_present", [])
    sig["log_total"] = logs.get("total", 0)
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
    sig["tool_errors"] = []
    if "error" in pods:
        sig["tool_errors"].append(f"pod lookup failed: {pods['error']}")
        add("kubernetes", f"pods {ns}/{svc}-*", f"lookup failed: {pods['error']}")
    sig["oom"] = sig["oom_metric"] or any((p.get("last_terminated") or {}).get("reason") == "OOMKilled" for p in pods.get("pods", []))
    sig["not_ready"] = [p["pod"] for p in pods.get("pods", []) if not p["ready"]]
    sig["pod_restarts_total"] = sum(p["restarts"] for p in pods.get("pods", []))
    sig["pod_names"] = sorted({p["pod"] for p in pods.get("pods", [])})
    sig["pod_nodes"] = sorted({p["node"] for p in pods.get("pods", []) if p.get("node")})
    sig["exit_codes"] = sorted({(p.get("last_terminated") or {}).get("exit_code") for p in pods.get("pods", []) if (p.get("last_terminated") or {}).get("exit_code") is not None})
    sig["mem_limit"] = next((p["memory_limit"] for p in pods.get("pods", []) if p.get("memory_limit")), None)
    sig["app"] = next((p["app_instance"] for p in pods.get("pods", []) if p.get("app_instance")), svc)
    if "error" not in pods:
        add("kubernetes", f"pods {ns}/{svc}-*", f"{len(pods.get('pods', []))} pods, restarts={sig['pod_restarts_total']}, oom_killed={sig['oom']}, not_ready={sig['not_ready']}")

    events = tools.call_tool("get_k8s_events", {"namespace": ns, "minutes": lookback_minutes})
    raw["events"] = events
    sig["kill_events"] = [e for e in events.get("events", []) if e["reason"] in ("Killing", "OOMKilling", "BackOff", "Unhealthy")]
    sig["probe_failures"] = len([e for e in events.get("events", []) if e["reason"] == "Unhealthy" and e["object"].startswith(svc)])
    sig["infra_events"] = [e for e in events.get("events", []) if e["reason"] in ("Evicted", "NodeNotReady", "FailedScheduling", "FailedMount", "NodeHasDiskPressure", "NodeHasMemoryPressure", "ImagePullBackOff", "ErrImagePull", "FailedCreatePodSandBox")]
    if sig["kill_events"]:
        add("kubernetes", f"events {ns}", "; ".join(f"{e['reason']} {e['object']}" for e in sig["kill_events"][:4]))

    # what the available signals can and cannot tell us, used for the observability gaps
    sig["has_traces"] = bool(sig["slow_traces"] or sig["error_traces"])
    sig["has_error_logs"] = bool(sig["top_log_messages"])
    sig["logs_have_trace_id"] = bool(sig["log_trace_ids"]) if sig["has_error_logs"] else None

    deploys = tools.call_tool("get_recent_deploys", {"application": sig["app"]})
    raw["deploys"] = deploys
    sig["source_files"] = deploys.get("source_files", [])
    recent = [d for d in deploys.get("history", []) if d.get("deployed_at") and parse_ts(d["deployed_at"]) > started - 3600]
    sig["recent_deploy"] = recent[0] if recent else None
    if "error" in deploys:
        add("argocd", f"history {svc}", f"no Argo CD application named {svc} could be read ({deploys['error'][:80]})")
    else:
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


def _bigger_memory(limit: str | None) -> str:
    """Suggests a new memory limit: double the current one, rounded to a whole Mi."""
    m = re.fullmatch(r"(\d+)(Mi|Gi)", limit or "")
    if not m:
        return "about double the current limit"
    mib = int(m.group(1)) * (1024 if m.group(2) == "Gi" else 1)
    return f"{mib * 2}Mi"


def remediation(category: str, ctx: dict) -> tuple[str, HowToFix]:
    """Turns the evidence into a plain-language account of what happened and concrete steps to fix it."""
    sig, svc, ns = ctx["signals"], ctx["service"], ctx["namespace"]
    app = sig.get("app") or svc
    files = ", ".join(f"`{f}`" for f in sig.get("source_files", [])) or f"the manifest of `{app}` in Git"
    pods = ", ".join(sig.get("pod_names", [])[:3]) or f"{svc}-*"
    started = iso(ctx["started"])
    p99_ms, base_ms = sig["p99_peak"] * 1000, sig["p99_baseline"] * 1000
    drill = sig.get("fault_injection")
    kill = sig["kill_events"][0] if sig.get("kill_events") else None
    deploy = sig.get("recent_deploy")
    slow = sig.get("slow_span")
    p99_q = f'histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket{{service="{svc}"}}[2m])))'
    err_q = f'sum(rate(http_requests_total{{service="{svc}", status=~"5.."}}[2m])) / sum(rate(http_requests_total{{service="{svc}"}}[2m]))'

    if category == "dependency_latency":
        what = (
            f"Starting around {started}, requests to `{svc}` became slow: p99 latency went from about {base_ms:.0f} ms to {p99_ms:.0f} ms "
            f"(the SLO allows 300 ms), so the error budget started burning. The traces show the time is spent in the downstream call "
            f"`{slow['name']}`, which took {slow['duration_ms']:.0f} ms inside each request. `{svc}` itself is healthy: no restarts and "
            f"{sig['replicas_min']:.0f} replicas available. The slowness comes from the dependency, not from this service."
            + (" A fault injection flag is set on the service, so this is most likely the latency drill." if drill else "")
        )
        immediate = []
        if drill:
            immediate.append("This looks like a drill. Clear the injected latency: `scripts/drill.sh reset`.")
        immediate += [
            f"Check the dependency behind `{slow['name']}`: its health, saturation and any recent change. Its latency is the problem, not `{svc}`.",
            "If the dependency was changed recently, roll that change back first.",
        ]
        permanent = [
            f"Add a client timeout (close to the 300 ms SLO threshold) and a circuit breaker around `{slow['name']}` in `{svc}`, so one slow dependency cannot use up the whole request.",
            "Cache or batch repeated lookups where the data allows it.",
            "Alert on the dependency's own latency (span metrics from Tempo) so it is caught before the user-facing SLO burns.",
        ]
        verify = [
            f"p99 back under 0.3 s: `{p99_q}`",
            "The OrdersApiHighLatencyP99 and error budget burn alerts return to normal in Grafana.",
        ]
    elif category == "dependency_errors":
        what = (
            f"Starting around {started}, up to {sig['error_share_peak']:.0%} of responses from `{svc}` were 5xx errors. The logs show "
            f"the downstream dependency failing ({_top_msgs(sig)}) and the failing requests have error spans for the same call, while `{svc}` "
            f"itself kept running with {sig['replicas_min']:.0f} replicas. The dependency is returning errors."
            + (" A fault injection flag is set on the service, so this is most likely the error drill." if drill else "")
        )
        immediate = []
        if drill:
            immediate.append("This looks like a drill. Clear the injected errors: `scripts/drill.sh reset`.")
        immediate += [
            "Check the failing dependency's own logs and availability, and recent changes to it.",
            f"See the failing requests: Loki `{{namespace=\"{ns}\", service=\"{svc}\"}} | json | level=\"error\"`, then open a `trace_id` in Tempo.",
        ]
        permanent = [
            f"Add retries with exponential backoff and a circuit breaker to the failing call in `{svc}`.",
            "Return a degraded but useful response (for example cached data) instead of a 500 when the dependency is down.",
            "Alert on the dependency's error rate directly.",
        ]
        verify = [f"5xx share back near zero: `{err_q}`", "The error budget burn alerts return to normal in Grafana."]
    elif category == "resource_exhaustion":
        limit = sig.get("mem_limit")
        what = (
            f"The container in `{pods}` ({ns}) ran out of memory and was OOMKilled: its memory use reached the limit"
            + (f" of {limit}" if limit else "")
            + f" and the kernel killed the process. There have been {sig['restarts']:.0f} container restarts in the last 45 minutes; "
            "each restart drops the in-flight work and, when the component is on the request path, fails requests until it is ready again."
            + (" A fault injection flag is set, so this may be the memory drill." if drill else "")
        )
        immediate = [
            f"Raise the memory limit in Git so it sticks (Argo CD applies it): set `resources.limits.memory` in {files}"
            + (f" from {limit} to {_bigger_memory(limit)}." if limit else f" to {_bigger_memory(limit)}."),
            f"First decide if it is a leak or a limit that is simply too low: look at `container_memory_working_set_bytes{{namespace=\"{ns}\", pod=~\"{svc}-.*\"}}`. "
            "Steady growth until the kill is a leak; a plateau at the limit means the limit is too low.",
        ]
        if drill:
            immediate.insert(0, "This may be the memory drill. The restarted pods are clean; no further action is needed for the drill itself.")
        permanent = [
            f"Set memory requests equal to limits for `{svc}` so the scheduler reserves what the process really needs.",
            f"Add an alert at 80% of the limit: `container_memory_working_set_bytes{{namespace=\"{ns}\", pod=~\"{svc}-.*\"}} / on(namespace,pod,container) kube_pod_container_resource_limits{{resource=\"memory\"}} > 0.8`.",
            "If memory grows without bound, profile the process and fix the leak.",
        ]
        verify = [
            "No new restarts for 30 minutes: `increase(kube_pod_container_status_restarts_total{namespace=\"" + ns + "\", pod=~\"" + svc + "-.*\"}[30m]) == 0`.",
            "Memory use stays below 80% of the new limit.",
        ]
    elif category == "pod_crash":
        what = (
            f"Pods of `{svc}` in `{ns}` were restarted or unavailable ({sig['restarts']:.0f} restarts in the last 45 minutes, "
            f"minimum {sig['replicas_min'] if sig['replicas_min'] is not None else 'unknown'} replicas available)"
            + (f"; Kubernetes recorded `{kill['reason']}` on `{kill['object']}`" if kill else "")
            + ". While a pod is down the service has less capacity and in-flight requests fail."
        )
        immediate = [
            f"Find out why it stopped: `kubectl -n {ns} logs {pods.split(', ')[0]} --previous` and `kubectl -n {ns} describe pod {pods.split(', ')[0]}`.",
            "If it started right after a deploy, roll the deploy back (revert the commit in Git).",
        ]
        if sig.get("not_ready"):
            immediate.append(f"These pods are not ready right now: {', '.join(sig['not_ready'])}. Check their readiness probe and events.")
        permanent = [
            f"Run at least 2 replicas of `{svc}` with a PodDisruptionBudget and spread them across nodes, so one pod dying does not take the service down.",
            "Review liveness/readiness probe timings, a probe that is too strict restarts healthy pods.",
            "Alert when restarts exceed 2 in 10 minutes (already in place as PodCrashLooping) and keep runbooks for the usual causes.",
        ]
        verify = [f"No restarts and all replicas ready: `kubectl -n {ns} get pods -l app.kubernetes.io/name={svc}`.", "The PodCrashLooping alert resolves in Grafana."]
    elif category == "deploy_regression":
        rev = deploy["revision"] if deploy else "the latest sync"
        what = (
            f"`{svc}` started failing or slowing down shortly after Argo CD synced revision `{rev}`"
            + (f" at {deploy['deployed_at']}" if deploy else "")
            + f". Nothing else changed in the evidence (p99 peaked at {p99_ms:.0f} ms, {sig['error_share_peak']:.0%} 5xx), so the deploy is the prime suspect."
        )
        immediate = [
            f"Roll back by reverting the commit in Git: `git revert {rev} && git push`. Argo CD syncs the revert automatically.",
            f"Or roll back directly: `argocd app rollback {app}` (the next Git sync will reapply Git, so also revert the commit).",
        ]
        permanent = [
            "Roll out changes progressively (Argo Rollouts canary) with automatic analysis on the error rate and latency, so a bad release is stopped at a small percentage of traffic.",
            "Add a test that covers the behavior that broke.",
        ]
        verify = [f"p99 and 5xx return to baseline: `{p99_q}`", "The error budget burn alerts return to normal."]
    else:
        what = (
            f"The alert fired at {started}, but the evidence does not point at one cause: p99 ranged from {base_ms:.0f} to {p99_ms:.0f} ms and 5xx peaked at "
            f"{sig['error_share_peak']:.0%}. Metrics, logs, traces, pod state and recent deploys show nothing decisive, or the problem had already recovered "
            "by the time the investigation ran."
        )
        immediate = [
            f"Open the dashboard for `{svc}` and compare the window with the same time yesterday.",
            f"Check whether traffic changed: `sum(rate(http_requests_total{{service=\"{svc}\"}}[2m]))`.",
        ]
        permanent = ["If this alert fires again without a clear cause, add the missing signal (a dependency metric, a trace attribute) so the next investigation can decide."]
        verify = ["The alert stays resolved for 30 minutes."]
    return what, HowToFix(immediate=immediate, permanent=permanent, verify=verify)


def _top_msgs(sig: dict) -> str:
    top = sig.get("top_log_messages", [])
    return "; ".join(f"'{m}' x{c}" for m, c in top[:2]) or "no error logs captured"


def fault_domain(category: str, sig: dict, ctx: dict) -> FaultDomain:
    """Decides whether the problem sits in the application or in the infrastructure underneath it, and says why."""
    svc = ctx["service"]
    infra: list[str] = []
    app: list[str] = []
    codes = sig.get("exit_codes", [])
    slow = sig.get("slow_span")

    if sig.get("node_not_ready"):
        infra.append("a node was NotReady during the window")
    if sig.get("node_pressure"):
        infra.append("a node reported memory, disk or PID pressure")
    if sig.get("infra_events"):
        infra.append("Kubernetes recorded infrastructure events: " + ", ".join(sorted({e["reason"] for e in sig["infra_events"]})))
    if sig.get("cpu_throttle", 0) > 0.25:
        infra.append(f"CPU throttling reached {sig['cpu_throttle']:.0%} of periods, so the CPU limit is too low for the load")
    if sig.get("oom"):
        growth = sig.get("mem_growth")
        if growth and growth > 1.5:
            app.append(f"memory grew {growth:.1f}x before the kill, which points at a leak in the process")
        else:
            infra.append("memory sat at the container limit instead of growing steadily, so the limit is too low for the workload")
    if any(c in (1, 2) for c in codes):
        app.append("the process exited by itself with code " + ", ".join(str(c) for c in codes if c in (1, 2)))
    if 143 in codes:
        infra.append("exit code 143 (SIGTERM): the platform stopped the container (rollout, drain or eviction)")
    if sig.get("probe_failures"):
        app.append(f"{sig['probe_failures']} failed readiness/liveness probes: the app was unhealthy or slow to start")
    if slow:
        app.append(f"traces show the time is spent in `{slow['name']}` ({slow['duration_ms']:.0f} ms) inside the request path")
    if sig.get("error_span"):
        app.append(f"error spans point at `{sig['error_span']}`")
    if sig.get("has_error_logs") and category in ("dependency_errors", "deploy_regression", "pod_crash", "unknown"):
        app.append(f"the application logged errors: {_top_msgs(sig)}")
    if sig.get("recent_deploy"):
        app.append(f"a deploy ({sig['recent_deploy']['revision']}) preceded the incident")
    if sig.get("fault_injection"):
        app.append("a fault injection flag is set inside the application")

    # metadata in the log lines: is the problem tied to one node, one replica or one release?
    bd, total = sig.get("log_breakdown", {}), max(sig.get("log_total", 0), 1)
    nodes, replicas, versions, deps = bd.get("node", {}), bd.get("pod", {}), bd.get("version", {}), bd.get("dependency", {})
    if len(nodes) == 1 and len(sig.get("pod_nodes", [])) >= 2:
        infra.append(f"every error/warning log line came from node `{next(iter(nodes))}` while `{svc}` runs on {len(sig['pod_nodes'])} nodes")
    if len(replicas) == 1 and len(sig.get("pod_names", [])) >= 2:
        infra.append(f"every error/warning log line came from one replica (`{next(iter(replicas))}`) of {len(sig['pod_names'])}, so it is tied to that instance or its node")
    if len(versions) >= 2 and max(versions.values()) / sum(versions.values()) >= 0.8:
        top_version = max(versions, key=versions.get)
        app.append(f"{max(versions.values())} of {sum(versions.values())} logged errors come from release `{top_version}` and the other releases are quiet")
    if deps:
        name, count = max(deps.items(), key=lambda kv: kv[1])
        app.append(f"{count} log lines name the dependency `{name}` as the failing or slow call")

    if category in ("dependency_latency", "dependency_errors", "deploy_regression"):
        layer = "application"
    elif category == "resource_exhaustion":
        layer = "application" if (sig.get("mem_growth") or 0) > 1.5 else "infrastructure"
    elif category == "pod_crash":
        layer = "infrastructure" if len(infra) > len(app) else ("application" if app else "unknown")
    else:
        layer = "application" if len(app) > len(infra) else ("infrastructure" if infra else "unknown")

    if category in ("dependency_latency", "dependency_errors"):
        what = (slow["name"] if slow else "inventory.lookup")
        component = f"downstream dependency `{what}` called by `{svc}`"
    elif category == "resource_exhaustion":
        component = f"memory use of the `{svc}` process" if layer == "application" else f"memory limit of `{svc}`" + (f" ({sig['mem_limit']})" if sig.get("mem_limit") else "")
    elif category == "pod_crash":
        component = f"the `{svc}` process or its probes" if layer == "application" else "pod lifecycle on the node (eviction, drain or node health)"
    elif category == "deploy_regression":
        component = f"release {sig['recent_deploy']['revision']} of `{svc}`" if sig.get("recent_deploy") else f"latest release of `{svc}`"
    else:
        component = "not determined"

    main, other = (app, infra) if layer == "application" else (infra, app)
    confidence = "high" if len(main) >= 2 and not other else ("medium" if main and len(main) > len(other) else "low")
    reasoning = list(main) or ["no signal in logs, metrics or traces separates the layers"]
    if other and layer != "unknown":
        reasoning.append("also seen on the other side: " + "; ".join(other))
    return FaultDomain(layer=layer, component=component, reasoning=reasoning, confidence=confidence)


def observability_gaps(category: str, sig: dict, ctx: dict) -> list[str]:
    """Signals that were missing or too weak for this investigation, each with what to add."""
    svc, ns = ctx["service"], ctx["namespace"]
    slow = sig.get("slow_span")
    gaps: list[str] = []
    if category in ("dependency_latency", "dependency_errors"):
        dep = slow["name"].split(".")[0] if slow else "the dependency"
        gaps.append(
            f"`{dep}` is only visible as a trace span. Add a client metric in `{svc}`: `dependency_request_duration_seconds{{dependency=\"{dep}\", outcome=...}}` "
            f"and an alert on its p99 and error rate, so the dependency is flagged before the user-facing SLO burns."
        )
    if category == "dependency_errors" and not sig.get("has_error_logs"):
        gaps.append(f"The failing calls left no error logs. Log every failed dependency call in `{svc}` with the dependency name, status and `trace_id`.")
    if sig.get("has_error_logs"):
        present = set(sig.get("log_fields", []))
        wanted = {
            "request_id": "a request id (propagate `X-Request-ID`, or generate one) on every line, so one request can be followed across services",
            "trace_id": "the OpenTelemetry `trace_id` and `span_id`, so a log line opens its trace",
            "version": "the release (`APP_VERSION` set from the image tag), to see if a problem is tied to one release",
            "node": "the node (`NODE_NAME` from the downward API `spec.nodeName`), to see if a problem is tied to one node",
            "pod": "the pod name (`POD_NAME` from the downward API)",
            "route": "the route and `duration_ms` of each request",
            "error_type": "the error type, message and stack trace on every failure",
        }
        missing = [k for k in wanted if k not in present]
        if missing:
            gaps.append(f"The log lines of `{svc}` lack " + ", ".join(f"`{k}`" for k in missing) + ". Add: " + "; ".join(wanted[k] for k in missing) + ".")
    if sig.get("error_share_peak", 0) > 0.05 and not sig.get("error_traces"):
        gaps.append(f"5xx responses have no error spans. Record exceptions on the span and set its status to error in `{svc}`.")
    if sig.get("p99_peak", 0) > 0.5 and not sig.get("has_traces"):
        gaps.append(f"No slow traces were found for `{svc}` during the latency. Check that it exports OTLP traces and that sampling keeps slow requests (tail sampling on duration).")
    if sig.get("oom") or 137 in sig.get("exit_codes", []):
        gaps.append(
            f"Nothing warned before the kill. Add a saturation alert: `container_memory_working_set_bytes{{namespace=\"{ns}\", pod=~\"{svc}-.*\"}} / on(namespace,pod,container) "
            f"kube_pod_container_resource_limits{{resource=\"memory\"}} > 0.8` for 5m."
        )
    if category == "pod_crash" and not sig.get("exit_codes"):
        gaps.append("Exit codes of the previous container were not available. Keep `terminationMessagePolicy: FallbackToLogsOnError` on the container so the last log lines are kept with the pod status.")
    if sig.get("cpu_throttle", 0) > 0.25:
        gaps.append(f"Add a CPU throttling alert for `{svc}`: `rate(container_cpu_cfs_throttled_periods_total[5m]) / rate(container_cpu_cfs_periods_total[5m]) > 0.25`.")
    for err in sig.get("tool_errors", []):
        gaps.append(f"The agent could not read what it needed ({err}). Grant its ServiceAccount read access to that resource.")
    if not sig.get("source_files") and sig.get("app") == svc and not sig.get("recent_deploy"):
        gaps.append(f"No Argo CD application named `{svc}` could be matched, so deploys cannot be correlated. Label the workload with `app.kubernetes.io/instance` set to its Argo CD application name.")
    if category == "unknown":
        gaps.insert(0, "The alert could not be explained. The missing signal is the first thing to add: a metric or span for the step where time is spent, and an error log with context for each failure.")
    return gaps


def decide_priority(alert: dict, sig: dict, category: str, domain: FaultDomain, svc: str) -> Priority:
    """P1 to P4 from the alert severity and the measured impact."""
    severity = alert.get("labels", {}).get("severity", "ticket")
    level = 2 if severity == "page" else 3
    why = [f"alert severity is '{severity}'"]
    user_facing = svc in config.USER_FACING_SERVICES
    bad_peak, bad_now = sig.get("bad_ratio_peak", 0.0), sig.get("bad_ratio_last", 0.0)
    ongoing = bad_now > 0.005 or (sig.get("replicas_min") is not None and sig["replicas_min"] < 1 and sig.get("not_ready"))
    outage = (sig.get("replicas_min") is not None and sig["replicas_min"] < 1) or sig.get("error_share_peak", 0) >= 0.5 or bad_peak >= 0.5
    burn = bad_peak / 0.005 if bad_peak else 0.0

    if user_facing:
        why.append(f"`{svc}` serves users")
        if outage:
            level = 1
            why.append("it is effectively down: " + ("no replicas available" if (sig.get("replicas_min") or 1) < 1 else f"{max(bad_peak, sig.get('error_share_peak', 0)):.0%} of requests failed"))
        elif bad_peak >= 0.1 or sig.get("p99_peak", 0) > 1.0:
            level = min(level, 2)
            why.append(f"{bad_peak:.0%} of requests were bad and p99 reached {sig.get('p99_peak', 0) * 1000:.0f} ms")
        if burn >= 14.4:
            level = min(level, 2)
            why.append(f"the error budget burns {burn:.0f}x faster than allowed")
        elif burn >= 6:
            level = min(level, 3)
            why.append(f"the error budget burns {burn:.0f}x faster than allowed")
    else:
        level = max(level, 3)
        why.append(f"`{svc}` is a platform component with no direct user traffic" + (f", but {bad_peak:.0%} of user requests were affected" if bad_peak >= 0.05 else ""))
        if bad_peak >= 0.05:
            level = 2

    if not ongoing and not outage:
        level = min(level + 1, 4)
        why.append("the signals have recovered, so the urgency is lower")
    elif ongoing:
        why.append("it is still ongoing")

    respond = {1: "now: page the on-call and open an incident", 2: "within 1 hour", 3: "next business day", 4: "backlog, no action needed unless it repeats"}[level]
    return Priority(level=f"P{level}", respond_within=respond, ongoing=bool(ongoing), rationale=why)


def analyze_rules(alert: dict, ctx: dict, now: float | None = None) -> RcaReport:
    now = now or time.time()
    sig = ctx["signals"]
    svc = ctx["service"]
    category, description, confidence, contrib = classify(sig)
    if sig.get("tool_errors"):
        contrib.append("some lookups failed, so this analysis may be incomplete: " + "; ".join(sig["tool_errors"]))
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
    what, fix = remediation(category, ctx)
    domain = fault_domain(category, sig, ctx)
    priority = decide_priority(alert, sig, category, domain, svc)
    gaps = observability_gaps(category, sig, ctx)
    report = RcaReport(
        id=f"rca-{svc}-{datetime.fromtimestamp(started, timezone.utc).strftime('%Y%m%d-%H%M%S')}",
        alert=alertname,
        service=svc,
        severity=severity,
        started_at=iso(started),
        generated_at=iso(now),
        mode="rule_based",
        summary=f"{alertname} on {svc}: {description}",
        what_happened=what,
        priority=priority,
        fault_domain=domain,
        impact=impact,
        timeline=timeline,
        root_cause=RootCause(category=category, description=description, confidence=confidence),
        evidence=[Evidence(**e) for e in ctx["evidence"]],
        contributing_factors=contrib,
        how_to_fix=fix,
        observability_gaps=gaps,
        postmortem_markdown="",
    )
    report.postmortem_markdown = render_postmortem(report)
    return report


def _bullets(items: list[str], checkbox: bool = False) -> str:
    return "\n".join(f"- {'[ ] ' if checkbox else ''}{i}" for i in items) or "- none"


def render_postmortem(r: RcaReport) -> str:
    timeline = "\n".join(f"- {t.time}: {t.event}" for t in r.timeline)
    evidence = "\n".join(f"- **{e.source}**: {e.finding}\n  - `{e.query}`" for e in r.evidence)
    factors = "\n".join(f"- {c}" for c in r.contributing_factors) or "- none identified"
    return f"""# Postmortem: {r.alert} on {r.service}

**Report:** {r.id} | **Priority:** {r.priority.level} | **Alert severity:** {r.severity} | **Analysis mode:** {r.mode}

## Priority and where the problem is
**{r.priority.level}**: respond {r.priority.respond_within}. {"Still ongoing." if r.priority.ongoing else "No longer ongoing."}
{_bullets(r.priority.rationale)}

**{r.fault_domain.layer.capitalize()} level** ({r.fault_domain.confidence} confidence): {r.fault_domain.component}
{_bullets(r.fault_domain.reasoning)}

## What happened
{r.what_happened}

## Root cause ({r.root_cause.confidence} confidence)
Category: `{r.root_cause.category}`

{r.root_cause.description}

## Impact
{r.impact.summary}
SLO: {r.impact.slo}.

## How to fix it now
{_bullets(r.how_to_fix.immediate, checkbox=True)}

## How to prevent it
{_bullets(r.how_to_fix.permanent, checkbox=True)}

## How to confirm it is fixed
{_bullets(r.how_to_fix.verify)}

## Timeline
{timeline}

## Contributing factors
{factors}

## Detection
The alert `{r.alert}` fired in Grafana alerting and was sent by webhook to the SRE agent, which started this investigation as an Argo Workflow automatically.

## Observability gaps (what to add so the next one is faster)
{_bullets(r.observability_gaps)}

## Evidence
{evidence}

_This postmortem is blameless and was generated automatically. Review it before acting on it._
"""
