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
import contextlib
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARGOCD_CHART_VERSION = "7.6.12"
ARGOCD_NS = "argocd"
CALICO_MANIFEST = "https://raw.githubusercontent.com/projectcalico/calico/v3.28.2/manifests/calico.yaml"
HUB = "gitops"  # the cluster that runs Argo CD
SPOKES = ["platform", "observability"]  # registered in the hub under these names, Applications target them with destination.name
AGENT = "observability"  # the cluster the SRE agent runs in; it gets read-only tokens for the others
# basic-auth login the gateway puts in front of the UIs that have none (Argo Workflows, the RCA report viewer). A stopgap
# for local use: production puts SSO (OIDC) in front of them. Mimir, Loki, Tempo and Alloy are not published at all.
GATEWAY_AUTH_SECRET = "gateway-basic-auth"  # what Traefik reads: it must hold exactly one key, `users` (htpasswd lines)
GATEWAY_LOGIN_SECRET = "gateway-login"  # the plain password, kept apart so --credentials can print it
GATEWAY_AUTH_NAMESPACES = ["argo-workflows", "sre-agent"]  # in the observability cluster; the login Secret is in the first

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
        "seed_namespaces": ["observability", "sre-agent", "argo-workflows"],
    },
    "gitops": {
        "kind_config": ROOT / "bootstrap" / "kind-config-gitops.yaml",
        "argocd_values": ROOT / "gitops" / "hub" / "argocd" / "values.yaml",
        "host_ports": [7080, 7443],
        "seed_namespaces": [],
    },
}
ROOT_APPS = {
    "gitops": ROOT / "gitops" / "root" / "root-app-gitops.yaml",              # Argo CD itself and its ingress
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

# what the developer portal (Backstage) may read in every cluster: workloads, pods and routes, no secrets, no writes
PORTAL_ACCESS = """\
apiVersion: v1
kind: ServiceAccount
metadata: {name: portal-reader, namespace: kube-system}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata: {name: portal-read}
rules:
  - {apiGroups: [""], resources: [pods, services, configmaps, limitranges, resourcequotas, replicationcontrollers, namespaces, nodes], verbs: [get, list, watch]}
  - {apiGroups: [apps], resources: [deployments, replicasets, statefulsets, daemonsets], verbs: [get, list, watch]}
  - {apiGroups: [autoscaling], resources: [horizontalpodautoscalers], verbs: [get, list, watch]}
  - {apiGroups: [batch], resources: [jobs, cronjobs], verbs: [get, list, watch]}
  - {apiGroups: [networking.k8s.io], resources: [ingresses], verbs: [get, list, watch]}
  - {apiGroups: [gateway.networking.k8s.io], resources: [gateways, httproutes], verbs: [get, list, watch]}
  - {apiGroups: [metrics.k8s.io], resources: [pods], verbs: [get, list]}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata: {name: portal-read}
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: ClusterRole, name: portal-read}
subjects: [{kind: ServiceAccount, name: portal-reader, namespace: kube-system}]
---
apiVersion: v1
kind: Secret
metadata:
  name: portal-reader-token
  namespace: kube-system
  annotations: {kubernetes.io/service-account.name: portal-reader}
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


def run(*cmd, check=True, capture=False, stdin=None, quiet=False):
    hide = ("password", "token", "api_key", "ca.crt")
    if not quiet:
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
        keep_running(cluster)
        size_system_components(cluster)
        return
    run("kind", "create", "cluster", "--config", str(CLUSTERS[cluster]["kind_config"]))
    keep_running(cluster)
    # kind has no CNI here (see the kind configs); Calico provides pod networking and NetworkPolicy
    kubectl(cluster, "apply", "-f", CALICO_MANIFEST)
    kubectl(cluster, "wait", "--for=condition=Ready", "nodes", "--all", "--timeout=300s")
    size_system_components(cluster)


# components that arrive with the cluster or the CNI manifest and carry no memory limit (or a small one)
SYSTEM_SIZING = [
    ("kube-system", "ds/calico-node", "calico-node", "cpu=100m,memory=128Mi", "memory=512Mi"),
    ("kube-system", "deploy/calico-kube-controllers", "calico-kube-controllers", "cpu=20m,memory=64Mi", "memory=256Mi"),
    ("kube-system", "ds/kube-proxy", "kube-proxy", "cpu=50m,memory=64Mi", "memory=256Mi"),
    ("kube-system", "deploy/coredns", "coredns", "cpu=100m,memory=70Mi", "memory=256Mi"),
    ("local-path-storage", "deploy/local-path-provisioner", "local-path-provisioner", "cpu=10m,memory=32Mi", "memory=128Mi"),
]


def size_system_components(cluster):
    for ns, target, container, requests, limits in SYSTEM_SIZING:
        kubectl(cluster, "-n", ns, "set", "resources", target, "-c", container, f"--requests={requests}", f"--limits={limits}", check=False)


def keep_running(cluster):
    """kind nodes are plain containers: when the Docker daemon restarts (a WSL2 restart, a reboot) they stay stopped and
    the cluster looks gone. A restart policy brings them back with the daemon, and the cluster recovers on its own."""
    nodes = run("docker", "ps", "-a", "--filter", f"label=io.x-k8s.kind.cluster={cluster}", "--format", "{{.Names}}", capture=True).stdout.split()
    if nodes:
        run("docker", "update", "--restart", "unless-stopped", *nodes, capture=True)


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
    out = kubectl(cluster, "-n", ns, "get", "secret", name, "-o", f"jsonpath={{.data.{key}}}", capture=True, check=False, quiet=True).stdout
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
        seed_gateway_login(cluster)
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


def seed_gateway_login(cluster):
    """One random password for the gateway login. Traefik reads the users file from a Secret that must hold exactly one
    key, so the plain password is kept in a second Secret. The users file holds a {SHA} htpasswd entry (the one format
    available without extra packages), fine for a random 24 character password. Safe to run again: the password is
    kept, and an older Secret that also held the password is rewritten."""
    login_ns = GATEWAY_AUTH_NAMESPACES[0]
    pw = read_secret(cluster, login_ns, GATEWAY_LOGIN_SECRET, "password")
    if not pw:
        pw = next((p for ns in GATEWAY_AUTH_NAMESPACES if (p := read_secret(cluster, ns, GATEWAY_AUTH_SECRET, "password"))), "") or secrets.token_urlsafe(18)
        kubectl(cluster, "-n", login_ns, "delete", "secret", GATEWAY_LOGIN_SECRET, "--ignore-not-found")
        create_secret(cluster, login_ns, GATEWAY_LOGIN_SECRET, password=pw)
    users = "admin:{SHA}" + base64.b64encode(hashlib.sha1(pw.encode()).digest()).decode()
    for ns in GATEWAY_AUTH_NAMESPACES:
        kubectl(cluster, "-n", ns, "delete", "secret", GATEWAY_AUTH_SECRET, "--ignore-not-found")
        create_secret(cluster, ns, GATEWAY_AUTH_SECRET, users=users)


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
    give_portal_access(clusters_present)   # early, so the portal can start; the Argo CD and Grafana tokens are filled in by wait_for_services


@contextlib.contextmanager
def port_forward(cluster, ns, svc, remote_port, local_port):
    """kubectl port-forward for the length of a with block (the API of Argo CD or Grafana, called from this machine)."""
    p = subprocess.Popen(["kubectl", "--context", context(cluster), "-n", ns, "port-forward", f"svc/{svc}", f"{local_port}:{remote_port}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(3)
        yield f"http://127.0.0.1:{local_port}"
    finally:
        p.terminate()


def http_json(url, body=None, user=None, token=None, method=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method or ("POST" if body is not None else "GET"))
    req.add_header("Content-Type", "application/json")
    if user:
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{user[0]}:{user[1]}".encode()).decode())
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def portal_argocd_token():
    """An API key of the read-only `portal` account, which gitops/hub/argocd/values.yaml declares (the default policy is read only)."""
    pw = argocd_password()
    with port_forward(HUB, ARGOCD_NS, "argocd-server", 80, 18081) as base:
        session = http_json(base + "/api/v1/session", {"username": "admin", "password": pw})["token"]
        return http_json(base + "/api/v1/account/portal/token", {"name": "portal"}, token=session)["token"]


def grafana_viewer_token(name):
    """A token of a Viewer service account of that name in Grafana. Needs Grafana, which Argo CD deploys later."""
    pw = read_secret("observability", "observability", "grafana-admin", "admin-password")
    with port_forward("observability", "observability", "grafana", 80, 18082) as base:
        admin = ("admin", pw)
        found = http_json(base + f"/api/serviceaccounts/search?query={name}", user=admin)["serviceAccounts"]
        sa = next((s for s in found if s["name"] == name), None) or http_json(base + "/api/serviceaccounts", {"name": name, "role": "Viewer"}, user=admin)
        return http_json(base + f"/api/serviceaccounts/{sa['id']}/tokens", {"name": f"{name}-{int(time.time())}"}, user=admin)["key"]


def portal_grafana_token():
    """The portal's alerts card."""
    return grafana_viewer_token("portal")


def give_viewer_access(present):
    """The RCA report viewer shows whether the alert of a report is still firing: its nginx asks Grafana for the state of the alert rules with
    the token of a Viewer service account, from the Secret rca-viewer-grafana (a one-line nginx include that sets $grafana_auth). Needs Grafana to be up, so run
    `bootstrap.py --viewer-access` once it is if the first bootstrap was too early. Without the Secret the viewer still works and shows "unknown"."""
    if AGENT not in present:
        return
    try:
        token = grafana_viewer_token("rca-viewer")
    except Exception as exc:  # noqa: BLE001
        print(f"viewer: no Grafana token yet ({type(exc).__name__}: Grafana is not ready); run `python3 bootstrap/bootstrap.py --viewer-access` once it is")
        return
    ensure_namespace(AGENT, "sre-agent")
    manifest = json.dumps({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "rca-viewer-grafana", "namespace": "sre-agent"}, "type": "Opaque",
                           "stringData": {"grafana-auth.conf": f'set $grafana_auth "Bearer {token}";\n'}})
    kubectl(AGENT, "apply", "-f", "-", stdin=manifest)
    print("rca-viewer-grafana written in the observability cluster (read-only Grafana token for the report viewer)")


