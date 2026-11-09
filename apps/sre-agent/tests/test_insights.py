"""The rule based insights of a report: blast radius, change, confidence, competing causes, recurrence, related alerts, chain, trend."""
import json

from agent import insights
from test_generic import NOW, alert, event, fake, pod, rca


def run(monkeypatch, tmp_path, a, **kw):
    fake(monkeypatch, **kw)
    ctx = rca.gather(a, NOW)
    report = rca.analyze_rules(a, ctx, NOW)
    return report, ctx, insights.build(report, ctx, NOW, str(tmp_path))


def badimage(monkeypatch, tmp_path, **kw):
    a = alert("ContainerWaiting", cluster="platform", namespace="demo", pod="orders-api-7d9f8b5c6d-abcde", reason="ImagePullBackOff")
    return run(monkeypatch, tmp_path, a, pods=[pod(waiting="ImagePullBackOff")], events=[event("Failed", 'Failed to pull image "x:bad": manifest unknown')], **kw)


def test_alternatives_mark_what_the_events_support(monkeypatch, tmp_path):
    _, _, i = badimage(monkeypatch, tmp_path)
    causes = {a["cause"]: a["supported"] for a in i["alternatives"]}
    assert causes["wrong tag or image name"] and not causes["missing pull credentials"]
    assert i["alternatives"][0]["supported"]   # supported causes first


def test_blast_radius_names_what_depends_on_the_service(monkeypatch, tmp_path):
    _, _, i = badimage(monkeypatch, tmp_path)
    assert any("orders URL through the gateway" in x for x in i["blast_radius"]) and any("users of orders-api" in x for x in i["blast_radius"])


def test_confidence_lists_missing_sources(monkeypatch, tmp_path):
    _, _, i = badimage(monkeypatch, tmp_path)
    assert i["confidence"]["level"] in ("low", "medium", "high")
    assert any("tempo" in x for x in i["confidence"]["check_next"]) or "tempo" in i["confidence"]["agreeing_sources"]


def test_change_is_likely_when_a_deploy_landed_just_before(monkeypatch, tmp_path):
    report, ctx, _ = badimage(monkeypatch, tmp_path)
    ctx["signals"]["recent_deploy"] = {"revision": "abc1234", "deployed_at": rca.iso(NOW - 300 - 600)}
    c = insights.change(report, ctx["signals"])
    assert c["likely"] and c["minutes_before_alert"] == 10 and "abc1234" in c["note"]
    ctx["signals"]["recent_deploy"] = None
    assert not insights.change(report, ctx["signals"])["likely"]


def test_recurrence_and_related_alerts_come_from_earlier_reports(monkeypatch, tmp_path):
    report, ctx, _ = badimage(monkeypatch, tmp_path)
    for n, (alertname, svc, ago) in enumerate([(report.alert, report.service, 86400), (report.alert, report.service, 172800), ("NodeNotReady", "node", 60)]):
        d = tmp_path / f"r{n}"
        d.mkdir()
        (d / "report.json").write_text(json.dumps({"id": f"r{n}", "alert": alertname, "service": svc, "started_at": rca.iso(NOW - 300 - ago), "priority": {"level": "P2"}, "alert_details": {"cluster": "platform"}}))
    i = insights.build(report, ctx, NOW, str(tmp_path))
    assert i["recurrence"]["times_before"] == 2 and "3th" in i["recurrence"]["note"]
    assert [r["alert"] for r in i["related"]] == ["NodeNotReady"]


def test_chain_flags_one_hop_and_trend_and_section_render(monkeypatch, tmp_path):
    report, ctx, i = badimage(monkeypatch, tmp_path)
    assert sum(h["problem"] for h in i["chain"]) == 1
    text = insights.section(i)
    for heading in ("What else is affected", "Was it a change?", "How sure is this", "Other possible causes", "Dependency chain", "Seen before", "Confirming the fix"):
        assert heading in text
    assert insights.build(report, {"evidence": []}, NOW, str(tmp_path / "missing"))["alternatives"] is not None
