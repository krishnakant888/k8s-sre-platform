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
