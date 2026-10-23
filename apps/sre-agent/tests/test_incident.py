import json
import re

from agent import config, incident

DNS_1123 = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
ALERT = {
    "status": "firing",
    "labels": {"alertname": "OrdersApiErrorBudgetFastBurn", "severity": "page", "service": "orders-api"},
    "fingerprint": "ab12cd34ef56",
    "startsAt": "2026-10-23T13:55:00.123Z",
}


def test_workflow_name_is_deterministic():
    assert incident.workflow_name(ALERT) == incident.workflow_name(dict(ALERT))


def test_workflow_name_differs_per_alert_instance():
    other = {**ALERT, "fingerprint": "ffffffffffff"}
    later = {**ALERT, "startsAt": "2026-10-23T14:55:00Z"}
    assert len({incident.workflow_name(a) for a in (ALERT, other, later)}) == 3


def test_workflow_name_is_a_valid_object_name():
    for name in ("OrdersApiErrorBudgetFastBurn", "weird name / with * chars!!", "x" * 200, "", "---"):
        wf = incident.workflow_name({"labels": {"alertname": name}, "fingerprint": "1"})
        assert len(wf) <= 63
        assert DNS_1123.match(wf), wf


def test_build_workflow_references_the_template_and_carries_the_alert():
    wf = incident.build_workflow(ALERT)
    assert wf["kind"] == "Workflow"
    assert wf["metadata"]["namespace"] == config.WORKFLOW_NAMESPACE
    assert wf["spec"]["workflowTemplateRef"] == {"name": config.WORKFLOW_TEMPLATE}
    (param,) = wf["spec"]["arguments"]["parameters"]
    assert param["name"] == "alert"
    assert json.loads(param["value"]) == ALERT
    label = wf["metadata"]["labels"]["sre-agent/alertname"]
    assert len(label) <= 63 and DNS_1123.match(label)


def test_fit_evidence_leaves_small_context_alone():
    ctx = {"service": "orders-api", "evidence": [{"q": "a"}, {"q": "b"}]}
    assert incident.fit_evidence(dict(ctx, evidence=list(ctx["evidence"]))) == ctx


def test_fit_evidence_drops_items_until_it_fits_and_says_so():
    ctx = {"service": "orders-api", "evidence": [{"q": str(i), "data": "x" * 1000} for i in range(50)]}
    out = incident.fit_evidence(ctx, limit=10_000)
    assert len(json.dumps(out)) <= 10_000 + 40
    assert 0 < len(out["evidence"]) < 50
    assert out["truncated_evidence_items"] == 50 - len(out["evidence"])
