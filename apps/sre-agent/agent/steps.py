"""Entrypoints for the steps of the incident Argo Workflow (gitops/workloads/sre-agent/workflow-template.yaml).

    python -m agent.steps <collect|analyze|store|notify>

Inputs are files that Argo writes into /tmp/in (raw artifacts built from workflow parameters and earlier step
outputs). Outputs go to /tmp/out, where Argo reads them back as output parameters. Env vars and args are avoided
for the payloads because a single environment string is capped at 128 KiB.
"""
import json
import logging
import os
import sys
from pathlib import Path

from agent import activities, incident
from agent.logging_setup import setup_logging

IN_DIR = Path(os.getenv("STEP_IN_DIR", "/tmp/in"))
OUT_DIR = Path(os.getenv("STEP_OUT_DIR", "/tmp/out"))


def _read(name: str):
    return json.loads((IN_DIR / name).read_text())


def _write(name: str, value) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / name).write_text(value if isinstance(value, str) else json.dumps(value))


def collect() -> None:
    _write("ctx.json", incident.fit_evidence(activities.collect_evidence(_read("alert.json"))))


def analyze() -> None:
    _write("report.json", activities.analyze(_read("alert.json"), _read("ctx.json")))


def store() -> None:
    _write("report-id", activities.store_report(_read("report.json")))


def notify() -> None:
    activities.notify(_read("report.json"))


STEPS = {"collect": collect, "analyze": analyze, "store": store, "notify": notify}


def main(argv: list[str]) -> int:
    setup_logging("sre-agent")
    if len(argv) != 2 or argv[1] not in STEPS:
        print(f"usage: python -m agent.steps <{'|'.join(STEPS)}>", file=sys.stderr)
        return 2
    logging.getLogger("sre-agent").info("step starting", extra={"step": argv[1]})
    STEPS[argv[1]]()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
