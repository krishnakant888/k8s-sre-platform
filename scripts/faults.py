#!/usr/bin/env python3
"""Fault injection for the generic alerts and the SRE agent: one command per fault, and a check that the report the agent wrote is right.

    scripts/faults.py list                      what can be injected, which alert it should raise and what the report should conclude
    scripts/faults.py start crash badimage      inject some faults (names from `list`)
    scripts/faults.py start all                 inject every fault that does not disturb the platform
    scripts/faults.py start disruptive          node down, ingress controller down, collector down (pauses Argo CD sync of what it breaks)
    scripts/faults.py check                     compare the RCA reports with what each fault should produce: PASS / WRONG / NO ALERT
    scripts/faults.py clear                     remove everything created here and undo the disruptive faults

The workloads live in the `sandbox` namespace (and `fault-quota`) of the platform cluster and are not managed by Argo CD, so nothing heals them.
The expectation of each fault (alert, root-cause category, layer) is the single source of truth for `check`.
Faults of the orders-api service itself (latency, errors, memory) are in scripts/drill.sh.
"""
import json
import os
import subprocess
import sys
import time

PLATFORM = os.environ.get("KUBE_CONTEXT", "kind-platform")
HUB = os.environ.get("HUB_CONTEXT", "kind-gitops")
OBS = os.environ.get("OBS_CONTEXT", "kind-observability")
NS = "sandbox"
BUSY = "busybox:1.37.0"
NGINX = "nginxinc/nginx-unprivileged:1.27-alpine"
PYTHON = "python:3.12-alpine"
SMALL = {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"memory": "64Mi"}}


def kubectl(ctx, *args, data=None, check=True):
    r = subprocess.run(["kubectl", "--context", ctx, *args], input=data, text=True, capture_output=True)
    if check and r.returncode:
        sys.exit(f"kubectl {' '.join(args)} failed: {r.stderr.strip()[:300]}")
    return r


def apply(objs, ctx=PLATFORM):
    objs = objs if isinstance(objs, list) else [objs]
    kubectl(ctx, "apply", "-f", "-", data=json.dumps({"apiVersion": "v1", "kind": "List", "items": objs}))


LABELS = lambda name: {"app.kubernetes.io/name": name, "app.kubernetes.io/version": "0.0.1", "team": "fault-drill"}


def container(image=BUSY, command=None, resources=None, env=None, probes=None, ports=None, mounts=None, read_only=True, name="app"):
    c = {"name": name, "image": image, "resources": resources or SMALL,
         "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": read_only, "capabilities": {"drop": ["ALL"]}}}
    if command:
        c["command"] = command
    if env:
        c["env"] = env
    if ports:
        c["ports"] = ports
    if mounts:
        c["volumeMounts"] = mounts
    c.update(probes or {})
    return c


def deployment(name, c, replicas=1, ns=NS, volumes=None, pod=None, uid=10001):
    spec = {"automountServiceAccountToken": False, "containers": [c],
            "securityContext": {"runAsNonRoot": True, "runAsUser": uid, "runAsGroup": uid, "seccompProfile": {"type": "RuntimeDefault"}}}
    spec.update(pod or {})
    if volumes:
        spec["volumes"] = volumes
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": ns, "labels": LABELS(name)},
            "spec": {"replicas": replicas, "selector": {"matchLabels": {"app.kubernetes.io/name": name}},
                     "template": {"metadata": {"labels": LABELS(name)}, "spec": spec}}}


def sh(script):
    return ["sh", "-c", script]


def ingress_app(name, host, objs):
    """service + ingress in front of a deployment, so the ingress controller sees the traffic"""
    return objs + [
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": NS}, "spec": {"selector": {"app.kubernetes.io/name": name}, "ports": [{"name": "http", "port": 80, "targetPort": "http"}]}},
        {"apiVersion": "networking.k8s.io/v1", "kind": "Ingress", "metadata": {"name": name, "namespace": NS},
         "spec": {"ingressClassName": "nginx", "rules": [{"host": host, "http": {"paths": [{"path": "/", "pathType": "Prefix", "backend": {"service": {"name": name, "port": {"name": "http"}}}}]}}]}}]


