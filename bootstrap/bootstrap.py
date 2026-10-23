#!/usr/bin/env python3
"""Create the two local Kubernetes clusters (kind), install Calico and Argo CD in each, then hand over to GitOps.

    monitoring  Mimir, Loki, Tempo, Grafana, alerting. Created first and kept apart from production, so it stays
                up when the production cluster is down or rebuilt.
    platform    ingress, policy, workloads, the SRE agent, and the collectors that ship telemetry to monitoring.

This is the only imperative step. Everything after it, including the ingress controllers, is synced from Git
by the Argo CD of each cluster.

    python3 bootstrap/bootstrap.py                         # create both clusters
    python3 bootstrap/bootstrap.py --only platform         # create (or repair) just one
    python3 bootstrap/bootstrap.py --destroy --only platform   # delete one, monitoring keeps running
    python3 bootstrap/bootstrap.py --destroy               # delete both

Secrets: nothing secret is stored in Git. The bootstrap creates the few secrets each cluster needs with random
values (Grafana admin login in monitoring, the fault injection token in platform). Set ANTHROPIC_API_KEY in the
environment to also create the key the SRE agent uses for LLM analysis; without it the agent runs its rule based
analysis. In AWS/Azure this step is replaced by External Secrets.
"""
import argparse
import base64
import os
import secrets
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARGOCD_CHART_VERSION = "7.6.12"
ARGOCD_NS = "argocd"
CALICO_MANIFEST = "https://raw.githubusercontent.com/projectcalico/calico/v3.28.2/manifests/calico.yaml"

# Order matters: monitoring first, so it is already receiving when the platform cluster starts shipping.
CLUSTERS = {
    "monitoring": {
        "kind_config": ROOT / "bootstrap" / "kind-config-monitoring.yaml",
        "argocd_values": ROOT / "gitops" / "monitoring" / "argocd" / "values.yaml",
        "root_app": ROOT / "gitops" / "root" / "root-app-monitoring.yaml",
        "host_ports": [9080, 9443],  # see the kind config
        # namespaces that need secrets before Argo CD creates them (Argo adopts them and adds labels)
        "seed_namespaces": ["observability"],
    },
    "platform": {
        "kind_config": ROOT / "bootstrap" / "kind-config.yaml",
        "argocd_values": ROOT / "gitops" / "argocd" / "values.yaml",
        "root_app": ROOT / "gitops" / "root" / "root-app.yaml",
        "host_ports": [8080, 8443],
        "seed_namespaces": ["demo", "sre-agent"],
    },
}


def run(*cmd, check=True, capture=False, stdin=None):
    hide = ("password", "token", "api_key")
    print("+", " ".join("[hidden]" if any(h in c.lower() for h in hide) else c for c in cmd), flush=True)
    return subprocess.run(
        cmd, check=check, text=True, input=stdin,
        stdout=subprocess.PIPE if capture else None,
    )


def context(cluster):
    return f"kind-{cluster}"


def kubectl(cluster, *args, **kw):
    return run("kubectl", "--context", context(cluster), *args, **kw)


def preflight():
    missing = [t for t in ("docker", "kind", "kubectl", "helm") if not shutil.which(t)]
    if missing:
        sys.exit(f"missing on PATH: {', '.join(missing)}")
    if run("docker", "info", check=False, capture=True).returncode != 0:
        sys.exit("docker daemon is not reachable")


def check_ports(cluster):
    """kind publishes these host ports; fail early with a clear message if something else holds them."""
    busy = []
    for port in CLUSTERS[cluster]["host_ports"]:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                busy.append(port)
    if busy:
        sys.exit(f"host port(s) {busy} already in use (a port-forward or another cluster?), free them or change {CLUSTERS[cluster]['kind_config'].name}")


def cluster_exists(cluster):
    out = run("kind", "get", "clusters", capture=True).stdout
    return cluster in out.split()


def create_cluster(cluster):
    if cluster_exists(cluster):
        print(f"cluster {cluster} already exists, leaving it alone")
        return
    run("kind", "create", "cluster", "--config", str(CLUSTERS[cluster]["kind_config"]))
    # kind has no CNI here (see the kind configs); Calico provides pod networking and NetworkPolicy
    kubectl(cluster, "apply", "-f", CALICO_MANIFEST)
    kubectl(cluster, "wait", "--for=condition=Ready", "nodes", "--all", "--timeout=300s")


