import json
import logging

import pytest
from fastapi.testclient import TestClient

import app.main as main


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    monkeypatch.setattr(main, "CHAOS_TOKEN", "secret")
    main.chaos.update(latency_ms=0, error_rate=0.0, until=0.0)
    main.orders.clear()


client = TestClient(main.app)


def test_health():
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").status_code == 200


def test_create_and_get_order(monkeypatch):
    async def plenty(_item):
        return 1000

    monkeypatch.setattr(main, "lookup_stock", plenty)
    r = client.post("/orders", json={"item": "widget", "qty": 2})
    assert r.status_code == 201
    oid = r.json()["id"]
    assert client.get(f"/orders/{oid}").json()["item"] == "widget"


def test_validation_rejects_bad_qty():
    assert client.post("/orders", json={"item": "widget", "qty": 0}).status_code == 422


def test_unknown_order_is_404():
    assert client.get("/orders/99999").status_code == 404


def test_metrics_expose_request_counter():
    client.get("/orders")
    body = client.get("/metrics").text
    assert 'http_requests_total{method="GET",route="/orders",service="orders-api",status="200"}' in body
    assert "http_request_duration_seconds_bucket" in body


def test_chaos_requires_token():
    assert client.post("/chaos/errors").status_code == 403
    assert client.post("/chaos/errors", headers={"x-chaos-token": "nope"}).status_code == 403


def test_error_injection_returns_500_and_resets():
    h = {"x-chaos-token": "secret"}
    assert client.post("/chaos/errors?rate=1&seconds=60", headers=h).status_code == 200
    assert client.get("/orders").status_code == 500
    client.post("/chaos/reset", headers=h)
    assert client.get("/orders").status_code == 200


def test_logs_are_json_with_fields_and_trace_ids():
    from app.logging_setup import JsonFormatter

    record = logging.LogRecord("orders-api", logging.INFO, __file__, 1, "hello", (), None)
    record.order_id = 7
    with main.tracer.start_as_current_span("t") as span:
        entry = json.loads(JsonFormatter("orders-api").format(record))
        assert entry["trace_id"] == format(span.get_span_context().trace_id, "032x")
    assert entry["service"] == "orders-api" and entry["order_id"] == 7 and entry["level"] == "info"


def capture_logs():
    """Collects every record of the service logger as the parsed json line that would be written to stdout."""
    from app.logging_setup import JsonFormatter

    lines = []

    class Collect(logging.Handler):
        def emit(self, record):
            lines.append(json.loads(JsonFormatter("orders-api").format(record)))

    handler = Collect()
    main.log.addHandler(handler)
    return lines, handler


def test_request_log_has_the_metadata_to_pinpoint_an_issue(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "sha-abc1234")
    monkeypatch.setenv("POD_NAME", "orders-api-xyz")
    monkeypatch.setenv("NODE_NAME", "platform-worker2")
    monkeypatch.setenv("POD_NAMESPACE", "demo")
    lines, handler = capture_logs()
    try:
        r = client.get("/orders", headers={"user-agent": "curl/8", "x-forwarded-for": "10.1.2.3, 10.9.9.9", "x-request-id": "req-42"})
    finally:
        main.log.removeHandler(handler)
    assert r.headers["x-request-id"] == "req-42"
    entry = next(l for l in lines if l["msg"] == "request handled")
    for field in ("ts", "level", "service", "version", "namespace", "pod", "node", "request_id", "method", "route", "path", "status", "duration_ms", "dependency_ms", "client_ip", "user_agent", "fault_injected"):
        assert field in entry, field
    assert entry["request_id"] == "req-42" and entry["client_ip"] == "10.1.2.3" and entry["version"] == "sha-abc1234" and entry["node"] == "platform-worker2"


def test_failed_dependency_call_names_the_dependency_and_shares_the_request_id():
    lines, handler = capture_logs()
    try:
        client.post("/chaos/errors?rate=1&seconds=60", headers={"x-chaos-token": "secret"})
        r = client.get("/orders")
    finally:
        main.log.removeHandler(handler)
        client.post("/chaos/reset", headers={"x-chaos-token": "secret"})
    failed = next(l for l in lines if l["msg"] == "inventory lookup failed")
    assert (failed["dependency"], failed["operation"], failed["outcome"], failed["error_type"]) == ("inventory", "lookup", "error", "DependencyUnavailable")
    assert failed["request_id"] == r.headers["x-request-id"]
    request_line = next(l for l in lines if l["msg"] == "request handled" and l.get("status") == 500)
    assert request_line["request_id"] == failed["request_id"] and request_line["fault_injected"] is True


def test_slow_dependency_shows_up_as_dependency_ms():
    lines, handler = capture_logs()
    try:
        client.post("/chaos/latency?ms=400&seconds=60", headers={"x-chaos-token": "secret"})
        client.get("/orders")
    finally:
        main.log.removeHandler(handler)
        client.post("/chaos/reset", headers={"x-chaos-token": "secret"})
    request_line = next(l for l in lines if l["msg"] == "request handled" and l["route"] == "/orders")
    assert request_line["dependency_ms"] >= 400 and request_line["duration_ms"] >= request_line["dependency_ms"]
    assert any(l["msg"] == "dependency call is slow" and l["dependency"] == "inventory" for l in lines)


def test_unhandled_exception_is_logged_with_type_stack_and_request_id(monkeypatch):
    async def boom(_item):
        raise RuntimeError("kaput")

    monkeypatch.setattr(main, "lookup_stock", boom)
    lines, handler = capture_logs()
    try:
        r = TestClient(main.app, raise_server_exceptions=False).get("/orders")
    finally:
        main.log.removeHandler(handler)
    assert r.status_code == 500 and r.json()["request_id"]
    err = next(l for l in lines if l["msg"] == "unhandled exception")
    assert err["error_type"] == "RuntimeError" and "kaput" in err["error_message"] and "Traceback" in err["stack"]
    assert err["request_id"] == r.json()["request_id"]
