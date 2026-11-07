from agent import activities, config, rca


def test_alert_details_and_section():
    alert = {"status": "firing", "startsAt": "2026-11-07T10:00:00Z", "fingerprint": "abc",
             "labels": {"alertname": "PodStuckPending", "severity": "ticket", "cluster": "platform", "namespace": "sandbox", "pod": "x-1"},
             "annotations": {"summary": "x-1 has not started", "description": "Pod is Pending", "other": "dropped"}}
    d = activities.alert_details(alert)
    assert d["cluster"] == "platform" and d["namespace"] == "sandbox" and d["labels"]["pod"] == "x-1"
    assert set(d["annotations"]) == {"summary", "description"}
    md = activities.alert_section(d)
    assert "## Alert details" in md and "`pod=x-1`" in md and "Pod is Pending" in md
    assert activities.alert_section({}) == ""


def test_analyze_adds_them_to_the_report(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    alert = {"status": "firing", "startsAt": "2026-11-07T10:00:00Z", "labels": {"alertname": "X", "namespace": "demo"}, "annotations": {}}

    class R:
        rca_markdown = "# RCA\n"
        alert_details = {}
        contributing_factors = []

        def model_dump(self):
            return {"alert_details": self.alert_details, "rca_markdown": self.rca_markdown}

    monkeypatch.setattr(rca, "analyze_rules", lambda a, c: R())
    out = activities.analyze(alert, {})
    assert out["alert_details"]["namespace"] == "demo" and "## Alert details" in out["rca_markdown"]
