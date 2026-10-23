"""orders-api: small service that emits metrics, json logs and traces.

It has fault injection endpoints (latency, errors) so the SRE agent has something to investigate.
"""
import asyncio
import hmac
import itertools
import logging
import os
import random
import threading
import time
import uuid

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from opentelemetry import trace
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field

from app.logging_setup import request_ctx, setup_logging
from app.tracing import setup_tracing

SERVICE = "orders-api"
CHAOS_TOKEN = os.getenv("CHAOS_TOKEN", "")

setup_logging(SERVICE)
log = logging.getLogger(SERVICE)
tracer = trace.get_tracer(SERVICE)

REQUESTS = Counter("http_requests_total", "HTTP requests", ["service", "method", "route", "status"])
LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["service", "route", "status"],
    buckets=(0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1, 2, 5),
)
ORDERS = Counter("orders_created_total", "Orders created", ["service"])
CHAOS_ACTIVE = Gauge("chaos_active", "1 while a fault is being injected", ["service", "kind"])

# fault injection state, process local on purpose
chaos = {"latency_ms": 0, "error_rate": 0.0, "until": 0.0}
orders: dict[int, dict] = {}
order_ids = itertools.count(1)

app = FastAPI(title=SERVICE)
setup_tracing(app, SERVICE)


class OrderIn(BaseModel):
    item: str = Field(min_length=1, max_length=64)
    qty: int = Field(ge=1, le=100)


def chaos_live() -> bool:
    if chaos["until"] and time.time() > chaos["until"]:
        chaos.update(latency_ms=0, error_rate=0.0, until=0.0)
    CHAOS_ACTIVE.labels(SERVICE, "latency").set(1 if chaos["latency_ms"] else 0)
    CHAOS_ACTIVE.labels(SERVICE, "errors").set(1 if chaos["error_rate"] else 0)
    return bool(chaos["latency_ms"] or chaos["error_rate"])


@app.middleware("http")
async def observe(request: Request, call_next):
    start = time.perf_counter()
    status = 500
    # one id per request, taken from the caller when present, returned in the response and on every log line
    ctx = {"request_id": request.headers.get("x-request-id") or uuid.uuid4().hex[:16], "dependency_ms": 0.0}
    token = request_ctx.set(ctx)
    try:
        try:
            response = await call_next(request)
        except Exception:
            # logged here, not in an exception handler, because only here the request id is still in scope
            route_now = request.scope.get("route")
            log.exception("unhandled exception", extra={"route": route_now.path if route_now else "unmatched"})
            response = JSONResponse(status_code=500, content={"detail": "internal error", "request_id": ctx["request_id"]})
        status = response.status_code
        response.headers["x-request-id"] = ctx["request_id"]
        return response
    finally:
        elapsed = time.perf_counter() - start
        route = request.scope.get("route")
        path = route.path if route else "unmatched"
        if path not in ("/metrics", "/healthz", "/readyz"):
            REQUESTS.labels(SERVICE, request.method, path, str(status)).inc()
            LATENCY.labels(SERVICE, path, str(status)).observe(elapsed)
            level = logging.ERROR if status >= 500 else logging.INFO
            forwarded = request.headers.get("x-forwarded-for", "")
            log.log(
                level,
                "request handled",
                extra={
                    "method": request.method,
                    "route": path,
                    "path": request.url.path,
                    "status": status,
                    "duration_ms": round(elapsed * 1000, 1),
                    # how much of the request was spent waiting on the dependency, tells slow service from slow dependency
                    "dependency_ms": round(ctx["dependency_ms"], 1),
                    "client_ip": (forwarded.split(",")[0].strip() or (request.client.host if request.client else None)),
                    "user_agent": request.headers.get("user-agent", "")[:80],
                    "fault_injected": chaos_live(),
                },
            )
        request_ctx.reset(token)


