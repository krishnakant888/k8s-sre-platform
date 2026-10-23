import json
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


def fake_tools(monkeypatch, *, bad=0.0, p99=0.05, err=0.0, replicas=2, restarts=0, logs=None, slow_span=None, oom=False, deploy=None, chaos=0):
    """Replace the observability calls with canned answers for one scenario."""

    def call(name, args):
        q = args.get("query", "")
        if name == "query_promql":
            if "chaos_active" in q:
                return series(chaos)
            if "kube_deployment_status_replicas_available" in q:
                return series(replicas)
            if "restarts_total" in q:
                return series(restarts)
            if "histogram_quantile" in q:
                return {"series": [{"labels": {}, "min": 0.03, "max": p99, "avg": p99, "last": p99}]}
            if 'status=~"5.."}[1m])) /' in q:
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
            last = {"reason": "OOMKilled", "exit_code": 137, "at": "x"} if oom else None
            return {"pods": [{"pod": "orders-api-1", "ready": True, "restarts": 1 if oom else 0, "last_terminated": last}]}
        if name == "get_k8s_events":
            return {"events": [{"ts": rca.iso(NOW - 200), "type": "Warning", "reason": "Killing", "object": "orders-api-1", "message": "m"}] if oom else []}
        if name == "get_recent_deploys":
            return {"history": [deploy] if deploy else []}
        return {}

    monkeypatch.setattr(tools, "call_tool", call)


def run(monkeypatch, **kw):
    fake_tools(monkeypatch, **kw)
    ctx = rca.gather(ALERT, now=NOW)
    return rca.analyze_rules(ALERT, ctx, now=NOW)


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
    for heading in ("## Summary", "## Impact", "## Timeline", "## Root cause", "## Action items"):
        assert heading in again.postmortem_markdown
    assert again.evidence and all(e.query for e in again.evidence)


def test_store_report_writes_files_to_the_reports_volume(monkeypatch, tmp_path):
    from agent import activities, config

    monkeypatch.setattr(config, "REPORTS_DIR", str(tmp_path))
    r = run(monkeypatch, bad=0.9, p99=1.2)
    assert activities.store_report(r.model_dump()) == r.id
    folder = tmp_path / r.id
    assert json.loads((folder / "report.json").read_text())["id"] == r.id
    assert "## Summary" in (folder / "postmortem.md").read_text()
    assert activities.store_report(r.model_dump()) == r.id  # a retry overwrites, it does not fail
    assert sorted(p.name for p in folder.iterdir()) == ["postmortem.md", "report.json"]  # no temp files left behind


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
