import time

import httpx
import pytest

from agent import rca, tools
from agent.models import RcaReport

NOW = time.time()
ALERT = {
    "labels": {"alertname": "OrdersApiErrorBudgetFastBurn", "severity": "page", "service": "orders-api"},
    "startsAt": rca.iso(NOW - 300),
    "fingerprint": "abc",
}


def series(value, key="max"):
    return {"series": [{"labels": {}, "min": value, "max": value, "avg": value, "last": value}]}


def fake_tools(monkeypatch, *, bad=0.0, p99=0.05, err=0.0, replicas=2, restarts=0, logs=None, slow_span=None, oom=False, deploy=None, chaos=0, pods_error=None, node_down=False, throttle=0.0, mem_growth=1.0, exit_code=137, replicas_min_override=None, severity="page"):
    """Replace the observability calls with canned answers for one scenario."""

    def call(name, args):
        q = args.get("query", "")
        if name == "query_promql":
            if "chaos_active" in q:
                return series(chaos)
            if "kube_deployment_status_replicas_available" in q:
                return series(replicas)
            if "kube_node_status_condition" in q:
                return series(0 if node_down and "Ready" in q and "Pressure" not in q else (1 if node_down else (1 if "Ready" in q and "Pressure" not in q else 0)))
            if "cfs_throttled" in q:
                return series(throttle)
            if "container_memory_working_set_bytes" in q:
                base = 100.0
                return {"series": [{"labels": {}, "min": base, "max": base * mem_growth, "avg": base, "last": base * mem_growth,
                                    "points": [(i, base + (base * mem_growth - base) * i / 11) for i in range(12)]}]}
            if "last_terminated_reason" in q:
                return series(1 if oom else 0)
            if "restarts_total" in q:
                return series(restarts)
            if "histogram_quantile" in q:
                return {"series": [{"labels": {}, "min": 0.03, "max": p99, "avg": p99, "last": p99}]}
            if 'status=~"5.."}[2m])) /' in q:
                return series(err)
            return series(bad)
        if name == "query_logql":
            lines = logs or []
            return {"top_messages": [(m, 10) for m in lines], "lines": [{"ts": 1, "pod": "p", "line": "{}", "trace_id": "t1"}] if lines else [], "total": len(lines)}
        if name == "search_traces":
            return {"traces": [{"trace_id": "t1", "root": "GET /orders", "duration_ms": 900}]} if slow_span or logs else {"traces": []}
        if name == "get_trace":
            spans = [slow_span] if slow_span else []
            return {"slowest": spans, "errors": []}
        if name == "get_pod_status":
            if pods_error:
                return {"error": pods_error}
            last = {"reason": "OOMKilled" if oom else "Error", "exit_code": exit_code, "at": "x"} if (oom or exit_code != 137) else None
            return {"pods": [{"pod": "orders-api-1", "ready": True, "restarts": 1 if oom else 0, "last_terminated": last, "memory_limit": "768Mi", "app_instance": "orders-api"}]}
        if name == "get_k8s_events":
            return {"events": [{"ts": rca.iso(NOW - 200), "type": "Warning", "reason": "Killing", "object": "orders-api-1", "message": "m"}] if oom else []}
        if name == "get_recent_deploys":
            return {"history": [deploy] if deploy else [], "source_files": ["gitops/workloads/orders-api"]}
        return {}

    monkeypatch.setattr(tools, "call_tool", call)


def run(monkeypatch, severity="page", **kw):
    fake_tools(monkeypatch, **kw)
    alert = {**ALERT, "labels": {**ALERT["labels"], "severity": severity}}
    ctx = rca.gather(alert, now=NOW)
    return rca.analyze_rules(alert, ctx, now=NOW)


def test_slow_dependency_is_latency_root_cause(monkeypatch):
    r = run(monkeypatch, bad=0.9, p99=1.2, slow_span={"name": "inventory.lookup", "duration_ms": 1000.0}, chaos=1)
    assert r.root_cause.category == "dependency_latency"
    assert r.root_cause.confidence == "high"
    assert any("fault injection" in c for c in r.contributing_factors)
    assert "inventory.lookup" in r.root_cause.description


def test_dependency_errors(monkeypatch):
    r = run(monkeypatch, bad=0.5, err=0.5, logs=["inventory backend unavailable"], slow_span={"name": "inventory.lookup", "duration_ms": 20.0})
    assert r.root_cause.category == "dependency_errors"