def give_portal_access(present):
    """The developer portal (Backstage, in the hub) reads every cluster, Argo CD and Grafana with read-only tokens, which go
    into its Secret portal-secrets. Safe to run again: the database password and the backend secret are kept. The Grafana
    token needs Grafana to be up, so after the first sync run `bootstrap.py --portal-access` again if it was not ready."""
    if HUB not in present:
        return
    ensure_namespace(HUB, "portal")
    old = {k: read_secret(HUB, "portal", "portal-secrets", k) for k in ("POSTGRES_PASSWORD", "BACKEND_SECRET", "GRAFANA_TOKEN", "ARGOCD_TOKEN", "GITHUB_TOKEN")}
    data = {"POSTGRES_PASSWORD": old["POSTGRES_PASSWORD"] or secrets.token_urlsafe(18), "BACKEND_SECRET": old["BACKEND_SECRET"] or secrets.token_urlsafe(24)}
    if old["GITHUB_TOKEN"]:
        data["GITHUB_TOKEN"] = old["GITHUB_TOKEN"]   # added by hand for the self-service templates (docs/self-service.md): keep it
    for c in (HUB, *SPOKES):
        if c in present:
            kubectl(c, "apply", "-f", "-", stdin=PORTAL_ACCESS)
            data[f"K8S_TOKEN_{c.upper()}"] = wait_token(c, "kube-system", "portal-reader-token")[0]
        else:
            data[f"K8S_TOKEN_{c.upper()}"] = "not-created"
    # the RCA report viewer is behind the gateway's basic-auth login; the portal's backend sends it
    rca_pw = read_secret("observability", GATEWAY_AUTH_NAMESPACES[0], GATEWAY_LOGIN_SECRET, "password") if "observability" in present else ""
    data["RCA_BASIC_AUTH"] = "Basic " + base64.b64encode(f"admin:{rca_pw}".encode()).decode() if rca_pw else "not-created"
    for key, fn, where in (("ARGOCD_TOKEN", portal_argocd_token, "Argo CD"), ("GRAFANA_TOKEN", portal_grafana_token, "Grafana")):
        try:
            data[key] = fn()
        except Exception as exc:  # noqa: BLE001
            data[key] = old[key] or "not-created"
            print(f"portal: no {key} yet ({type(exc).__name__}: {where} is not ready); run `python3 bootstrap/bootstrap.py --portal-access` once it is")
    manifest = json.dumps({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "portal-secrets", "namespace": "portal"}, "type": "Opaque", "stringData": data})
    kubectl(HUB, "apply", "-f", "-", stdin=manifest)
    print("portal-secrets written in the gitops cluster (read-only tokens for the portal)")


