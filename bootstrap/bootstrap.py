#!/usr/bin/env python3
"""Create the local Kubernetes cluster (kind), install Calico and Argo CD.

This is the only imperative step. Everything after it, including the ingress
controller (Traefik), is synced from Git.

    python3 bootstrap/bootstrap.py            # create cluster, install Calico and Argo CD
    python3 bootstrap/bootstrap.py --destroy  # delete the cluster
"""
import argparse
import base64
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLUSTER = "platform"
ARGOCD_CHART_VERSION = "7.6.12"
ARGOCD_NS = "argocd"
CALICO_MANIFEST = "https://raw.githubusercontent.com/projectcalico/calico/v3.28.2/manifests/calico.yaml"


def run(*cmd, check=True, capture=False, stdin=None):
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(
        cmd, check=check, text=True, input=stdin,
        stdout=subprocess.PIPE if capture else None,
    )


def preflight():
    missing = [t for t in ("docker", "kind", "kubectl", "helm") if not shutil.which(t)]
    if missing:
        sys.exit(f"missing on PATH: {', '.join(missing)}")
    if run("docker", "info", check=False, capture=True).returncode != 0:
        sys.exit("docker daemon is not reachable")


def cluster_exists():
    out = run("kind", "get", "clusters", capture=True).stdout
    return CLUSTER in out.split()


def create_cluster():
    if cluster_exists():
        print(f"cluster {CLUSTER} already exists, leaving it alone")
        return
    run("kind", "create", "cluster", "--config", str(ROOT / "bootstrap" / "kind-config.yaml"))
    # kind has no CNI here (see kind-config.yaml); Calico provides pod networking and NetworkPolicy
    run("kubectl", "apply", "-f", CALICO_MANIFEST)
    run("kubectl", "wait", "--for=condition=Ready", "nodes", "--all", "--timeout=300s")


def install_argocd():
    run("helm", "repo", "add", "argo", "https://argoproj.github.io/argo-helm", "--force-update")
    run(
        "helm", "upgrade", "--install", "argocd", "argo/argo-cd",
        "--namespace", ARGOCD_NS, "--create-namespace",
        "--version", ARGOCD_CHART_VERSION,
        "-f", str(ROOT / "gitops" / "argocd" / "values.yaml"),
        "--wait", "--timeout", "10m",
    )


def apply_root_app():
    run("kubectl", "apply", "-f", str(ROOT / "gitops" / "root" / "root-app.yaml"))


def initial_password():
    out = run(
        "kubectl", "-n", ARGOCD_NS, "get", "secret", "argocd-initial-admin-secret",
        "-o", "jsonpath={.data.password}", capture=True, check=False,
    ).stdout
    return base64.b64decode(out).decode() if out else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--destroy", action="store_true")
    args = ap.parse_args()

    preflight()
    if args.destroy:
        run("kind", "delete", "cluster", "--name", CLUSTER)
        return

    create_cluster()
    install_argocd()
    apply_root_app()

    print("\nArgo CD:  http://argocd.localhost:8080  (available once Traefik has synced)")
    print("user:     admin")
    print("password:", initial_password() or "(initial secret already removed)")


if __name__ == "__main__":
    main()
