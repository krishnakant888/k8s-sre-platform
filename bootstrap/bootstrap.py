#!/usr/bin/env python3
"""Create the three local Kubernetes clusters (kind), install Calico, and hand over to GitOps.

    platform    the production side: ingress, policy, workloads, and the collectors that ship telemetry out.
    observability  everything used to observe and debug production: Mimir, Loki, Tempo, Grafana and alerting, plus the
                SRE agent, Argo Workflows and the RCA report viewer.
    gitops      Argo CD, alone. It deploys the other two clusters and itself from this repo.

Each cluster is kept apart so the tools keep working when another one is down or being rebuilt. The platform and
observability clusters have no Argo CD of their own: this script registers them in the one in the gitops cluster, which then
deploys everything from Git. This is the only imperative step.

    python3 bootstrap/bootstrap.py                         # create all three clusters (platform, observability, gitops)
    python3 bootstrap/bootstrap.py --only platform         # create (or repair) one, and re-register it if gitops exists
    python3 bootstrap/bootstrap.py --destroy --only platform   # delete one, the others keep running
    python3 bootstrap/bootstrap.py --destroy               # delete all

Secrets: nothing secret is stored in Git. The bootstrap creates the few secrets each cluster needs with random
values (Grafana admin login in observability, the fault injection token in platform), and the read-only tokens the SRE
agent uses to read the other clusters. Set ANTHROPIC_API_KEY in the environment to also create the key the SRE agent
uses for LLM analysis; without it the agent runs its rule based analysis. In AWS/Azure this step is replaced by
External Secrets.
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
HUB = "gitops"  # the cluster that runs Argo CD
SPOKES = ["platform", "observability"]  # registered in the hub under these names, Applications target them with destination.name
AGENT = "observability"  # the cluster the SRE agent runs in; it gets read-only tokens for the others

# Order matters: the spokes' API servers have to exist before the hub's Argo CD can register them.
CLUSTERS = {
    "platform": {
        "kind_config": ROOT / "bootstrap" / "kind-config.yaml",
        "host_ports": [8080, 8443],
        # namespaces that need secrets before Argo CD creates them (Argo adopts them and adds labels)
        "seed_namespaces": ["demo"],
    },
    "observability": {
        "kind_config": ROOT / "bootstrap" / "kind-config-observability.yaml",
        "host_ports": [9080, 9443],
        "seed_namespaces": ["observability", "sre-agent"],
    },
    "gitops": {
        "kind_config": ROOT / "bootstrap" / "kind-config-gitops.yaml",
        "argocd_values": ROOT / "gitops" / "hub" / "argocd" / "values.yaml",
        "host_ports": [7080, 7443],
        "seed_namespaces": [],
    },
}
ROOT_APPS = {
    "gitops": ROOT / "gitops" / "root" / "root-app-hub.yaml",              # Argo CD itself and its ingress
    "observability": ROOT / "gitops" / "root" / "root-app-observability.yaml",   # the observability cluster's apps
    "platform": ROOT / "gitops" / "root" / "root-app-platform.yaml",       # the platform cluster's apps
}

# what Argo CD needs in a spoke: the same rights `argocd cluster add` grants. The second part is what the SRE agent
# may read there: pods, events and workloads, no secrets, no writes.
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
---
apiVersion: v1
kind: ServiceAccount
metadata: {name: sre-agent-reader, namespace: kube-system}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata: {name: sre-agent-read}
rules:
  - {apiGroups: [""], resources: [pods, events, nodes], verbs: [get, list]}
  - {apiGroups: [apps], resources: [deployments, statefulsets, daemonsets, replicasets], verbs: [get, list]}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata: {name: sre-agent-read}
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: ClusterRole, name: sre-agent-read}
subjects: [{kind: ServiceAccount, name: sre-agent-reader, namespace: kube-system}]
---
apiVersion: v1
kind: Secret
metadata:
  name: sre-agent-reader-token
  namespace: kube-system
  annotations: {kubernetes.io/service-account.name: sre-agent-reader}
type: kubernetes.io/service-account-token
"""