def tmp_mount():
    return [{"name": "tmp", "emptyDir": {}}], [{"name": "tmp", "mountPath": "/tmp"}]


# ------------------------------------------------------------------------------------------------------------- scenarios
SCENARIOS = {}


def scenario(name, description, alerts, categories, layers, minutes, service=None, group="all"):
    def deco(fn):
        SCENARIOS[name] = {"fn": fn, "description": description, "alerts": alerts, "categories": categories, "layers": layers, "minutes": minutes,
                           "service": service or f"fault-{name}", "group": group}
        return fn
    return deco


@scenario("crash", "process exits with an error at start (missing setting)", ["ContainerWaiting", "PodCrashLooping"], ["pod_crash"], ["application"], 4)
def _():
    apply(deployment("fault-crash", container(command=sh("echo 'KeyError: DATABASE_URL is not set' >&2; exit 1"))))


@scenario("crash-perm", "process cannot write to its read-only filesystem", ["ContainerWaiting", "PodCrashLooping"], ["config_error"], ["application"], 4)
def _():
    apply(deployment("fault-crash-perm", container(command=sh("echo 'open /data/state.db: read-only file system' >&2; exit 1"))))


@scenario("crash-dep", "process exits because its dependency refuses connections", ["ContainerWaiting", "PodCrashLooping"], ["dependency_unreachable"], ["application"], 4)
def _():
    apply(deployment("fault-crash-dep", container(command=sh("echo 'dial tcp 10.96.0.99:5432: connect: connection refused' >&2; exit 1"))))


@scenario("crash-netpol", "dependency call times out because the namespace network policy blocks it", ["ContainerWaiting", "PodCrashLooping"], ["dependency_unreachable"], ["application"], 4)
def _():
    apply(deployment("fault-crash-netpol", container(command=sh("wget -T 3 -q -O- http://dummy-shop.sandbox.svc/ || { echo 'wget: download timed out' >&2; exit 1; }"))))


@scenario("crash-dns", "process cannot resolve a host name (CoreDNS is healthy)", ["ContainerWaiting", "PodCrashLooping"], ["dns_failure"], ["application"], 4)
def _():
    apply(deployment("fault-crash-dns", container(command=sh("wget -T 3 -q -O- http://payments.internal.invalid/ ; echo \"wget: bad address 'payments.internal.invalid'\" >&2; exit 1"))))


@scenario("crash-exit0", "command finishes with exit code 0 instead of serving", ["ContainerWaiting", "PodCrashLooping"], ["pod_crash"], ["application"], 4)
def _():
    apply(deployment("fault-crash-exit0", container(command=sh("echo 'migration done'"))))


@scenario("badimage", "image tag that does not exist", ["ContainerWaiting"], ["image_pull_failure"], ["application"], 5)
def _():
    apply(deployment("fault-badimage", container(image="nginxinc/nginx-unprivileged:9.9.9-does-not-exist")))


@scenario("badregistry", "image on a registry host that does not resolve", ["ContainerWaiting"], ["image_pull_failure"], ["infrastructure"], 5)
def _():
    apply(deployment("fault-badregistry", container(image="registry.invalid/team/app:1.0.0")))


@scenario("nosecret", "environment from a secret that does not exist", ["ContainerWaiting"], ["config_error"], ["application"], 5)
def _():
    env = [{"name": "API_KEY", "valueFrom": {"secretKeyRef": {"name": "payments-api-key", "key": "key"}}}]
    apply(deployment("fault-nosecret", container(command=["sleep", "3600"], env=env)))


@scenario("noconfigmap", "environment from a config map that does not exist", ["ContainerWaiting"], ["config_error"], ["application"], 5)
def _():
    env = [{"name": "MODE", "valueFrom": {"configMapKeyRef": {"name": "feature-flags", "key": "mode"}}}]
    apply(deployment("fault-noconfigmap", container(command=["sleep", "3600"], env=env)))


