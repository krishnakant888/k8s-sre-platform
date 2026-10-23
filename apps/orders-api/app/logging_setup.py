import contextvars
import json
import logging
import os
import sys
import time

from opentelemetry import trace

# per request data shared with the log formatter (request_id and friends), a mutable dict so handlers can add to it
request_ctx: contextvars.ContextVar = contextvars.ContextVar("request_ctx", default=None)

# attributes on a LogRecord that are not user supplied extras
_STD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


def _static_fields() -> dict:
    """Who is logging: the release, where it runs and which pod. Read on every record so tests can change the env."""
    fields = {
        "version": os.getenv("APP_VERSION"),
        "environment": os.getenv("ENVIRONMENT"),
        "namespace": os.getenv("POD_NAMESPACE"),
        "pod": os.getenv("POD_NAME"),
        "node": os.getenv("NODE_NAME"),
    }
    return {k: v for k, v in fields.items() if v}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "service": self.service,
            **_static_fields(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        ctx = request_ctx.get()
        if ctx:
            entry["request_id"] = ctx.get("request_id")
        span = trace.get_current_span().get_span_context()
        if span.is_valid:
            entry["trace_id"] = format(span.trace_id, "032x")
            entry["span_id"] = format(span.span_id, "016x")
        for key, value in record.__dict__.items():
            if key not in _STD:
                entry[key] = value
        if record.exc_info:
            entry["error_type"] = record.exc_info[0].__name__
            entry["error_message"] = str(record.exc_info[1])[:300]
            entry["stack"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def setup_logging(service: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    # uvicorn gets its own handlers, route its logs through ours and drop the access log (the middleware logs requests)
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    logging.getLogger("uvicorn.access").disabled = True
