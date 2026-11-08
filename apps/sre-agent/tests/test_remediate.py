from agent import remediate


def test_memory_limits_are_capped_at_configured_ceiling(monkeypatch):
    monkeypatch.setattr(remediate, "MAX_MEMORY_LIMIT_MIB", 4096)

    assert remediate._memory_limit_mib("2Gi") == 2048
    assert remediate._cap_memory_limit("2Gi") == "2Gi"
    assert remediate._cap_memory_limit("5Gi") == "4096Mi"


def test_replica_scaleout_stops_at_five():
    assert remediate._new_replicas(4, 0.5) == 5
    assert remediate._new_replicas(5, 0.5) == 5


def test_memory_chooses_vertical_then_horizontal_only_with_cpu_pressure(monkeypatch):
    monkeypatch.setattr(remediate, "MAX_MEMORY_LIMIT_MIB", 4096)

    assert remediate._memory_scaling_mode("2Gi", 0.8) == "vertical"
    assert remediate._memory_scaling_mode("4Gi", 0.25) == "horizontal"
    assert remediate._memory_scaling_mode("4Gi", 0.1) == "review"
    assert remediate._memory_scaling_mode("4Gi", None) == "review"


def test_cpu_throttle_query_matches_alert_metric(monkeypatch):
    seen = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"result": [{"value": [0, "0.42"]}]}}

    def get(url, params, timeout):
        seen.update(url=url, params=params, timeout=timeout)
        return Response()

    monkeypatch.setattr(remediate.httpx, "get", get)

    assert remediate._query_cpu_throttle_ratio("demo", "orders-api") == 0.42
    assert "container_cpu_cfs_throttled_periods_total" in seen["params"]["query"]
    assert "container_cpu_cfs_throttled_seconds_total" not in seen["params"]["query"]
    assert "[5m]" in seen["params"]["query"]
    assert "max by (pod, container)" in seen["params"]["query"]
