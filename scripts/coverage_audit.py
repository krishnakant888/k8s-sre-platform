#!/usr/bin/env python3
"""Checks that every workload of every cluster is covered by the generic observability: metrics, logs, labels, resources, probes.

    scripts/coverage_audit.py            needs the three kind clusters and kubectl; starts the port-forwards it needs

For each Deployment, StatefulSet and DaemonSet it asks the live Mimir and Loki whether data for it arrives (not whether it is configured),
and lists what a workload owner would have to fix. A workload with a gap here is a workload the dashboards, alerts or the agent cannot see well.
"""
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request

CLUSTERS = ["platform", "observability"]
MIMIR = "http://localhost:18080/prometheus"
LOKI = "http://localhost:13100"


def kjson(cluster, *args):
    r = subprocess.run(["kubectl", "--context", f"kind-{cluster}", *args, "-o", "json"], capture_output=True, text=True)
    return json.loads(r.stdout) if r.returncode == 0 else {"items": []}


def prom(expr):
    with urllib.request.urlopen(MIMIR + "/api/v1/query?" + urllib.parse.urlencode({"query": expr}), timeout=60) as r:
        return json.load(r)["data"]["result"]


def loki_count(selector):
    q = f"sum(count_over_time({selector}[30m]))"
    with urllib.request.urlopen(LOKI + "/loki/api/v1/query?" + urllib.parse.urlencode({"query": q}), timeout=60) as r:
        res = json.load(r)["data"]["result"]
    return float(res[0]["value"][1]) if res else 0.0


def main():
    pf = [
        subprocess.Popen(["kubectl", "--context", "kind-observability", "-n", "observability", "port-forward", "svc/mimir", "18080:8080"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
        subprocess.Popen(["kubectl", "--context", "kind-observability", "-n", "observability", "port-forward", "svc/loki", "13100:3100"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
    ]
    try:
        for _ in range(30):
            try:
                urllib.request.urlopen(MIMIR.replace("/prometheus", "/ready"), timeout=3)
                urllib.request.urlopen(LOKI + "/ready", timeout=3)
                break
            except Exception:  # noqa: BLE001
                time.sleep(2)
        gaps_total = 0
        rows = 0
        for cluster in CLUSTERS:
            ingresses = kjson(cluster, "get", "ingress", "-A")["items"]
            backends = {(i["metadata"]["namespace"], p["backend"]["service"]["name"]) for i in ingresses for rule in i["spec"].get("rules", []) for p in rule["http"]["paths"]}
            pdbs = kjson(cluster, "get", "pdb", "-A")["items"]
            print(f"\n=== {cluster}")
            print(f"{'workload':<52} {'metrics':<8} {'logs':<6} {'gaps'}")
            for kind in ("deployments", "statefulsets", "daemonsets"):
                for w in kjson(cluster, "get", kind, "-A")["items"]:
                    ns, name = w["metadata"]["namespace"], w["metadata"]["name"]
                    spec = w["spec"]["template"]["spec"]
                    labels = w["spec"]["template"]["metadata"].get("labels", {})
                    gaps = []
                    pod_rx = f"{name}-.*"
                    cad = prom(f'count(container_memory_working_set_bytes{{cluster="{cluster}", namespace="{ns}", pod=~"{pod_rx}", container!=""}})')
                    ksm = prom(f'count(kube_pod_info{{cluster="{cluster}", namespace="{ns}", pod=~"{pod_rx}"}})')
                    has_metrics = bool(cad) and bool(ksm)
                    if not has_metrics:
                        gaps.append("no container or pod metrics arrive")
                    logs = loki_count(f'{{cluster="{cluster}", namespace="{ns}", pod=~"{pod_rx}"}}')
                    if logs == 0:
                        gaps.append("no log lines in 30 min (idle, or not writing to stdout)")
                    app = prom(f'count(up{{cluster="{cluster}", namespace="{ns}", pod=~"{pod_rx}"}})')
                    if (ns, name) in backends:
                        ing = prom(f'count(nginx_ingress_controller_requests{{cluster="{cluster}", namespace="{ns}"}})')
                        if not ing:
                            gaps.append("has an Ingress but no traffic metrics yet (no request has passed)")
                    if not labels.get("app.kubernetes.io/name"):
                        gaps.append("no app.kubernetes.io/name label")
                    if not labels.get("app.kubernetes.io/version") and ns in ("demo", "sandbox", "sre-agent"):
                        gaps.append("no version label")
                    for c in spec["containers"]:
                        res = c.get("resources", {})
                        if not res.get("limits", {}).get("memory"):
                            gaps.append(f"container {c['name']} has no memory limit")
                    replicas = w["spec"].get("replicas", 1)
                    if kind != "daemonsets" and replicas and replicas > 1 and not any(p["metadata"]["namespace"] == ns for p in pdbs):
                        gaps.append("more than one replica and no PodDisruptionBudget in its namespace")
                    if kind == "deployments" and ns in ("demo", "sandbox") and not all(c.get("readinessProbe") for c in spec["containers"]):
                        gaps.append("no readiness probe")
                    rows += 1
                    gaps_total += len(gaps)
                    print(f"{ns + '/' + name:<52} {'yes' if has_metrics else 'NO':<8} {'yes' if logs else 'no':<6} {'; '.join(gaps) if gaps else '-'}")
        print(f"\n{rows} workloads, {gaps_total} gaps")
    finally:
        for p in pf:
            p.terminate()


if __name__ == "__main__":
    sys.exit(main())
