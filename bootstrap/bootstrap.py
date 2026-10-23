#!/usr/bin/env python3
"""Create the local Kubernetes cluster (kind), install Calico and Argo CD, then hand over to GitOps.

This is the only imperative step. Everything after it, including the ingress
controller (Traefik), is synced from Git.

    python3 bootstrap/bootstrap.py            # create cluster, seed secrets, install Calico and Argo CD
    python3 bootstrap/bootstrap.py --destroy  # delete the cluster

Secrets: nothing secret is stored in Git. The bootstrap creates the few secrets the platform needs
with random values (Grafana admin login, the fault injection token). Set ANTHROPIC_API_KEY in the
environment to also create the key the SRE agent uses for LLM analysis; without it the agent runs
its rule based analysis. In AWS/Azure this step is replaced by External Secrets.
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
CLUSTER = "platform"
CONTEXT = f"kind-{CLUSTER}"
ARGOCD_CHART_VERSION = "7.6.12"
ARGOCD_NS = "argocd"
CALICO_MANIFEST = "https://raw.githubusercontent.com/projectcalico/calico/v3.28.2/manifests/calico.yaml"
# namespaces that need secrets before Argo CD creates them (Argo adopts them and adds labels)
SEED_NAMESPACES = ["observability", "demo", "sre-agent"]
HOST_PORTS = [8080, 8444]  # see bootstrap/kind-config.yaml


def run(*cmd, check=True, capture=False, stdin=None):
    hide = ("password", "token", "api_key")
    print("+", " ".join("[hidden]" if any(h in c.lower() for h in hide) else c for c in cmd), flush=True)
    return subprocess.run(
        cmd, check=check, text=True, input=stdin,
        stdout=subprocess.PIPE if capture else None,
    )


def kubectl(*args, **kw):
    return run("kubectl", "--context", CONTEXT, *args, **kw)


def preflight():
    missing = [t for t in ("docker", "kind", "kubectl", "helm") if not shutil.which(t)]
    if missing:
        sys.exit(f"missing on PATH: {', '.join(missing)}")
    if run("docker", "info", check=False, capture=True).returncode != 0:
        sys.exit("docker daemon is not reachable")


def check_ports():
    """kind publishes these host ports; fail early with a clear message if something else holds them."""
    busy = []
    for port in HOST_PORTS:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                busy.append(port)
    if busy:
        sys.exit(f"host port(s) {busy} already in use (a port-forward or another cluster?), free them or change bootstrap/kind-config.yaml")


def cluster_exists():
    out = run("kind", "get", "clusters", capture=True).stdout
    return CLUSTER in out.split()


def create_cluster():
    if cluster_exists():
        print(f"cluster {CLUSTER} already exists, leaving it alone")
        return
    run("kind", "create", "cluster", "--config", str(ROOT / "bootstrap" / "kind-config.yaml"))
    # kind has no CNI here (see kind-config.yaml); Calico provides pod networking and NetworkPolicy
    kubectl("apply", "-f", CALICO_MANIFEST)
    kubectl("wait", "--for=condition=Ready", "nodes", "--all", "--timeout=300s")


def install_argocd():
    run("helm", "repo", "add", "argo", "https://argoproj.github.io/argo-helm", "--force-update")
    run(
        "helm", "--kube-context", CONTEXT, "upgrade", "--install", "argocd", "argo/argo-cd",
        "--namespace", ARGOCD_NS, "--create-namespace",
        "--version", ARGOCD_CHART_VERSION,
        "-f", str(ROOT / "gitops" / "argocd" / "values.yaml"),
        "--wait", "--timeout", "10m",
    )


def secret_exists(ns, name):
    return kubectl("-n", ns, "get", "secret", name, check=False, capture=True).returncode == 0


def read_secret(ns, name, key):
    out = kubectl("-n", ns, "get", "secret", name, "-o", f"jsonpath={{.data.{key}}}", capture=True, check=False).stdout
    return base64.b64decode(out).decode() if out else ""


def create_secret(ns, name, **literals):
    args = ["-n", ns, "create", "secret", "generic", name]
    args += [f"--from-literal={k}={v}" for k, v in literals.items()]
    kubectl(*args)


def seed_secrets():
    for ns in SEED_NAMESPACES:
        manifest = kubectl("create", "namespace", ns, "--dry-run=client", "-o", "yaml", capture=True).stdout
        kubectl("apply", "-f", "-", stdin=manifest)

    if not secret_exists("observability", "grafana-admin"):
        create_secret("observability", "grafana-admin", **{"admin-user": "admin", "admin-password": secrets.token_urlsafe(18)})
    if not secret_exists("demo", "orders-api-chaos"):
        create_secret("demo", "orders-api-chaos", CHAOS_TOKEN=secrets.token_urlsafe(18))

    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        kubectl("-n", "sre-agent", "delete", "secret", "anthropic-api-key", "--ignore-not-found")
        create_secret("sre-agent", "anthropic-api-key", ANTHROPIC_API_KEY=key)
        print("anthropic-api-key created: the SRE agent will use LLM analysis")
    else:
        print("ANTHROPIC_API_KEY not set: the SRE agent will use rule based analysis")


def apply_root_app():
    kubectl("apply", "-f", str(ROOT / "gitops" / "root" / "root-app.yaml"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--destroy", action="store_true")
    args = ap.parse_args()

    preflight()
    if args.destroy:
        run("kind", "delete", "cluster", "--name", CLUSTER)
        return

    if not cluster_exists():
        check_ports()
    create_cluster()
    install_argocd()
    seed_secrets()
    apply_root_app()

    argocd_pw = read_secret(ARGOCD_NS, "argocd-initial-admin-secret", "password") or "(initial secret already removed)"
    print("\nEverything else is now deployed by Argo CD from Git. The first sync takes about 10 minutes.")
    print("URLs work once Traefik has synced.\n")
    print("Argo CD   http://argocd.localhost:8080    admin / " + argocd_pw)
    print("Grafana   http://grafana.localhost:8080   admin / " + read_secret("observability", "grafana-admin", "admin-password"))
    print("Temporal  http://temporal.localhost:8080")
    print("Orders    http://orders.localhost:8080/orders")
    print("\nWatch it come up:  kubectl --context", CONTEXT, "-n argocd get applications")


if __name__ == "__main__":
    main()
