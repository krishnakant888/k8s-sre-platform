"""Alerts that are not about one service's SLO go through generic.py. Each test replays one kind of fault with canned tool
answers and checks the diagnosis (cause, layer), the priority and that the report validates and reads as a runbook."""
import time

import pytest

from agent import generic, rca, tools
from agent.models import RcaReport

NOW = time.time()


def alert(alertname, severity="ticket", **labels):
    return {"labels": {"alertname": alertname, "severity": severity, **labels}, "startsAt": rca.iso(NOW - 300), "fingerprint": "f"}


def series(value):
    return {"series": [{"labels": {}, "min": value, "max": value, "avg": value, "last": value}]}


def pod(name="orders-api-7d9f8b5c6d-abcde", ready=False, waiting=None, restarts=0, term=None, node="platform-worker", limit="192Mi"):
    return {"pod": name, "phase": "Running", "ready": ready, "restarts": restarts, "waiting": waiting,
            "last_terminated": {"reason": term[0], "exit_code": term[1], "at": ""} if term else None, "started_at": None,
            "memory_limit": limit, "node": node, "app_instance": "orders-api"}


def event(reason, message, obj="orders-api-7d9f8b5c6d-abcde"):
    return {"ts": rca.iso(NOW - 120), "type": "Warning", "reason": reason, "object": obj, "message": message}


def fake(monkeypatch, *, pods=None, events=None, logs=None, prom=None, argocd=None, pods_error=None, deploys_error=True):
    prom = prom or {}

    def call(name, args):
        if name == "query_promql":
            q = args["query"]
            for key, val in prom.items():
                if key in q:
                    return val if isinstance(val, dict) else series(val)
            return {"series": []}
        if name == "query_logql":
            lines = logs or []
            return {"total": sum(c for _, c in lines), "top_messages": lines, "fields_present": ["msg", "level"], "lines": [], "breakdown": {}}
        if name == "search_traces":
            return {"traces": []}
        if name == "get_pod_status":
            return {"error": pods_error} if pods_error else {"pods": pods or []}
        if name == "get_k8s_events":
            return {"events": events or []}
        if name == "get_argocd_app":
            return argocd or {"error": "not found"}
        if name == "get_recent_deploys":
            return {"error": "no application"} if deploys_error else {"source_files": ["gitops/workloads/orders-api"], "history": []}
        return {"error": f"unexpected tool {name}"}

    monkeypatch.setattr(tools, "call_tool", call)


def investigate(a):
    ctx = rca.gather(a, NOW)
    report = rca.analyze_rules(a, ctx, NOW)
    RcaReport.model_validate(report.model_dump())  # the stored report must validate
    return report


def test_generic_families_are_routed_and_slo_alerts_are_not():
    assert generic.is_generic(alert("NodeNotReady"))
    assert not generic.is_generic(alert("OrdersApiErrorBudgetFastBurn"))


def test_crash_loop_from_the_application_is_application_level_and_p1_when_nothing_serves(monkeypatch):
    fake(monkeypatch, pods=[pod(waiting="CrashLoopBackOff", restarts=6, term=("Error", 1))], logs=[("KeyError: DATABASE_URL", 12)], prom={"restarts_total": 6})
    r = investigate(alert("ContainerWaiting", cluster="platform", namespace="demo", pod="orders-api-7d9f8b5c6d-abcde", container="orders-api", reason="CrashLoopBackOff"))
    assert r.root_cause.category == "pod_crash"
    assert r.fault_domain.layer == "application"
    assert "KeyError: DATABASE_URL" in r.what_happened
    assert r.priority.level == "P1"  # a user-facing service with no ready pod
    assert any("logs" in c and "--previous" in c for c in r.how_to_fix.immediate)
    assert "kind-platform" in " ".join(r.how_to_fix.verify)


def test_image_pull_error_names_the_missing_tag(monkeypatch):
    fake(monkeypatch, pods=[pod(waiting="ImagePullBackOff")], events=[event("Failed", 'Failed to pull image "ghcr.io/x/orders-api:sha-bad": manifest unknown')])
    r = investigate(alert("ContainerWaiting", cluster="platform", namespace="demo", pod="orders-api-7d9f8b5c6d-abcde", reason="ImagePullBackOff"))
    assert r.root_cause.category == "image_pull_failure"
    assert r.fault_domain.layer == "application"
    assert "manifest unknown" in r.what_happened


def test_missing_secret_is_a_config_error(monkeypatch):
    fake(monkeypatch, pods=[pod(waiting="CreateContainerConfigError")], events=[event("Failed", 'Error: secret "orders-api-chaos" not found')])
    r = investigate(alert("ContainerWaiting", cluster="platform", namespace="demo", pod="orders-api-7d9f8b5c6d-abcde", reason="CreateContainerConfigError"))
    assert r.root_cause.category == "config_error"
    assert "orders-api-chaos" in r.fault_domain.component


def test_unschedulable_pod_is_a_capacity_problem(monkeypatch):
    fake(monkeypatch, pods=[], events=[event("FailedScheduling", "0/3 nodes are available: 3 Insufficient memory.", obj="batch-0")])
    r = investigate(alert("PodUnschedulable", cluster="platform", namespace="demo", pod="batch-0"))
    assert r.root_cause.category == "scheduling_failure"
    assert r.fault_domain.layer == "infrastructure"
    assert "memory" in r.fault_domain.component


