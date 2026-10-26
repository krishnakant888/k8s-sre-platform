#!/usr/bin/env python3
"""Create the two local Kubernetes clusters (kind), install Calico, and hand over to GitOps.

    platform    the production side: ingress, policy, workloads, and the collectors that ship telemetry out.
    monitoring  everything used to operate and debug production: the single Argo CD that deploys BOTH clusters,
                Mimir, Loki, Tempo, Grafana and alerting. It is kept apart so it keeps working when production is
                down or being rebuilt.

Argo CD runs only in the monitoring cluster (hub and spoke). The platform cluster has no Argo CD of its own:
this script registers it in the monitoring Argo CD, which then deploys everything from Git into both clusters.
This is the only imperative step.

    python3 bootstrap/bootstrap.py                         # create both clusters (platform first)
    python3 bootstrap/bootstrap.py --only platform         # create (or repair) one, and re-register it if monitoring exists
    python3 bootstrap/bootstrap.py --destroy --only platform   # delete one, monitoring keeps running
    python3 bootstrap/bootstrap.py --destroy               # delete both

Secrets: nothing secret is stored in Git. The bootstrap creates the few secrets each cluster needs with random
values (Grafana admin login in monitoring, the fault injection token in platform). Set ANTHROPIC_API_KEY in the
environment to also create the key the SRE agent uses for LLM analysis; without it the agent runs its rule based
analysis. In AWS/Azure this step is replaced by External Secrets.
"""
import argparse
import base64
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARGOCD_CHART_VERSION = "7.6.12"
ARGOCD_NS = "argocd"
CALICO_MANIFEST = "https://raw.githubusercontent.com/projectcalico/calico/v3.28.2/manifests/calico.yaml"
HUB = "monitoring"  # the cluster that runs Argo CD
SPOKE = "platform"  # registered in the hub under this name, Applications target it with destination.name
# how the hub reaches the spoke: container name on the shared kind Docker network
SPOKE_API = "https://platform-control-plane:6443"

# Order matters: the platform API has to exist before the hub's Argo CD can register it.
CLUSTERS = {
    "platform": {
        "kind_config": ROOT / "bootstrap" / "kind-config.yaml",
        "host_ports": [8080, 8443],
        # namespaces that need secrets before Argo CD creates them (Argo adopts them and adds labels)
        "seed_namespaces": ["demo", "sre-agent"],
    },
    "monitoring": {
        "kind_config": ROOT / "bootstrap" / "kind-config-monitoring.yaml",
        "argocd_values": ROOT / "gitops" / "monitoring" / "argocd" / "values.yaml",
        "host_ports": [9080, 9443],
        "seed_namespaces": ["observability"],
    },
}
ROOT_APPS = {
    "monitoring": ROOT / "gitops" / "root" / "root-app-monitoring.yaml",  # the monitoring cluster's own apps
    "platform": ROOT / "gitops" / "root" / "root-app-platform.yaml",      # the platform cluster's apps, deployed by the hub
}

# what Argo CD needs in the spoke: the same rights `argocd cluster add` grants
SPOKE_ACCESS = """\
apiVersion: v1
kind: ServiceAccount
metadata: {name: argocd-manager, namespace: kube-system}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata: {name: argocd-manager-role}
rules:
  - {apiGroups: ["*"], resources: ["*"], verbs: ["*"]}
  - {nonResourceURLs: ["*"], verbs: ["*"]}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata: {name: argocd-manager-role-binding}
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: ClusterRole, name: argocd-manager-role}
subjects: [{kind: ServiceAccount, name: argocd-manager, namespace: kube-system}]
---
apiVersion: v1
kind: Secret
metadata:
  name: argocd-manager-token
  namespace: kube-system
  annotations: {kubernetes.io/service-account.name: argocd-manager}
type: kubernetes.io/service-account-token
"""


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


def register_spoke():
    """Gives the hub's Argo CD access to the platform cluster: a service account there, its token as a cluster Secret here."""
    kubectl(SPOKE, "apply", "-f", "-", stdin=SPOKE_ACCESS)
    token = ca = ""
    for _ in range(30):  # the token controller fills the Secret a moment after it is created
        token = read_secret(SPOKE, "kube-system", "argocd-manager-token", "token")
        ca = read_secret_raw(SPOKE, "kube-system", "argocd-manager-token", "ca\\.crt")
        if token and ca:
            break
        time.sleep(1)
    if not (token and ca):
        sys.exit("the argocd-manager token was not created in the platform cluster")
    config = json.dumps({"bearerToken": token, "tlsClientConfig": {"insecure": False, "caData": ca, "serverName": "platform-control-plane"}})
    cluster_secret = json.dumps(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": f"cluster-{SPOKE}", "namespace": ARGOCD_NS, "labels": {"argocd.argoproj.io/secret-type": "cluster"}},
            "type": "Opaque",
            "stringData": {"name": SPOKE, "server": SPOKE_API, "config": config},
        }
    )
    kubectl(HUB, "apply", "-f", "-", stdin=cluster_secret)
    print(f"cluster '{SPOKE}' registered in the Argo CD of '{HUB}'")


def read_secret_raw(cluster, ns, name, key):
    """Like read_secret but returns the base64 text untouched (the CA is passed to Argo CD base64 encoded)."""
    return kubectl(cluster, "-n", ns, "get", "secret", name, "-o", f"jsonpath={{.data.{key}}}", capture=True, check=False).stdout


def apply_root(app):
    kubectl(HUB, "apply", "-f", str(ROOT_APPS[app]))


def build(cluster):
    if not cluster_exists(cluster):
        check_ports(cluster)
    create_cluster(cluster)
    if cluster == HUB:
        install_argocd(cluster)
    seed_secrets(cluster)


def argocd_password():
    return read_secret(HUB, ARGOCD_NS, "argocd-initial-admin-secret", "password") or "(initial secret already removed)"


def summary(clusters):
    print("\nEverything else is now deployed by the Argo CD of the monitoring cluster, into both clusters. The first sync takes about 10 minutes.")
    print("URLs work once ingress-nginx has synced.\n")
    if HUB in clusters:
        print("Argo CD         http://argocd-monitoring.localhost:9080    admin / " + argocd_password())
        print("Grafana         http://grafana.localhost:9080              admin / " + read_secret(HUB, "observability", "grafana-admin", "admin-password"))
    if "platform" in clusters:
        print("Workflows       http://workflows.localhost:8080")
        print("Orders          http://orders.localhost:8080/orders")
    print(f"\nWatch the deployments:  kubectl --context {context(HUB)} -n {ARGOCD_NS} get applications")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--destroy", action="store_true")
    ap.add_argument("--only", choices=list(CLUSTERS), help="act on one cluster instead of both")
    args = ap.parse_args()

    clusters = [args.only] if args.only else list(CLUSTERS)

    preflight()
    if args.destroy:
        for c in clusters:  # platform first, monitoring last
            run("kind", "delete", "cluster", "--name", c)
        return

    for c in clusters:
        build(c)

    # the hub deploys both clusters, so wire them together once both exist
    if cluster_exists(HUB):
        if cluster_exists(SPOKE):
            register_spoke()
        apply_root("monitoring")
        if cluster_exists(SPOKE):
            apply_root("platform")
    summary(clusters)


if __name__ == "__main__":
    main()
