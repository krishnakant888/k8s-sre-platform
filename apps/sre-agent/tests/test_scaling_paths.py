import json
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
        opened["workload"] = args[6]
        opened["operation"] = args[7]
        opened["evidence"] = args[8]
        return {"number": 8, "html_url": "https://example.test/pr/8", "_merged": True}

    monkeypatch.setattr(scale_down, "_open_recommendation", open_pr)
    return opened


LOW = {
    "cpu_p95_cores": 0.01, "cpu_throttle": 0.0,
    "memory_p99_bytes": 100 * 1024 * 1024,
    "cpu_coverage_seconds": 86400.0, "memory_coverage_seconds": 86400.0,
    "restarts_in_window": 0.0, "available_replicas": 3.0,
}


def test_short_scale_down_window_uses_fine_query_steps(monkeypatch):
    queries = []
    monkeypatch.setattr(scale_down, "WINDOW", "2m")
    monkeypatch.setattr(scale_down, "CPU_SUBQUERY_STEP", scale_down._subquery_step("2m", "15s", "5m"))
    monkeypatch.setattr(scale_down, "COVERAGE_SUBQUERY_STEP", scale_down._subquery_step("2m", "15s", "1m"))
    monkeypatch.setattr(scale_down, "_query", lambda query: queries.append(query) or 1.0)

    scale_down._metrics("platform", "demo", "orders-api")

    assert any("quantile_over_time(0.95" in query and "[2m:15s]" in query for query in queries)
    assert any("max_over_time((rate(" in query and "[2m:15s]" in query for query in queries)
    assert any("timestamp(rate(" in query and "[135s:15s]" in query for query in queries)
    assert any("timestamp(container_memory_working_set_bytes" in query and "[135s:15s]" in query
               for query in queries)


def test_scale_down_proposes_floor_directly_when_metrics_are_low(monkeypatch):
    five_replicas = DEPLOYMENT.replace("replicas: 3", "replicas: 5")
    opened = _stub_scale_down(monkeypatch, five_replicas, {**LOW, "available_replicas": 5.0})
    review = {}

    pr = scale_down._propose_for_workload("owner/repo", "main", "head", "orders-api", "demo", "platform", review)

    assert pr["_merged"] is True
    assert "replicas: 2" in opened["content"]
    assert "memory: 512Mi" in opened["content"]
    assert opened["evidence"]["candidate_floor"] == 2
    assert "candidate floor" in opened["operation"]
    assert review["configured_floor"] == 2
    assert review["decision"] == "proposal"
    assert "below their thresholds" in review["reason"]


def test_capacity_review_report_is_visible_in_rca_viewer_format(monkeypatch, tmp_path):
    monkeypatch.setattr(scale_down.config, "REPORTS_DIR", str(tmp_path))
    review = {
        "workload": "orders-api", "namespace": "demo", "cluster": "platform",
        "window": "2m", "started_at": "2026-11-08T02:30:00Z",
        "decision": "proposal", "reason": "CPU and memory evidence support the floor.",
        "current_replicas": 5, "candidate_floor": 2,
        "proposed_change": "replicas 5 to 2", "pr_number": 168,
        "pr_url": "https://example.test/pr/168", "merged": True,
        "metrics": LOW,
    }

    report_id = scale_down._write_capacity_report(review)
    report_dir = tmp_path / report_id
    report = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))

    assert report_id.startswith("rca-capacity-")
    assert report["report_type"] == "capacity"
    assert report["capacity"]["candidate_floor"] == 2
    assert report["remediation"]["pr_url"] == "https://example.test/pr/168"
    assert "**Candidate replica floor:** 2" in (report_dir / "rca.md").read_text(encoding="utf-8")
    assert scale_down._write_capacity_report(review) != report_id


def test_weekly_floor_review_requires_six_days_of_coverage_and_never_opens_pr(monkeypatch):
    monkeypatch.setattr(scale_down, "_load_manifest", lambda *a: (DEPLOYMENT, "blob"))
    monkeypatch.setattr(scale_down, "_metrics", lambda *a: {
        **LOW, "cpu_coverage_seconds": 12 * 60 * 60, "memory_coverage_seconds": 12 * 60 * 60,
    })
    review = scale_down._review_replica_floor("orders-api", "demo", "platform")

    assert review["review_type"] == "weekly_floor"
    assert review["decision"] == "skipped"
    assert "Insufficient seven-day history" in review["reason"]
    assert review["configured_floor"] == 2
    assert review["candidate_floor"] == 2
    assert "pr_url" not in review


def test_weekly_floor_review_is_read_only_and_keeps_the_configured_floor(monkeypatch):
    manifest = DEPLOYMENT.replace("replicas: 3", "replicas: 2")
    monkeypatch.setattr(scale_down, "_load_manifest", lambda *a: (manifest, "blob"))
    monkeypatch.setattr(scale_down, "_metrics", lambda *a: {
        **LOW, "cpu_coverage_seconds": 7 * 24 * 60 * 60,
        "memory_coverage_seconds": 7 * 24 * 60 * 60, "available_replicas": 2,
    })
    monkeypatch.setattr(scale_down, "_active_alert", lambda *a: False)
    review = scale_down._review_replica_floor("orders-api", "demo", "platform")

    assert review["decision"] == "recommendation"
    assert review["candidate_floor"] == 2
    assert review["proposed_change"].startswith("Retain the configured floor")
    assert "pr_url" not in review


def test_weekly_floor_report_uses_capacity_report_identity_and_recommendation_copy(monkeypatch, tmp_path):
    monkeypatch.setattr(scale_down.config, "REPORTS_DIR", str(tmp_path))
    review = {
        "review_type": "weekly_floor", "workload": "orders-api", "namespace": "demo",
        "cluster": "platform", "window": "7d", "decision": "recommendation",
        "candidate_floor": 2, "proposed_change": "Retain floor of 2 replicas",
        "reason": "Healthy weekly evidence supports keeping the policy floor.", "metrics": LOW,
    }

    report_id = scale_down._write_capacity_report(review)
    report = json.loads((tmp_path / report_id / "report.json").read_text(encoding="utf-8"))
    markdown = (tmp_path / report_id / "rca.md").read_text(encoding="utf-8")

    assert report["report_type"] == "capacity"
    assert report["capacity"]["review_type"] == "weekly_floor"
    assert "Weekly recommendation" in markdown
    assert "no scaling change or pull request was made" in report["summary"]


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