@scenario("unschedulable", "asks for more memory than any node has", ["PodUnschedulable", "PodStuckPending"], ["scheduling_failure"], ["infrastructure"], 6)
def _():
    apply(deployment("fault-unschedulable", container(command=["sleep", "3600"], resources={"requests": {"cpu": "10m", "memory": "200Gi"}, "limits": {"memory": "200Gi"}})))


@scenario("nodeselector", "node selector that no node satisfies", ["PodUnschedulable", "PodStuckPending"], ["scheduling_failure"], ["application"], 6)
def _():
    apply(deployment("fault-nodeselector", container(command=["sleep", "3600"]), pod={"nodeSelector": {"disktype": "ssd-nvme"}}))


@scenario("pvc-unbound", "volume claim whose storage class does not exist", ["PodUnschedulable", "PodStuckPending"], ["scheduling_failure"], ["infrastructure"], 6)
def _():
    pvc = {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": "fault-pvc-unbound-data", "namespace": NS},
           "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": "fast-ssd", "resources": {"requests": {"storage": "1Gi"}}}}
    dep = deployment("fault-pvc-unbound", container(command=["sleep", "3600"], mounts=[{"name": "data", "mountPath": "/data"}]),
                     volumes=[{"name": "data", "persistentVolumeClaim": {"claimName": "fault-pvc-unbound-data"}}])
    apply([pvc, dep])