async def lookup_stock(item: str) -> int:
    """Pretend downstream call. This is where injected latency and errors show up in traces."""
    started = time.perf_counter()
    with tracer.start_as_current_span("inventory.lookup") as span:
        span.set_attribute("inventory.item", item)
        chaos_live()
        delay = random.uniform(0.005, 0.02) + chaos["latency_ms"] / 1000
        await asyncio.sleep(delay)
        elapsed_ms = (time.perf_counter() - started) * 1000
        ctx = request_ctx.get()
        if ctx is not None:
            ctx["dependency_ms"] += elapsed_ms
        if chaos["error_rate"] and random.random() < chaos["error_rate"]:
            span.set_attribute("error", True)
            span.set_status(trace.StatusCode.ERROR, "inventory backend unavailable")
            log.error(
                "inventory lookup failed",
                extra={"dependency": "inventory", "operation": "lookup", "outcome": "error", "duration_ms": round(elapsed_ms, 1), "item": item,
                       "reason": "backend unavailable", "error_type": "DependencyUnavailable"},
            )
            raise HTTPException(status_code=500, detail="inventory backend unavailable")
        if elapsed_ms > 300:  # slower than the SLO allows for the whole request
            log.warning("dependency call is slow", extra={"dependency": "inventory", "operation": "lookup", "outcome": "slow", "duration_ms": round(elapsed_ms, 1), "item": item})
        return random.randint(1, 500)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    return {"status": "ready"}


@app.get("/metrics")
def metrics():
    chaos_live()
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/orders", status_code=201)
async def create_order(body: OrderIn):
    stock = await lookup_stock(body.item)
    if stock < body.qty:
        raise HTTPException(status_code=409, detail="not enough stock")
    order_id = next(order_ids)
    orders[order_id] = {"id": order_id, "item": body.item, "qty": body.qty}
    ORDERS.labels(SERVICE).inc()
    log.info("order created", extra={"order_id": order_id, "item": body.item, "qty": body.qty})
    return orders[order_id]


@app.get("/orders")
async def list_orders():
    await lookup_stock("list")
    return list(orders.values())[-50:]


@app.get("/orders/{order_id}")
async def get_order(order_id: int):
    await lookup_stock("get")
    if order_id not in orders:
        raise HTTPException(status_code=404, detail="order not found")
    return orders[order_id]


def require_chaos_token(token: str | None):
    if not CHAOS_TOKEN or not token or not hmac.compare_digest(token, CHAOS_TOKEN):
        raise HTTPException(status_code=403, detail="forbidden")


@app.post("/chaos/latency")
def chaos_latency(ms: int = 1000, seconds: int = 300, x_chaos_token: str | None = Header(default=None)):
    require_chaos_token(x_chaos_token)
    chaos.update(latency_ms=min(ms, 10000), until=time.time() + min(seconds, 1800))
    log.warning("chaos latency enabled", extra={"latency_ms": chaos["latency_ms"], "seconds": seconds})
    return dict(chaos)


@app.post("/chaos/errors")
def chaos_errors(rate: float = 0.5, seconds: int = 300, x_chaos_token: str | None = Header(default=None)):
    require_chaos_token(x_chaos_token)
    chaos.update(error_rate=max(0.0, min(rate, 1.0)), until=time.time() + min(seconds, 1800))
    log.warning("chaos errors enabled", extra={"error_rate": chaos["error_rate"], "seconds": seconds})
    return dict(chaos)


@app.post("/chaos/oom", status_code=202)
def chaos_oom(x_chaos_token: str | None = Header(default=None)):
    """Allocates memory in the background until the kernel kills the container (drill for OOMKilled)."""
    require_chaos_token(x_chaos_token)
    log.warning("chaos oom started")

    def hog():
        blocks = []
        while True:
            blocks.append(bytearray(8 * 1024 * 1024))
            time.sleep(0.05)

    threading.Thread(target=hog, daemon=True).start()
    return {"oom": "started"}


@app.post("/chaos/reset")
def chaos_reset(x_chaos_token: str | None = Header(default=None)):
    require_chaos_token(x_chaos_token)
    chaos.update(latency_ms=0, error_rate=0.0, until=0.0)
    log.info("chaos reset")
    return dict(chaos)