def _available(cluster, ns, deploy):
    r = kubectl(cluster, "-n", ns, "get", "deploy", deploy, "-o", "jsonpath={.status.availableReplicas}", capture=True, check=False, quiet=True)
    return r.returncode == 0 and (r.stdout or "").strip() not in ("", "0")


def _portal_account_declared():
    r = kubectl(HUB, "-n", ARGOCD_NS, "get", "cm", "argocd-cm", "-o", "jsonpath={.data.accounts\\.portal}", capture=True, check=False, quiet=True)
    return r.returncode == 0 and "apiKey" in (r.stdout or "")


def _apps_progress():
    r = kubectl(HUB, "-n", ARGOCD_NS, "get", "applications", "-o", "json", capture=True, check=False, quiet=True)
    try:
        items = json.loads(r.stdout or "{}").get("items", [])
    except ValueError:
        return ""
    ok = sum(1 for a in items if a.get("status", {}).get("sync", {}).get("status") == "Synced" and a.get("status", {}).get("health", {}).get("status") == "Healthy")
    return f"{ok} of {len(items)} Argo CD applications Synced and Healthy" if items else ""


def wait_for_services(present, timeout_min=25):
    """Argo CD deploys Grafana, and applies the portal account of its own configuration, after the bootstrap itself has returned.
    Wait for them (printing where it stands), then create the tokens that need them: the portal's and the report viewer's.
    If it takes longer than the timeout, say which commands finish the job later."""
    waits = {}
    if "observability" in present:
        waits["Grafana"] = lambda: _available("observability", "observability", "grafana") and bool(read_secret("observability", "observability", "grafana-admin", "admin-password"))
    if HUB in present:
        waits["the Argo CD portal account"] = lambda: _available(HUB, ARGOCD_NS, "argocd-server") and _portal_account_declared()
    start = time.time()
    print(f"\nWaiting for Argo CD to deploy {' and '.join(waits)} (usually 10 to 20 minutes, up to {timeout_min}); the access tokens are created when they are ready.", flush=True)
    while waits and time.time() - start < timeout_min * 60:
        for name, ready in list(waits.items()):
            try:
                ok = ready()
            except Exception:  # noqa: BLE001
                ok = False
            if ok:
                print(f"[{int(time.time() - start) // 60:02d}:{int(time.time() - start) % 60:02d}] {name} is ready", flush=True)
                del waits[name]
        if waits:
            s = int(time.time() - start)
            print(f"[{s // 60:02d}:{s % 60:02d}] waiting for {', '.join(waits)} ... {_apps_progress()}", flush=True)
            time.sleep(30)
    if waits:
        print(f"Gave up waiting for {', '.join(waits)} after {timeout_min} minutes. When they are up, run:\n"
              "  python3 bootstrap/bootstrap.py --portal-access\n  python3 bootstrap/bootstrap.py --viewer-access", flush=True)
        return False
    give_portal_access(present)
    give_viewer_access(present)
    return True