def test_oom_wins_over_other_signals(monkeypatch):
    r = run(monkeypatch, bad=0.3, err=0.3, oom=True, restarts=2)
    assert r.root_cause.category == "resource_exhaustion"
    assert any(t.event.startswith("Killing") for t in r.timeline)


def test_pod_crash_without_oom(monkeypatch):
    r = run(monkeypatch, restarts=3, replicas=0)
    assert r.root_cause.category == "pod_crash"


def test_deploy_regression(monkeypatch):
    dep = {"deployed_at": rca.iso(NOW - 600), "revision": "abc1234", "id": 3}
    r = run(monkeypatch, bad=0.4, err=0.4, deploy=dep)
    assert r.root_cause.category == "deploy_regression"


def test_nothing_obvious_is_unknown_with_low_confidence(monkeypatch):
    r = run(monkeypatch)
    assert r.root_cause.category == "unknown" and r.root_cause.confidence == "low"


def test_report_is_valid_and_has_postmortem(monkeypatch):
    r = run(monkeypatch, bad=0.9, p99=1.2, slow_span={"name": "inventory.lookup", "duration_ms": 1000.0})
    again = RcaReport.model_validate(r.model_dump())
    assert again.mode == "rule_based"
    for heading in ("## What happened", "## Priority and where the problem is", "## Root cause", "## Impact", "## How to fix it now", "## Observability gaps", "## Timeline"):
        assert heading in again.postmortem_markdown
    assert again.evidence and all(e.query for e in again.evidence)


def test_service_derived_from_pod_label():
    assert rca.service_of({"labels": {"pod": "orders-api-844dbcf56d-j99pb"}}) == "orders-api"
    assert rca.service_of({"labels": {"deployment": "x"}}) == "x"


def test_redact_hides_secrets():
    assert "hunter2" not in tools.redact('{"password": "hunter2", "msg": "x"}')
    assert "abc123" not in tools.redact("api_key=abc123")


def test_promql_summary_and_clamping(monkeypatch):
    seen = {}

    def handler(request: httpx.Request):
        seen.update(request.url.params)
        return httpx.Response(200, json={"data": {"result": [{"metric": {"a": "b"}, "values": [[1, "1"], [2, "3"], [3, "NaN"]]}]}})

    monkeypatch.setattr(tools, "_client", httpx.Client(transport=httpx.MockTransport(handler)))
    out = tools.query_promql("up", minutes=100000)
    s = out["series"][0]
    assert (s["min"], s["max"], s["avg"], s["last"]) == (1.0, 3.0, 2.0, 3.0)
    assert float(seen["end"]) - float(seen["start"]) == pytest.approx(120 * 60, abs=2)


def test_unknown_tool_is_an_error_value():
    assert "error" in tools.call_tool("rm_rf", {})


def test_latency_report_explains_what_happened_and_how_to_fix(monkeypatch):
    r = run(monkeypatch, bad=0.9, p99=1.2, slow_span={"name": "inventory.lookup", "duration_ms": 1000.0}, chaos=1)
    assert "inventory.lookup" in r.what_happened and "1200 ms" in r.what_happened
    assert any("scripts/drill.sh reset" in step for step in r.how_to_fix.immediate)
    assert any("timeout" in step and "circuit breaker" in step for step in r.how_to_fix.permanent)
    assert any("histogram_quantile" in step for step in r.how_to_fix.verify)
    assert "## How to fix it now" in r.postmortem_markdown and "## What happened" in r.postmortem_markdown


def test_oom_fix_names_the_file_and_the_new_limit(monkeypatch):
    r = run(monkeypatch, bad=0.3, oom=True, restarts=2)
    assert r.root_cause.category == "resource_exhaustion"
    fix = " ".join(r.how_to_fix.immediate)
    assert "768Mi to 1536Mi" in fix and "gitops/workloads/orders-api" in fix
    assert "leak" in fix


def test_deploy_regression_fix_has_the_revert_command(monkeypatch):
    dep = {"deployed_at": rca.iso(NOW - 600), "revision": "abc1234", "id": 3}
    r = run(monkeypatch, bad=0.4, err=0.4, deploy=dep)
    assert any("git revert abc1234" in step for step in r.how_to_fix.immediate)