def install_argocd(cluster):
    run("helm", "repo", "add", "argo", "https://argoproj.github.io/argo-helm", "--force-update")
    run(
        "helm", "--kube-context", context(cluster), "upgrade", "--install", "argocd", "argo/argo-cd",
        "--namespace", ARGOCD_NS, "--create-namespace",
        "--version", ARGOCD_CHART_VERSION,
        "-f", str(CLUSTERS[cluster]["argocd_values"]),
        "--wait", "--timeout", "10m",
    )


def secret_exists(cluster, ns, name):
    return kubectl(cluster, "-n", ns, "get", "secret", name, check=False, capture=True).returncode == 0


def read_secret(cluster, ns, name, key):
    out = kubectl(cluster, "-n", ns, "get", "secret", name, "-o", f"jsonpath={{.data.{key}}}", capture=True, check=False).stdout
    return base64.b64decode(out).decode() if out else ""


def create_secret(cluster, ns, name, **literals):
    args = ["-n", ns, "create", "secret", "generic", name]
    args += [f"--from-literal={k}={v}" for k, v in literals.items()]
    kubectl(cluster, *args)


def seed_secrets(cluster):
    for ns in CLUSTERS[cluster]["seed_namespaces"]:
        manifest = kubectl(cluster, "create", "namespace", ns, "--dry-run=client", "-o", "yaml", capture=True).stdout
        kubectl(cluster, "apply", "-f", "-", stdin=manifest)

    if cluster == "monitoring":
        if not secret_exists(cluster, "observability", "grafana-admin"):
            create_secret(cluster, "observability", "grafana-admin", **{"admin-user": "admin", "admin-password": secrets.token_urlsafe(18)})
        return

    if not secret_exists(cluster, "demo", "orders-api-chaos"):
        create_secret(cluster, "demo", "orders-api-chaos", CHAOS_TOKEN=secrets.token_urlsafe(18))

    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        kubectl(cluster, "-n", "sre-agent", "delete", "secret", "anthropic-api-key", "--ignore-not-found")
        create_secret(cluster, "sre-agent", "anthropic-api-key", ANTHROPIC_API_KEY=key)
        print("anthropic-api-key created: the SRE agent will use LLM analysis")
    else:
        print("ANTHROPIC_API_KEY not set: the SRE agent will use rule based analysis")


def apply_root_app(cluster):
    kubectl(cluster, "apply", "-f", str(CLUSTERS[cluster]["root_app"]))


def build(cluster):
    if not cluster_exists(cluster):
        check_ports(cluster)
    create_cluster(cluster)
    install_argocd(cluster)
    seed_secrets(cluster)
    apply_root_app(cluster)


def argocd_password(cluster):
    return read_secret(cluster, ARGOCD_NS, "argocd-initial-admin-secret", "password") or "(initial secret already removed)"


def summary(clusters):
    print("\nEverything else is now deployed by Argo CD from Git. The first sync takes about 10 minutes.")
    print("URLs work once ingress-nginx has synced.\n")
    if "monitoring" in clusters:
        print("Grafana         http://grafana.localhost:9080              admin / " + read_secret("monitoring", "observability", "grafana-admin", "admin-password"))
        print("Argo CD (mon.)  http://argocd-monitoring.localhost:9080    admin / " + argocd_password("monitoring"))
    if "platform" in clusters:
        print("Argo CD (prod)  http://argocd.localhost:8080               admin / " + argocd_password("platform"))
        print("Workflows       http://workflows.localhost:8080")
        print("Orders          http://orders.localhost:8080/orders")
    print()
    for c in clusters:
        print(f"Watch {c}:  kubectl --context {context(c)} -n argocd get applications")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--destroy", action="store_true")
    ap.add_argument("--only", choices=list(CLUSTERS), help="act on one cluster instead of both")
    args = ap.parse_args()

    clusters = [args.only] if args.only else list(CLUSTERS)

    preflight()
    if args.destroy:
        for c in reversed(clusters):  # platform first, monitoring last
            run("kind", "delete", "cluster", "--name", c)
        return

    for c in clusters:
        build(c)
    summary(clusters)


if __name__ == "__main__":
    main()