def build(cluster):
    if not cluster_exists(cluster):
        check_ports(cluster)
    create_cluster(cluster)
    if cluster == HUB:
        install_argocd(cluster)
    seed_secrets(cluster)


def argocd_password():
    return read_secret(HUB, ARGOCD_NS, "argocd-initial-admin-secret", "password") or "(the initial secret was deleted: the password was changed, or reset it with `argocd admin initial-password` / the argocd-secret)"


def summary(clusters, first_run=True):
    if first_run:
        print("\nEverything else is now deployed by the Argo CD of the gitops cluster, into the other two. The first sync takes about 10 minutes.")
        print("URLs work once traefik, cert-manager and gateway have synced in each cluster. The certificate is self-signed (browser warning).")
    print("\nWhere to log in (the passwords are random per bootstrap; print them again with: python3 bootstrap/bootstrap.py --credentials)")

    def show(name, host, ports, creds, path=""):
        https, http = ports
        print(f"\n{name}")
        print(f"  https  https://{host}:{https}{path}")
        print(f"  http   http://{host}:{http}{path}")
        print(f"  login  {creds}")

    if HUB in clusters:
        show("Argo CD", "argocd.localhost", (7443, 7080), "admin / " + argocd_password())
        show("Developer portal (Backstage)", "backstage-portal.localhost", (7443, 7080), "guest sign-in, read-only")
    if "observability" in clusters:
        gateway_pw = read_secret("observability", GATEWAY_AUTH_NAMESPACES[0], GATEWAY_LOGIN_SECRET, "password") or "(secret not found)"
        gateway_login = f"admin / {gateway_pw}   (gateway basic auth)"
        show("Grafana", "grafana.localhost", (9443, 9080), "admin / " + (read_secret("observability", "observability", "grafana-admin", "admin-password") or "(secret not found)"))
        show("Argo Workflows", "workflows.localhost", (9443, 9080), gateway_login)
        show("RCA reports", "reports.localhost", (9443, 9080), gateway_login)
    if "platform" in clusters:
        show("Orders API", "orders.localhost", (8443, 8080), "sample API, no login", "/orders")
        chaos = read_secret("platform", "demo", "orders-api-chaos", "CHAOS_TOKEN")
        if chaos:
            print("  fault  X-Chaos-Token: " + chaos + "   (header for the sample API's POST /chaos/* endpoints)")
    if "observability" in clusters:
        # no login of their own, so they are not published: people use Grafana, an operator uses a port-forward
        pf = f"kubectl --context {context('observability')} -n observability port-forward"
        print("\nMimir, Loki, Tempo and Alloy are not published on a gateway. Query them in Grafana, or use a port-forward:")
        print(f"  Mimir  {pf} svc/mimir 8081:8080    ->  http://localhost:8081/prometheus/api/v1/query?query=up")
        print(f"  Loki   {pf} svc/loki 3100:3100     ->  http://localhost:3100/loki/api/v1/labels")
        print(f"  Tempo  {pf} svc/tempo 3200:3200    ->  http://localhost:3200/api/search")
        print(f"  Alloy  {pf} svc/alloy 12345:12345  ->  http://localhost:12345")
    print(f"\nWatch the deployments:  kubectl --context {context(HUB)} -n {ARGOCD_NS} get applications")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--destroy", action="store_true")
    ap.add_argument("--only", choices=list(CLUSTERS), help="act on one cluster instead of all three")
    ap.add_argument("--credentials", action="store_true", help="print the URLs and logins of the clusters that exist, and exit")
    ap.add_argument("--no-wait", action="store_true", help="do not wait for Grafana and the Argo CD portal account (and so do not create the access tokens); run --portal-access and --viewer-access later")
    ap.add_argument("--wait-access", action="store_true", help="only wait for Grafana and Argo CD and create the access tokens, then exit")
    ap.add_argument("--viewer-access", action="store_true", help="(re)create the read-only Grafana token of the RCA report viewer, and exit (run it once Grafana is up)")
    ap.add_argument("--portal-access", action="store_true", help="(re)create the read-only tokens of the developer portal, and exit (run it once Grafana is up)")
    args = ap.parse_args()

    clusters = [args.only] if args.only else list(CLUSTERS)
    if args.credentials:
        summary([c for c in clusters if cluster_exists(c)], first_run=False)
        return

    if args.wait_access:
        wait_for_services({c for c in CLUSTERS if cluster_exists(c)})
        return
    if args.viewer_access:
        give_viewer_access({c for c in CLUSTERS if cluster_exists(c)})
        return
    if args.portal_access:
        give_portal_access({c for c in CLUSTERS if cluster_exists(c)})
        return

    preflight()
    if args.destroy:
        for c in clusters:  # the hub last: it is what deploys the others
            if cluster_exists(c):
                run("kind", "delete", "cluster", "--name", c)
            else:
                print(f"cluster {c} does not exist, skipping")
        return

    for c in clusters:
        build(c)

    # the hub deploys the other clusters, so wire them together once they exist
    if cluster_exists(HUB):
        wire({c for c in CLUSTERS if cluster_exists(c)})
    if cluster_exists(HUB) and not args.no_wait:
        wait_for_services({c for c in CLUSTERS if cluster_exists(c)})
    summary(clusters)


if __name__ == "__main__":
    main()