@pytest.mark.parametrize("growth,layer", [(1.0, "infrastructure"), (2.5, "application")])
def test_memory_near_limit_separates_sizing_from_a_leak(monkeypatch, growth, layer):
    mem_points = {"series": [{"labels": {}, "min": 100, "max": 100 * growth, "avg": 100, "last": 100 * growth, "points": [(i, 100 + (100 * growth - 100) * i / 11) for i in range(12)]}]}
    fake(monkeypatch, pods=[pod(ready=True, term=("OOMKilled", 137), restarts=2)], prom={"container_memory_working_set_bytes{": mem_points, "/ on (cluster, namespace, pod, container) group_left": 0.96, "restarts_total": 2})
    r = investigate(alert("ContainerMemoryNearLimit", cluster="platform", namespace="demo", pod="orders-api-7d9f8b5c6d-abcde"))
    assert r.root_cause.category == "resource_exhaustion"
    assert r.fault_domain.layer == layer
    assert "384Mi" in " ".join(r.how_to_fix.immediate)  # twice the 192Mi limit


def test_node_not_ready_is_infrastructure_and_p1_when_users_run_there(monkeypatch):
    fake(monkeypatch, prom={'condition="Ready"': {"series": [{"labels": {"node": "platform-worker"}, "min": 0, "max": 0, "avg": 0, "last": 0}]}, "count(kube_pod_info": 7,
                            'namespace=~"demo"': {"series": [{"labels": {"pod": "orders-api-7d9f8b5c6d-abcde"}, "min": 1, "max": 1, "avg": 1, "last": 1}]}})
    r = investigate(alert("NodeNotReady", "page", cluster="platform", node="platform-worker"))
    assert r.root_cause.category == "node_failure"
    assert r.fault_domain.layer == "infrastructure"
    assert r.priority.level == "P1"
    assert "docker start platform-worker" in " ".join(r.how_to_fix.immediate)


def test_ingress_errors_with_no_ready_backend(monkeypatch):
    fake(monkeypatch, pods=[pod(ready=False, waiting="CrashLoopBackOff")], prom={
        'status=~"5.."}[2m])) /': 0.9, "request_duration_seconds_bucket": 0.2, "sum by (status)": 0.5, "sum(rate(nginx_ingress_controller_requests{cluster": 4.0,
        "kube_ingress_path": {"series": [{"labels": {"service_name": "orders-api"}, "min": 1, "max": 1, "avg": 1, "last": 1}]}})
    r = investigate(alert("IngressHighErrorRate", "page", cluster="platform", namespace="demo", ingress="orders-api", host="orders.localhost"))
    assert r.root_cause.category == "ingress_backend_failure"
    assert "no ready pod" in r.fault_domain.component
    assert r.priority.level == "P1"


def test_argocd_manifest_error_is_application_level(monkeypatch):
    fake(monkeypatch, argocd={"name": "prod-orders-api", "sync_status": "Unknown", "health_status": "Healthy", "destination": "platform", "destination_namespace": "demo",
                              "conditions": ["ComparisonError: kustomize build failed: accumulating resources: missing file"], "operation": None, "unhealthy": [], "paths": ["gitops/workloads/orders-api"]})
    r = investigate(alert("ArgoAppOutOfSync", name="prod-orders-api", dest_name="platform"))
    assert r.root_cause.category == "gitops_sync_failure"
    assert r.fault_domain.layer == "application"
    assert "Git" in r.fault_domain.component


def test_argocd_connection_error_is_infrastructure(monkeypatch):
    fake(monkeypatch, argocd={"name": "prod-orders-api", "sync_status": "Unknown", "health_status": "Unknown", "destination": "platform", "destination_namespace": "demo",
                              "conditions": ["ComparisonError: dial tcp 172.18.0.2:6443: connect: connection refused"], "operation": None, "unhealthy": [], "paths": []})
    r = investigate(alert("ArgoAppOutOfSync", name="prod-orders-api", dest_name="platform"))
    assert r.fault_domain.layer == "infrastructure"
    assert "bootstrap.py --only" in " ".join(r.how_to_fix.immediate)


def test_unreachable_cluster_is_reported_as_a_telemetry_gap(monkeypatch):
    fake(monkeypatch, pods_error="ConnectError: connection refused", prom={})
    r = investigate(alert("ClusterTelemetryMissing", "page", cluster="platform", service="alloy"))
    assert r.root_cause.category == "telemetry_gap"
    assert r.fault_domain.layer == "infrastructure"
    assert r.priority.level in ("P1", "P2")
    assert any("unreachable" in g or "incomplete" in g for g in r.observability_gaps + r.contributing_factors)


def test_every_family_has_a_report_that_reads_as_a_runbook(monkeypatch):
    """Smoke test: no family crashes on empty data, and each report has the sections an on-call engineer needs."""
    fake(monkeypatch)
    for name in generic.FAMILIES:
        r = investigate(alert(name, cluster="platform", namespace="demo", pod="x-0", node="n1", ingress="x", host="h", name="app", persistentvolumeclaim="pvc"))
        md = r.rca_markdown
        assert "What happened" in md and "How to fix it now" in md
        assert r.priority.level in ("P1", "P2", "P3", "P4"), name
        assert r.how_to_fix.immediate and r.how_to_fix.verify, name
