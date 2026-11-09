"""Tests of scripts/selfservice.py on a copy of the real manifests: run with `python3 -m pytest scripts/test_selfservice.py`."""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent))
import selfservice as ss  # noqa: E402

REAL = Path(__file__).resolve().parent.parent


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    for d in ("gitops", "scenarios"):
        shutil.copytree(REAL / d, tmp_path / d)
    monkeypatch.setattr(ss, "ROOT", tmp_path)
    for cmd in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"], ["add", "-A"], ["commit", "-q", "-m", "base"]):
        subprocess.run(["git", "-C", str(tmp_path), *cmd], check=True, capture_output=True)
    return tmp_path


def run(action, **params):
    return ss.ACTIONS[action](params)


def deployment(repo, name):
    path, parts, i = ss.find("Deployment", name)
    return yaml.safe_load(parts[i])


def test_scale(repo):
    r = run("scale", service="orders-api", replicas=3)
    assert deployment(repo, "orders-api")["spec"]["replicas"] == 3
    assert "scale to 3" in r["title"]
    with pytest.raises(ss.Refused):
        run("scale", service="orders-api", replicas=3)          # already 3
    with pytest.raises(ss.Refused):
        run("scale", service="orders-api", replicas=0)
    with pytest.raises(ss.Refused):
        run("scale", service="nope", replicas=2)


def test_resources(repo):
    run("resources", service="orders-api", memory_limit="1Gi", memory_request="512Mi", cpu_request="200m")
    res = deployment(repo, "orders-api")["spec"]["template"]["spec"]["containers"][0]["resources"]
    assert res == {"requests": {"cpu": "200m", "memory": "512Mi"}, "limits": {"memory": "1Gi"}}
    with pytest.raises(ss.Refused):
        run("resources", service="orders-api", memory_limit="128Mi", memory_request="512Mi")   # request above limit
    with pytest.raises(ss.Refused):
        run("resources", service="orders-api", memory_limit="lots")


