import json
import logging
import os
import socket
import sys
import time

_STD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


def _static_fields() -> dict:
    """Where the agent runs: release, pod (the hostname in Kubernetes), namespace and workflow when it runs in one."""
    fields = {
        "version": os.getenv("APP_VERSION"),
        "pod": os.getenv("POD_NAME") or socket.gethostname(),
        "namespace": os.getenv("POD_NAMESPACE"),
        "node": os.getenv("NODE_NAME"),
        "workflow": os.getenv("WORKFLOW_NAME"),
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
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("httpx").setLevel(logging.WARNING)
