from types import SimpleNamespace

from agent import remediate, scale_down


CTX = {"signals": {
    "workload": "orders-api", "namespace": "demo",
    "source_files": ["gitops/workloads/orders-api"],
}}
REPORT = SimpleNamespace(id="rca-test-1", alert="ContainerCpuThrottled")
DEPLOYMENT = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: orders-api
spec:
  replicas: 3
  template:
    spec:
      containers:
        - name: orders-api
          resources:
            requests: {cpu: 50m, memory: 256Mi}
            limits: {cpu: 100m, memory: 512Mi}
"""


def stub_repo(monkeypatch, content=DEPLOYMENT):
    monkeypatch.setattr(remediate, "_find_resource_file", lambda *a, **k: ("deploy.yaml", content, "blob"))
    monkeypatch.setattr(remediate, "_open_and_merge_pr", lambda *a, **k: {
        "html_url": "https://example.test/pr/1", "number": 1, "_merged": True,
    })


def test_cpu_throttle_scales_out_before_replica_cap(monkeypatch):
    stub_repo(monkeypatch)
    monkeypatch.setattr(remediate, "_query_cpu_throttle_ratio", lambda *a: 0.4)

    result = remediate._remediate_scaleout(CTX, REPORT, "owner/repo", "main", "head")

    assert (result["type"], result["old_replicas"], result["new_replicas"]) == ("scaleout", 3, 5)


def test_cpu_at_replica_cap_scales_limit_vertically(monkeypatch):
    manifest = DEPLOYMENT.replace("replicas: 3", "replicas: 5")
    stub_repo(monkeypatch, manifest)
    monkeypatch.setattr(remediate, "MAX_REPLICAS", 5)
    monkeypatch.setattr(remediate, "_query_cpu_throttle_ratio", lambda *a: 0.5)
    monkeypatch.setattr(remediate, "_query_cpu_p95_cores", lambda *a: 0.2)

    result = remediate._remediate_scaleout(CTX, REPORT, "owner/repo", "main", "head")

    assert result["type"] == "cpu_scaleup"
    assert result["old_cpu_limit"] == "100m"
    assert result["new_cpu_limit"] == "300m"


def test_oom_scales_memory_limit_vertically_from_peak_metric(monkeypatch):
    stub_repo(monkeypatch)
    monkeypatch.setattr(remediate, "_query_peak_bytes", lambda *a: 500 * 1024 * 1024)

    result = remediate._remediate_oom(CTX, REPORT, "owner/repo", "main", "head")

    assert result["type"] == "oom"
    assert result["old_limit"] == "512Mi"
    assert result["new_limit"] == "640Mi"


def test_memory_at_cap_scales_out_only_with_cpu_load_metric(monkeypatch):
    manifest = DEPLOYMENT.replace("memory: 512Mi", "memory: 4096Mi")
    stub_repo(monkeypatch, manifest)
    monkeypatch.setattr(remediate, "MAX_MEMORY_LIMIT_MIB", 4096)
    monkeypatch.setattr(remediate, "_query_cpu_throttle_ratio", lambda *a: 0.5)

    result = remediate._remediate_oom(CTX, REPORT, "owner/repo", "main", "head")

    assert result["type"] == "scaleout"
    assert (result["old_replicas"], result["new_replicas"]) == (3, 5)


def test_memory_at_cap_without_cpu_pressure_stops_for_review(monkeypatch):
    manifest = DEPLOYMENT.replace("memory: 512Mi", "memory: 4096Mi")
    stub_repo(monkeypatch, manifest)
    monkeypatch.setattr(remediate, "MAX_MEMORY_LIMIT_MIB", 4096)
    monkeypatch.setattr(remediate, "_query_cpu_throttle_ratio", lambda *a: 0.1)

    assert remediate._remediate_oom(CTX, REPORT, "owner/repo", "main", "head") is None


def _stub_scale_down(monkeypatch, manifest, metrics):
    monkeypatch.setattr(scale_down, "_has_open_recommendation", lambda *a: False)
    monkeypatch.setattr(scale_down, "_recent_commit", lambda *a: False)
    monkeypatch.setattr(scale_down, "_load_manifest", lambda *a: (manifest, "blob"))
    monkeypatch.setattr(scale_down, "_metrics", lambda *a: metrics)
    monkeypatch.setattr(scale_down, "_active_alert", lambda *a: False)
    opened = {}

    def open_pr(*args):
        opened["content"] = args[5]
        opened["operation"] = args[6]
        return {"number": 8, "html_url": "https://example.test/pr/8", "_merged": True}

    monkeypatch.setattr(scale_down, "_open_recommendation", open_pr)
    return opened


LOW = {
    "cpu_p95_cores": 0.01, "cpu_throttle": 0.0,
    "memory_p99_bytes": 100 * 1024 * 1024,
    "cpu_coverage_seconds": 86400.0, "memory_coverage_seconds": 86400.0,
    "restarts_24h": 0.0, "available_replicas": 3.0,
}


def test_scale_down_reduces_replicas_one_step_above_floor(monkeypatch):
    opened = _stub_scale_down(monkeypatch, DEPLOYMENT, LOW)

    pr = scale_down._propose_for_workload("owner/repo", "main", "head", "orders-api", "demo", "platform")

    assert pr["_merged"] is True
    assert "replicas: 2" in opened["content"]
    assert "memory: 512Mi" in opened["content"]


def test_scale_down_reduces_memory_at_replica_floor(monkeypatch):
    floor_manifest = DEPLOYMENT.replace("replicas: 3", "replicas: 2")
    opened = _stub_scale_down(monkeypatch, floor_manifest, LOW)

    scale_down._propose_for_workload("owner/repo", "main", "head", "orders-api", "demo", "platform")

    assert "replicas: 2" in opened["content"]
    assert "memory: 384Mi" in opened["content"]


def test_scale_down_reduces_cpu_at_floor_when_memory_is_not_low(monkeypatch):
    floor_manifest = DEPLOYMENT.replace("replicas: 3", "replicas: 2")
    metrics = {**LOW, "memory_p99_bytes": 450 * 1024 * 1024}
    opened = _stub_scale_down(monkeypatch, floor_manifest, metrics)

    scale_down._propose_for_workload("owner/repo", "main", "head", "orders-api", "demo", "platform")

    assert "replicas: 2" in opened["content"]
    assert "cpu: 50m" in opened["content"]


def test_scale_down_fails_closed_when_metric_history_is_short(monkeypatch):
    floor_manifest = DEPLOYMENT.replace("replicas: 3", "replicas: 2")
    metrics = {**LOW, "cpu_coverage_seconds": 900.0, "memory_coverage_seconds": 900.0}
    opened = _stub_scale_down(monkeypatch, floor_manifest, metrics)
    monkeypatch.setattr(scale_down, "MIN_COVERAGE_SECONDS", 86400)

    result = scale_down._propose_for_workload("owner/repo", "main", "head", "orders-api", "demo", "platform")

    assert result is None
    assert opened == {}