@scenario("oom", "allocates memory until the kernel kills it", ["PodOOMKilled", "ContainerMemoryNearLimit", "PodCrashLooping"], ["resource_exhaustion"], ["infrastructure", "application"], 4)
def _():
    code = "import time; a=[]; time.sleep(15); [(a.append(bytearray(5000000)), time.sleep(0.2)) for _ in range(1000)]"
    apply(deployment("fault-oom", container(image=PYTHON, command=["python", "-c", code], resources={"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"memory": "64Mi"}})))


@scenario("throttle", "busy loop under a tiny CPU limit", ["ContainerCpuThrottled"], ["resource_exhaustion"], ["infrastructure"], 13)
def _():
    apply(deployment("fault-throttle", container(command=sh("while true; do :; done"), resources={"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"cpu": "50m", "memory": "64Mi"}})))


@scenario("probe-liveness", "liveness probe that always fails: the kubelet keeps restarting a healthy process", ["PodCrashLooping", "ContainerWaiting"], ["probe_failure"], ["application"], 5)
def _():
    vols, mounts = tmp_mount()
    probe = {"livenessProbe": {"httpGet": {"path": "/does-not-exist", "port": "http"}, "initialDelaySeconds": 3, "periodSeconds": 5, "failureThreshold": 2}}
    apply(deployment("fault-probe-liveness", container(image=NGINX, ports=[{"name": "http", "containerPort": 8080}], mounts=mounts, probes=probe), volumes=vols, uid=101))


@scenario("probe-readiness", "readiness probe that always fails: the pod runs but never becomes ready", ["DeploymentReplicasUnavailable"], ["probe_failure"], ["application"], 6)
def _():
    vols, mounts = tmp_mount()
    probe = {"readinessProbe": {"httpGet": {"path": "/does-not-exist", "port": "http"}, "periodSeconds": 5}}
    apply(deployment("fault-probe-readiness", container(image=NGINX, ports=[{"name": "http", "containerPort": 8080}], mounts=mounts, probes=probe), volumes=vols, uid=101))


@scenario("job-fail", "one-shot job that fails", ["JobFailed"], ["pod_crash", "dependency_unreachable"], ["application"], 3)
def _():
    job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "fault-job-fail", "namespace": NS, "labels": LABELS("fault-job-fail")},
           "spec": {"backoffLimit": 0, "template": {"metadata": {"labels": LABELS("fault-job-fail")}, "spec": {
               "restartPolicy": "Never", "automountServiceAccountToken": False,
               "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
               "containers": [container(command=sh("echo 'migration failed: connection refused to db.sandbox.svc:5432' >&2; exit 2"))]}}}}
    apply(job)


@scenario("rollout-stuck", "a new version with a bad image while the old one still runs", ["ContainerWaiting", "DeploymentRolloutStuck"], ["image_pull_failure"], ["application"], 5)
def _():
    apply(deployment("fault-rollout-stuck", container(image=NGINX, ports=[{"name": "http", "containerPort": 8080}], mounts=tmp_mount()[1]), volumes=tmp_mount()[0], uid=101))
    kubectl(PLATFORM, "-n", NS, "rollout", "status", "deploy/fault-rollout-stuck", "--timeout=90s")
    kubectl(PLATFORM, "-n", NS, "set", "image", "deploy/fault-rollout-stuck", f"app={NGINX.split(':')[0]}:2.0.0-does-not-exist")


@scenario("quota", "namespace quota allows one pod, the deployment wants three", ["DeploymentReplicasUnavailable"], ["quota_exceeded"], ["infrastructure"], 6, service="fault-quota")
def _():
    ns = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "fault-quota", "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}}
    quota = {"apiVersion": "v1", "kind": "ResourceQuota", "metadata": {"name": "pods", "namespace": "fault-quota"}, "spec": {"hard": {"pods": "1"}}}
    apply([ns, quota])
    apply(deployment("fault-quota", container(command=["sleep", "3600"]), replicas=3, ns="fault-quota"))


@scenario("evict", "writes more than its ephemeral-storage limit", ["PodFailed", "PodCrashLooping"], ["resource_exhaustion"], ["application", "infrastructure"], 7)
def _():
    res = {"requests": {"cpu": "10m", "memory": "16Mi", "ephemeral-storage": "5Mi"}, "limits": {"memory": "64Mi", "ephemeral-storage": "10Mi"}}
    vols, mounts = tmp_mount()
    apply(deployment("fault-evict", container(command=sh("sleep 5; dd if=/dev/zero of=/tmp/fill bs=1M count=40; sleep 3600"), resources=res, mounts=mounts), volumes=vols))


@scenario("ingress-5xx", "backend answers 503 to users", ["IngressHighErrorRate"], ["ingress_backend_failure"], ["application"], 8, service="fault-ingress-5xx")
def _():
    conf = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "fault-ingress-5xx-nginx", "namespace": NS}, "data": {"default.conf": 'server { listen 8080; location / { return 503 "backend unavailable\\n"; } }\n'}}
    vols = [{"name": "conf", "configMap": {"name": "fault-ingress-5xx-nginx"}}, {"name": "tmp", "emptyDir": {}}]
    mounts = [{"name": "conf", "mountPath": "/etc/nginx/conf.d"}, {"name": "tmp", "mountPath": "/tmp"}]
    apply(ingress_app("fault-ingress-5xx", "fault-5xx.localhost", [conf, deployment("fault-ingress-5xx", container(image=NGINX, ports=[{"name": "http", "containerPort": 8080}], mounts=mounts), volumes=vols, uid=101)]))


@scenario("ingress-slow", "backend takes two seconds per request", ["IngressSlow"], ["ingress_backend_failure"], ["application"], 8, service="fault-ingress-slow")
def _():
    code = "import http.server,time\nclass H(http.server.BaseHTTPRequestHandler):\n  def do_GET(s):\n    time.sleep(2); s.send_response(200); s.end_headers(); s.wfile.write(b'ok')\n  def log_message(*a): pass\nhttp.server.ThreadingHTTPServer(('',8080),H).serve_forever()"
    apply(ingress_app("fault-ingress-slow", "fault-slow.localhost", [deployment("fault-ingress-slow", container(image=PYTHON, command=["python", "-c", code], ports=[{"name": "http", "containerPort": 8080}], resources={"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"memory": "96Mi"}}))]))


@scenario("ingress-nobackend", "service behind the ingress scaled to zero", ["IngressHighErrorRate"], ["ingress_backend_failure"], ["application"], 8, service="fault-ingress-nobackend")
def _():
    vols, mounts = tmp_mount()
    apply(ingress_app("fault-ingress-nobackend", "fault-nobackend.localhost", [deployment("fault-ingress-nobackend", container(image=NGINX, ports=[{"name": "http", "containerPort": 8080}], mounts=mounts), volumes=vols, uid=101)]))
    kubectl(PLATFORM, "-n", NS, "rollout", "status", "deploy/fault-ingress-nobackend", "--timeout=90s")
    kubectl(PLATFORM, "-n", NS, "scale", "deploy/fault-ingress-nobackend", "--replicas=0")


def argo_app(name, path, ns=NS):
    return {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application", "metadata": {"name": name, "namespace": "argocd"},
            "spec": {"project": "prod-workloads", "source": {"repoURL": "https://github.com/krishnakant888/k8s-sre-platform.git", "targetRevision": "main", "path": path},
                     "destination": {"name": "platform", "namespace": ns}, "syncPolicy": {"automated": {"prune": True, "selfHeal": True}}}}


@scenario("argo-badpath", "Git path that does not exist: Argo CD cannot render the application", ["ArgoAppOutOfSync"], ["gitops_sync_failure"], ["application"], 18, service="fault-argo-badpath")
def _():
    apply(argo_app("fault-argo-badpath", "gitops/workloads/this-path-does-not-exist"), HUB)


@scenario("argo-degraded", "application that syncs but whose deployment never becomes healthy", ["ArgoAppUnhealthy"], ["deploy_regression", "image_pull_failure"], ["application"], 8, service="fault-argo-degraded")
def _():
    apply(argo_app("fault-argo-degraded", "gitops/fault-drills/degraded"), HUB)


@scenario("argo-policy", "manifest that Pod Security refuses (privileged pod)", ["ArgoSyncFailing"], ["gitops_sync_failure"], ["application"], 6, service="fault-argo-policy")
def _():
    apply(argo_app("fault-argo-policy", "gitops/fault-drills/policy"), HUB)


@scenario("argo-kyverno", "Kyverno refuses a floating image tag at sync", ["ArgoSyncFailing"], ["gitops_sync_failure"], ["application"], 6, service="fault-argo-kyverno")
def _():
    apply(argo_app("fault-argo-kyverno", "gitops/fault-drills/kyverno", "demo"), HUB)


@scenario("sts-notready", "StatefulSet whose pod never becomes ready", ["StatefulSetReplicasNotReady"], ["probe_failure"], ["application"], 13)
def _():
    vols, mounts = tmp_mount()
    c = container(image=NGINX, ports=[{"name": "http", "containerPort": 8080}], mounts=mounts, probes={"readinessProbe": {"httpGet": {"path": "/nope", "port": "http"}, "periodSeconds": 5}})
    dep = deployment("fault-sts-notready", c, volumes=vols, uid=101)
    sts = {"apiVersion": "apps/v1", "kind": "StatefulSet", "metadata": dep["metadata"], "spec": {**dep["spec"], "serviceName": "fault-sts-notready"}}
    svc = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "fault-sts-notready", "namespace": NS}, "spec": {"clusterIP": "None", "selector": {"app.kubernetes.io/name": "fault-sts-notready"}, "ports": [{"name": "http", "port": 8080}]}}
    apply([svc, sts])


@scenario("ds-unavailable", "DaemonSet whose image cannot be pulled", ["DaemonSetPodsUnavailable", "ContainerWaiting"], ["image_pull_failure"], ["application"], 13)
def _():
    dep = deployment("fault-ds-unavailable", container(image="nginxinc/nginx-unprivileged:9.9.9-does-not-exist"))
    ds = {"apiVersion": "apps/v1", "kind": "DaemonSet", "metadata": dep["metadata"], "spec": {"selector": dep["spec"]["selector"], "template": dep["spec"]["template"]}}
    apply(ds)


@scenario("workflow-fail", "an investigation workflow ends in Failed", ["InvestigationWorkflowFailed"], ["investigation_pipeline_failed"], ["application", "infrastructure"], 6, service="sre-agent")
def _():
    wf = {"apiVersion": "argoproj.io/v1alpha1", "kind": "Workflow", "metadata": {"name": f"drill-failing-{int(time.time())}", "namespace": "sre-agent"},
          "spec": {"entrypoint": "main", "serviceAccountName": "sre-agent", "templates": [{"name": "main", "container": {
              "image": "busybox:1.37.0", "command": ["sh", "-c", "echo 'anthropic.AuthenticationError: 401 invalid x-api-key' >&2; exit 1"],
              "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"memory": "64Mi"}},
              "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}}}]}}
    apply(wf, OBS)


# ---- disruptive: they break something the platform depends on, so the Argo CD sync of it is paused first
def pause_sync(app):
    kubectl(HUB, "-n", "argocd", "patch", "application", app, "--type", "merge", "-p", '{"spec":{"syncPolicy":{"automated":null}}}')


def resume_sync(app):
    kubectl(HUB, "-n", "argocd", "patch", "application", app, "--type", "merge", "-p", '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}', check=False)


@scenario("addon-down", "ingress controller of the platform cluster scaled to zero", ["CriticalAddonDown"], ["scaled_to_zero"], ["infrastructure"], 5, service="ingress-nginx-controller", group="disruptive")
def _():
    pause_sync("prod-ingress-nginx")
    kubectl(PLATFORM, "-n", "ingress-nginx", "scale", "deploy/ingress-nginx-controller", "--replicas=0")


@scenario("collector-down", "collector of the gitops cluster scaled to zero", ["ClusterTelemetryMissing"], ["telemetry_gap"], ["infrastructure"], 8, service="alloy", group="disruptive")
def _():
    pause_sync("hub-alloy")
    kubectl(HUB, "-n", "observability", "scale", "deploy/alloy", "--replicas=0")


@scenario("loki-down", "the log store is scaled to zero", ["LogsDropped"], ["telemetry_gap"], ["infrastructure"], 8, service="alloy", group="disruptive")
def _():
    kubectl(HUB, "-n", "argocd", "patch", "application", "loki", "--type", "merge", "-p", '{"spec":{"syncPolicy":{"automated":null}}}')
    kubectl(OBS, "-n", "observability", "scale", "statefulset/loki", "--replicas=0")


@scenario("partition", "the node that runs the stores is cut off from the other clusters", ["ClusterTelemetryMissing"], ["telemetry_gap"], ["infrastructure"], 8, service="alloy", group="disruptive")
def _():
    subprocess.run(["docker", "network", "disconnect", "kind", "observability-worker"], check=True)


@scenario("node-down", "a worker node of the platform cluster stops", ["NodeNotReady"], ["node_failure"], ["infrastructure"], 5, service="platform-worker2", group="disruptive")
def _():
    subprocess.run(["docker", "stop", "platform-worker2"], check=True)


def clear():
    save_state({})
    kubectl(PLATFORM, "delete", "pods", "-A", "--field-selector=status.phase=Failed", check=False)   # evicted pods stay as objects
    kubectl(PLATFORM, "-n", NS, "delete", "deploy,svc,ingress,cm,pvc,job", "-l", "team=fault-drill", "--ignore-not-found", check=False)
    kubectl(PLATFORM, "delete", "ns", "fault-quota", "--ignore-not-found", check=False)
    for app in ("fault-argo-badpath", "fault-argo-degraded", "fault-argo-policy", "fault-argo-kyverno"):
        kubectl(HUB, "-n", "argocd", "delete", "application", app, "--ignore-not-found", check=False)
    subprocess.run(["docker", "start", "platform-worker2"], capture_output=True)
    resume_sync("prod-ingress-nginx")
    resume_sync("hub-alloy")
    kubectl(HUB, "-n", "argocd", "patch", "application", "loki", "--type", "merge", "-p", '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}', check=False)
    kubectl(OBS, "-n", "observability", "scale", "statefulset/loki", "--replicas=1", check=False)
    subprocess.run(["docker", "network", "connect", "kind", "observability-worker"], capture_output=True)
    kubectl(OBS, "-n", "sre-agent", "delete", "workflow", "-l", "none=none", "--ignore-not-found", check=False)
    kubectl(PLATFORM, "-n", "ingress-nginx", "scale", "deploy/ingress-nginx-controller", "--replicas=1", check=False)
    kubectl(HUB, "-n", "observability", "scale", "deploy/alloy", "--replicas=1", check=False)
    print("cleared")


# ------------------------------------------------------------------------------------------------------------- check
def reports():
    ids = kubectl(OBS, "-n", "sre-agent", "exec", "deploy/rca-viewer", "--", "ls", "/reports", check=False).stdout.split()
    out = []
    for rid in ids:
        raw = kubectl(OBS, "-n", "sre-agent", "exec", "deploy/rca-viewer", "--", "cat", f"/reports/{rid}/report.json", check=False).stdout
        try:
            out.append(json.loads(raw))
        except ValueError:
            pass
    return out


STATE = os.path.expanduser("~/.faults-state.json")


def load_state():
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {}


def save_state(state):
    json.dump(state, open(STATE, "w"))


def check(names):
    from datetime import datetime

    state = load_state()
    if state and names == list(SCENARIOS):
        names = [n for n in names if n in state]   # only the faults that were injected
    all_reports = reports()
    # a report counts only when it was written after the fault was injected (older ones are from earlier runs)
    for r in all_reports:
        r["_at"] = datetime.fromisoformat(r["generated_at"].replace("Z", "+00:00")).timestamp()
    ok = wrong = missing = 0
    print(f"{'fault':<18} {'verdict':<9} {'alert':<28} {'category':<24} {'layer':<15} {'priority'}")
    for name in names:
        s = SCENARIOS[name]
        mine = [r for r in all_reports if (r["service"] == s["service"] or r["service"].startswith(s["service"])) and r["_at"] >= state.get(name, 0)]
        if not mine:
            print(f"{name:<18} {'NO ALERT':<9} (expected {'/'.join(s['alerts'])} within {s['minutes']} minutes, or the report has not been written yet)")
            missing += 1
            continue
        good = [r for r in mine if r["root_cause"]["category"] in s["categories"] and r["fault_domain"]["layer"] in s["layers"]]
        r = (good or mine)[-1]
        verdict = "PASS" if good else "WRONG"
        ok += bool(good)
        wrong += not good
        print(f"{name:<18} {verdict:<9} {r['alert']:<28} {r['root_cause']['category']:<24} {r['fault_domain']['layer']:<15} {r['priority']['level']}"
              + ("" if good else f"   expected {'/'.join(s['categories'])} at {'/'.join(s['layers'])}"))
    print(f"\n{ok} right, {wrong} wrong, {missing} without a report")
    return wrong + missing


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "list":
        for n, s in SCENARIOS.items():
            print(f"{n:<18} [{s['group']}] {s['description']}\n{'':<18} alert {'/'.join(s['alerts'])} -> {'/'.join(s['categories'])}, {'/'.join(s['layers'])} level, ~{s['minutes']} min")
    elif cmd == "start":
        want = argv[2:]
        names = [n for n, s in SCENARIOS.items() if s["group"] == "all"] if want == ["all"] else [n for n, s in SCENARIOS.items() if s["group"] == "disruptive"] if want == ["disruptive"] else want
        state = load_state()
        for n in names:
            if n not in SCENARIOS:
                sys.exit(f"unknown fault {n}, see `list`")
            print("injecting", n)
            for target in (f"platform-sandbox-{SCENARIOS[n]['service']}", f"gitops-{SCENARIOS[n]['service']}", f"platform-fault-quota-{SCENARIOS[n]['service']}"):
                kubectl(OBS, "-n", "sre-agent", "delete", "workflow", "-l", f"sre-agent/target={target}", "--ignore-not-found", check=False)
            SCENARIOS[n]["fn"]()
            state[n] = time.time()
        save_state(state)
        print(f"\ninjected {len(names)}; the slowest needs about {max(SCENARIOS[n]['minutes'] for n in names)} minutes to alert. Then: scripts/faults.py check")
    elif cmd == "check":
        sys.exit(1 if check(argv[2:] or list(SCENARIOS)) else 0)
    elif cmd == "clear":
        clear()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv)