def test_failed_lookup_is_visible_in_the_report(monkeypatch):
    r = run(monkeypatch, bad=0.9, p99=1.2, slow_span={"name": "inventory.lookup", "duration_ms": 1000.0}, pods_error="Forbidden")
    assert any("lookup failed: Forbidden" in e.finding for e in r.evidence)
    assert any("may be incomplete" in c for c in r.contributing_factors)


def test_k8s_get_sends_the_token_and_builds_its_own_client(monkeypatch, tmp_path):
    """Regression: httpx rejects verify= on a request, which silently broke every kubernetes lookup."""
    (tmp_path / "token").write_text("tok123")
    monkeypatch.setattr(tools.config, "SA_DIR", str(tmp_path))
    seen = {}

    def handler(request: httpx.Request):
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"items": [{"metadata": {"name": "orders-api-abc", "labels": {"app.kubernetes.io/instance": "orders-api"}},
                                                    "spec": {"containers": [{"name": "c", "resources": {"limits": {"memory": "192Mi"}}}]},
                                                    "status": {"phase": "Running", "containerStatuses": [{"name": "c", "ready": True, "restartCount": 2, "state": {}, "lastState": {"terminated": {"reason": "OOMKilled"}}}]}}]})

    monkeypatch.setattr(tools, "_k8s_transport", httpx.MockTransport(handler))
    out = tools.get_pod_status("demo", "orders-api")
    assert seen["auth"] == "Bearer tok123"
    pod = out["pods"][0]
    assert pod["memory_limit"] == "192Mi" and pod["app_instance"] == "orders-api" and pod["last_terminated"]["reason"] == "OOMKilled"


def test_dependency_problem_is_application_level(monkeypatch):
    r = run(monkeypatch, bad=0.9, p99=1.2, slow_span={"name": "inventory.lookup", "duration_ms": 1000.0}, chaos=1)
    assert r.fault_domain.layer == "application"
    assert "inventory.lookup" in r.fault_domain.component
    assert any("traces show the time is spent" in x for x in r.fault_domain.reasoning)
    assert any("dependency_request_duration_seconds" in g for g in r.observability_gaps)


def test_oom_at_the_limit_is_infrastructure_but_a_leak_is_application(monkeypatch):
    plateau = run(monkeypatch, oom=True, restarts=2, mem_growth=1.05)
    assert plateau.fault_domain.layer == "infrastructure" and "memory limit" in plateau.fault_domain.component
    leak = run(monkeypatch, oom=True, restarts=2, mem_growth=3.0)
    assert leak.fault_domain.layer == "application" and "leak" in " ".join(leak.fault_domain.reasoning)
    assert any("kube_pod_container_resource_limits" in g for g in plateau.observability_gaps)


def test_node_trouble_makes_a_crash_infrastructure_level(monkeypatch):
    r = run(monkeypatch, restarts=3, replicas=1, node_down=True, exit_code=143)
    assert r.root_cause.category == "pod_crash"
    assert r.fault_domain.layer == "infrastructure"
    assert any("NotReady" in x for x in r.fault_domain.reasoning)


def test_priority_p1_for_a_user_facing_outage(monkeypatch):
    r = run(monkeypatch, bad=0.95, err=0.95, p99=2.0, replicas=0, restarts=4)
    assert r.priority.level == "P1" and r.priority.ongoing
    assert any("serves users" in x for x in r.priority.rationale)


def test_priority_p2_for_a_page_with_real_impact(monkeypatch):
    r = run(monkeypatch, bad=0.3, p99=1.2, slow_span={"name": "inventory.lookup", "duration_ms": 1000.0})
    assert r.priority.level == "P2"


def test_priority_falls_when_it_has_recovered_and_a_ticket_alert(monkeypatch):
    r = run(monkeypatch, severity="ticket")  # nothing bad in the window
    assert r.priority.level in ("P3", "P4") and not r.priority.ongoing


def test_platform_component_without_user_impact_is_capped_at_p3(monkeypatch):
    fake_tools(monkeypatch, oom=True, restarts=2)
    alert = {"labels": {"alertname": "PodOOMKilled", "severity": "page", "service": "argocd-application-controller", "namespace": "argocd"},
             "startsAt": rca.iso(NOW - 300), "fingerprint": "x"}
    ctx = rca.gather(alert, now=NOW)
    r = rca.analyze_rules(alert, ctx, now=NOW)
    assert r.priority.level in ("P3", "P4")
    assert any("platform component" in x for x in r.priority.rationale)