# what the SRE agent may read in the hub: the Argo CD Applications (deploy history), nothing else
HUB_READER = """\
apiVersion: v1
kind: ServiceAccount
metadata: {name: sre-agent-reader, namespace: argocd}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: {name: sre-agent-read-applications, namespace: argocd}
rules:
  - {apiGroups: [argoproj.io], resources: [applications], verbs: [get, list]}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: {name: sre-agent-read-applications, namespace: argocd}
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: Role, name: sre-agent-read-applications}
subjects: [{kind: ServiceAccount, name: sre-agent-reader, namespace: argocd}]
---
apiVersion: v1
kind: Secret
metadata:
  name: sre-agent-reader-token
  namespace: argocd
  annotations: {kubernetes.io/service-account.name: sre-agent-reader}
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


def read_secret_raw(cluster, ns, name, key):
    """Like read_secret but returns the base64 text untouched (the CA is passed to Argo CD base64 encoded)."""
    return kubectl(cluster, "-n", ns, "get", "secret", name, "-o", f"jsonpath={{.data.{key}}}", capture=True, check=False).stdout


def create_secret(cluster, ns, name, **literals):
    args = ["-n", ns, "create", "secret", "generic", name]
    args += [f"--from-literal={k}={v}" for k, v in literals.items()]
    kubectl(cluster, *args)


def ensure_namespace(cluster, ns):
    manifest = kubectl(cluster, "create", "namespace", ns, "--dry-run=client", "-o", "yaml", capture=True).stdout
    kubectl(cluster, "apply", "-f", "-", stdin=manifest)


def seed_secrets(cluster):
    for ns in CLUSTERS[cluster]["seed_namespaces"]:
        ensure_namespace(cluster, ns)

    if cluster == "observability":
        if not secret_exists(cluster, "observability", "grafana-admin"):
            create_secret(cluster, "observability", "grafana-admin", **{"admin-user": "admin", "admin-password": secrets.token_urlsafe(18)})
        key = os.environ.get("ANTHROPIC_API_KEY", "")  # the SRE agent runs in this cluster
        if key:
            kubectl(cluster, "-n", "sre-agent", "delete", "secret", "anthropic-api-key", "--ignore-not-found")
            create_secret(cluster, "sre-agent", "anthropic-api-key", ANTHROPIC_API_KEY=key)
            print("anthropic-api-key created: the SRE agent will use LLM analysis")
        else:
            print("ANTHROPIC_API_KEY not set: the SRE agent will use rule based analysis")
    elif cluster == "platform":
        if not secret_exists(cluster, "demo", "orders-api-chaos"):
            create_secret(cluster, "demo", "orders-api-chaos", CHAOS_TOKEN=secrets.token_urlsafe(18))


def wait_token(cluster, ns, name):
    """The token controller fills a service-account token Secret a moment after it is created: returns (token, ca base64)."""
    for _ in range(30):
        token = read_secret(cluster, ns, name, "token")
        ca = read_secret_raw(cluster, ns, name, "ca\\.crt")
        if token and ca:
            return token, ca
        time.sleep(1)
    sys.exit(f"the token {name} was not created in the {cluster} cluster")


def register_spoke(spoke):
    """Gives the hub's Argo CD access to a cluster: a service account there, its token as a cluster Secret in the hub."""
    kubectl(spoke, "apply", "-f", "-", stdin=SPOKE_ACCESS)
    token, ca = wait_token(spoke, "kube-system", "argocd-manager-token")
    config = json.dumps({"bearerToken": token, "tlsClientConfig": {"insecure": False, "caData": ca, "serverName": f"{spoke}-control-plane"}})
    cluster_secret = json.dumps(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": f"cluster-{spoke}", "namespace": ARGOCD_NS, "labels": {"argocd.argoproj.io/secret-type": "cluster"}},
            "type": "Opaque",
            "stringData": {"name": spoke, "server": f"https://{spoke}-control-plane:6443", "config": config},
        }
    )
    kubectl(HUB, "apply", "-f", "-", stdin=cluster_secret)
    print(f"cluster '{spoke}' registered in the Argo CD of '{HUB}'")


def give_agent_access(target, source_cluster, source_ns, source_secret, secret_name):
    """Copies a read-only token (and its API CA) from a cluster into the sre-agent namespace of the cluster the agent runs in."""
    token, _ = wait_token(source_cluster, source_ns, source_secret)
    ca = read_secret(source_cluster, source_ns, source_secret, "ca\\.crt")
    ensure_namespace(target, "sre-agent")
    kubectl(target, "-n", "sre-agent", "delete", "secret", secret_name, "--ignore-not-found")
    create_secret(target, "sre-agent", secret_name, **{"token": token, "ca.crt": ca})
    print(f"{secret_name} created in the sre-agent namespace of the {target} cluster")


def wire(clusters_present):
    """Everything that needs more than one cluster: registration in the hub and the agent's read-only access."""
    for spoke in SPOKES:
        if spoke in clusters_present:
            register_spoke(spoke)
    if AGENT in clusters_present:
        if "platform" in clusters_present:
            give_agent_access(AGENT, "platform", "kube-system", "sre-agent-reader-token", "platform-reader")
        kubectl(HUB, "apply", "-f", "-", stdin=HUB_READER)
        give_agent_access(AGENT, HUB, ARGOCD_NS, "sre-agent-reader-token", "gitops-reader")
    for name in [HUB, *SPOKES]:
        if name in clusters_present:
            kubectl(HUB, "apply", "-f", str(ROOT_APPS[name]))


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
    print("\nEverything else is now deployed by the Argo CD of the gitops cluster, into the other two. The first sync takes about 10 minutes.")
    print("URLs work once ingress-nginx has synced.\n")
    if HUB in clusters:
        print("Argo CD         http://argocd.localhost:7080               admin / " + argocd_password())
    if "observability" in clusters:
        print("Grafana         http://grafana.localhost:9080              admin / " + read_secret("observability", "observability", "grafana-admin", "admin-password"))
        print("Workflows       http://workflows.localhost:9080")
        print("RCA reports     http://reports.localhost:9080")
    if "platform" in clusters:
        print("Orders          http://orders.localhost:8080/orders")
    print(f"\nWatch the deployments:  kubectl --context {context(HUB)} -n {ARGOCD_NS} get applications")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--destroy", action="store_true")
    ap.add_argument("--only", choices=list(CLUSTERS), help="act on one cluster instead of all three")
    args = ap.parse_args()

    clusters = [args.only] if args.only else list(CLUSTERS)

    preflight()
    if args.destroy:
        for c in clusters:  # the hub last: it is what deploys the others
            run("kind", "delete", "cluster", "--name", c)
        return

    for c in clusters:
        build(c)

    # the hub deploys the other clusters, so wire them together once they exist
    if cluster_exists(HUB):
        wire({c for c in CLUSTERS if cluster_exists(c)})
    summary(clusters)


if __name__ == "__main__":
    main()
