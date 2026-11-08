from agent import scale_down


DEPLOYMENT = """spec:
  replicas: 3
  template:
    spec:
      containers:
        - name: orders-api
          resources:
            requests: {cpu: 50m, memory: 256Mi}
            limits: {cpu: 100m, memory: 512Mi}
"""


def test_reads_resource_quantities_from_inline_yaml():
    assert scale_down._quantity_values(DEPLOYMENT, "requests") == {"cpu": "50m", "memory": "256Mi"}
    assert scale_down._quantity_values(DEPLOYMENT, "limits") == {"cpu": "100m", "memory": "512Mi"}


def test_reduces_only_the_requested_resource_or_replica_step():
    replica_change = scale_down._replace_replicas(DEPLOYMENT, 3, 2)
    memory_change = scale_down._replace_resource(DEPLOYMENT, "limits", "memory", "512Mi", "384Mi")
    cpu_change = scale_down._replace_resource(DEPLOYMENT, "limits", "cpu", "100m", "50m")

    assert "replicas: 2" in replica_change and "memory: 512Mi" in replica_change
    assert "memory: 384Mi" in memory_change and "cpu: 100m" in memory_change
    assert "cpu: 50m" in cpu_change and "memory: 512Mi" in cpu_change


def test_cpu_and_memory_units_are_parsed():
    assert scale_down._cpu_millicores("250m") == 250
    assert scale_down._cpu_millicores("1") == 1000
    assert scale_down._memory_mib("1Gi") == 1024
    assert scale_down._memory_mib("512Mi") == 512


def test_scale_down_pr_attempts_to_merge(monkeypatch):
    calls = []

    def post(path, body):
        calls.append(("post", path))
        return {"number": 12, "title": body.get("title"), "html_url": "https://example.test/pr/12"}

    def put(path, body):
        calls.append(("put", path))
        return {"merged": True}

    monkeypatch.setattr(scale_down, "_post", post)
    monkeypatch.setattr(scale_down, "_put", put)

    pr = scale_down._open_recommendation("owner/repo", "main", "abc", "manifest.yaml", "blob",
                                          DEPLOYMENT, "orders-api", "replicas 3 to 2",
                                          {"cpu_pct": 20, "throttle_pct": 0, "memory_pct": 30})

    assert pr["_merged"] is True
    assert ("put", "repos/owner/repo/pulls/12/merge") in calls