def test_env_adds_replaces_and_refuses(repo):
    run("env", service="orders-api", name="FEATURE_X", value="on")
    env = {e["name"]: e for e in deployment(repo, "orders-api")["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["FEATURE_X"]["value"] == "on" and "CHAOS_TOKEN" in env       # the others stay
    run("env", service="orders-api", name="FEATURE_X", value="off")
    env = {e["name"]: e for e in deployment(repo, "orders-api")["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["FEATURE_X"]["value"] == "off"
    with pytest.raises(ss.Refused, match="secret"):
        run("env", service="orders-api", name="DB_PASSWORD", value="x")
    with pytest.raises(ss.Refused, match="valueFrom"):
        run("env", service="orders-api", name="POD_NAME", value="x")


def test_image_and_rollback(repo):
    run("image", service="orders-api", tag="v2")
    img = lambda: deployment(repo, "orders-api")["spec"]["template"]["spec"]["containers"][0]["image"]  # noqa: E731
    assert img() == "ghcr.io/krishnakant888/orders-api:v2"
    with pytest.raises(ss.Refused, match="latest"):
        run("image", service="orders-api", tag="latest")
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "v2"], check=True, capture_output=True)
    r = run("rollback", service="orders-api")
    assert img().startswith("ghcr.io/krishnakant888/orders-api:sha-") and "roll back" in r["title"]


def test_route(repo):
    run("route", service="orders-api", host="www.localhost")
    path, parts, i = ss.find("HTTPRoute", "orders-api")
    assert yaml.safe_load(parts[i])["spec"]["hostnames"] == ["orders.localhost", "www.localhost"]
    with pytest.raises(ss.Refused):
        run("route", service="orders-api", host="www.localhost")
    with pytest.raises(ss.Refused):
        run("route", service="orders-api", host="dummy.localhost")        # used by another route
    with pytest.raises(ss.Refused):
        run("route", service="orders-api", host="evil.example.com")


def test_promote(repo):
    (repo / "gitops/workloads/shop").mkdir()
    (repo / "gitops/workloads/shop/shop.yaml").write_text("apiVersion: v1\nkind: ServiceAccount\nmetadata:\n  name: shop\n  namespace: sandbox\n")
    app = (repo / "gitops/apps/orders-api.yaml").read_text().replace("orders-api", "shop").replace("namespace: demo", "namespace: sandbox")
    (repo / "gitops/apps/shop.yaml").write_text(app)
    run("promote", service="shop")
    assert "namespace: demo" in (repo / "gitops/workloads/shop/shop.yaml").read_text()
    assert yaml.safe_load((repo / "gitops/apps/shop.yaml").read_text())["spec"]["destination"]["namespace"] == "demo"


@pytest.mark.parametrize("kind", list(ss.ALERTS))
def test_alert(repo, kind):
    r = run("alert", service="orders-api", kind=kind)
    cm = yaml.safe_load((repo / r["files"][0]).read_text())
    rules = yaml.safe_load(cm["data"][f"request-orders-api-{kind}.yaml"])
    rule = rules["groups"][0]["rules"][0]
    assert rule["data"][0]["model"]["expr"].count("(") == rule["data"][0]["model"]["expr"].count(")")
    assert 'namespace="demo"' in rule["data"][0]["model"]["expr"] and cm["metadata"]["labels"]["grafana_alert"] == "1"
    with pytest.raises(ss.Refused):
        run("alert", service="orders-api", kind=kind)                   # already there


def test_dashboard(repo):
    r = run("dashboard", service="orders-api", panels=[{"title": "Mine", "expr": "up", "unit": "short"}])
    cm = yaml.safe_load((repo / r["files"][0]).read_text())
    dash = json.loads(cm["data"]["svc-orders-api.json"])
    assert len(dash["panels"]) == 9 and dash["panels"][-1]["title"] == "Mine"


def test_sandbox_access_and_expiry(repo):
    run("sandbox", name="alice", owner="group:default/platform-team", days=2)
    docs = [d for d in yaml.safe_load_all((repo / "gitops/platform/base/sandbox-alice.yaml").read_text()) if d]
    assert docs[0]["metadata"]["name"] == "sbx-alice" and {d["kind"] for d in docs} >= {"Namespace", "ResourceQuota", "NetworkPolicy", "LimitRange"}
    assert all(d["metadata"].get("namespace", "sbx-alice") == "sbx-alice" for d in docs if d["kind"] != "Namespace")
    with pytest.raises(ss.Refused):
        run("sandbox", name="alice", owner="x", days=2)
    with pytest.raises(ss.Refused):
        run("sandbox", name="alice2", owner="x", days=60)
    run("access", user="bob", namespace="sbx-alice", days=1)
    with pytest.raises(ss.Refused):
        run("access", user="bob", namespace="kube-system", days=1)
    with pytest.raises(ss.Refused, match="nothing has expired"):
        run("expire")
    for f in (repo / "gitops/platform/base").glob("*-alice*.yaml"):
        f.write_text(re.sub(r"expires: \S+", "expires: 2020-01-01T00:00:00Z", f.read_text()))
    (repo / "gitops/platform/base/access-sbx-alice-bob.yaml").write_text(re.sub(r"expires: \S+", "expires: 2020-01-01T00:00:00Z", (repo / "gitops/platform/base/access-sbx-alice-bob.yaml").read_text()))
    r = run("expire")
    assert not list((repo / "gitops/platform/base").glob("sandbox-alice.yaml")) and not list((repo / "gitops/platform/base").glob("access-*"))
    assert (repo / "gitops/platform/base/sandbox.yaml").exists() and len(r["files"]) == 2


def test_drill(repo):
    for s in ss.SCENARIOS:
        r = run("drill", scenario=s, minutes=30)
        assert (repo / f"gitops/fault-drills/active/{s}.yaml").exists()
    with pytest.raises(ss.Refused):
        run("drill", scenario="crashloop", minutes=30)
    with pytest.raises(ss.Refused):
        run("drill", scenario="../../etc/passwd", minutes=30)
    assert r["files"]


def test_main_prints_json_and_refuses(repo, capsys):
    ss.main(["x", "scale", json.dumps({"service": "orders-api", "replicas": 4})])
    out = json.loads(capsys.readouterr().out)
    assert out["branch"].startswith("selfservice/scale-") and out["files"] == ["gitops/workloads/orders-api/deployment.yaml"]
    with pytest.raises(SystemExit) as e:
        ss.main(["x", "scale", json.dumps({"service": "orders-api", "replicas": "many"})])
    assert e.value.code == 2
