"""Investigation of alerts that are not about one service's SLO: any pod, node, volume, ingress, Argo CD application, control plane
or telemetry problem, in any cluster. Nothing here names an application: the alert labels (cluster, namespace, pod, node, ingress,
application name) say where to look, the same read-only tools collect the facts, and a rule table turns the facts into

  what happened, the impact, the layer (application or infrastructure) with the reasoning, the fix, the priority and the gaps.

The SLO path in rca.py keeps handling the orders-api burn alerts; rca.gather / rca.analyze_rules hand everything listed in
FAMILIES to this module.
"""
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from agent import config, tools
from agent.models import Evidence, FaultDomain, HowToFix, Impact, Priority, RcaReport, RootCause, TimelineEntry

# alert name -> what kind of problem it is
FAMILIES = {
    "ContainerWaiting": "container", "PodStuckPending": "scheduling", "PodUnschedulable": "scheduling",
    "DeploymentRolloutStuck": "rollout", "StatefulSetReplicasNotReady": "rollout", "DaemonSetPodsUnavailable": "rollout",
    "JobFailed": "job", "ContainerMemoryNearLimit": "memory", "ContainerCpuThrottled": "cpu",
    "PersistentVolumeFillingUp": "volume", "PersistentVolumeAlmostFull": "volume", "TargetDown": "target",
    "NodeNotReady": "node", "NodePressure": "node", "NodeCpuSaturated": "node", "NodeMemoryNearAllocatable": "node",
    "ApiServerErrors": "controlplane", "ApiServerSlow": "controlplane", "CoreDnsErrors": "dns",
    "IngressHighErrorRate": "ingress", "IngressSlow": "ingress",
    "ArgoAppOutOfSync": "argocd", "ArgoAppUnhealthy": "argocd", "ArgoSyncFailing": "argocd",
    "ClusterTelemetryMissing": "telemetry", "TelemetryNotDelivered": "telemetry", "LogsDropped": "telemetry",
}

ERROR_LINES = r"(?i)error|fatal|panic|exception|traceback|refused|denied|timeout|unable|failed|cannot"


def is_generic(alert: dict) -> bool:
    return alert.get("labels", {}).get("alertname") in FAMILIES


# ---------------------------------------------------------------------------------------------------------------- helpers
def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _q(query: str, minutes: int = 30) -> dict:
    return tools.call_tool("query_promql", {"query": query, "minutes": minutes})


def _num(res: dict, key: str = "max", fn=max, default=None):
    vals = [s[key] for s in (res.get("series") or []) if key in s]
    return fn(vals) if vals else default


def _label_values(res: dict, label: str) -> list[str]:
    return sorted({s["labels"][label] for s in (res.get("series") or []) if s.get("labels", {}).get(label)})


def _workload(labels: dict) -> str:
    for key in ("deployment", "statefulset", "daemonset", "job_name", "service"):
        if labels.get(key):
            return labels[key]
    if labels.get("pod"):
        return re.sub(r"-[a-z0-9]{8,10}-[a-z0-9]{5}$|-[a-z0-9]{5}$|-\d+$", "", labels["pod"])
    return labels.get("ingress") or labels.get("name") or labels.get("node") or labels.get("persistentvolumeclaim") or ""


def _sel(cluster: str, ns: str = "", extra: str = "") -> str:
    parts = [f'cluster="{cluster}"'] + ([f'namespace="{ns}"'] if ns else []) + ([extra] if extra else [])
    return ", ".join(parts)


@dataclass
class Diag:
    category: str
    layer: str
    component: str
    confidence: str
    description: str
    what: str
    reasoning: list[str] = field(default_factory=list)
    immediate: list[str] = field(default_factory=list)
    permanent: list[str] = field(default_factory=list)
    verify: list[str] = field(default_factory=list)
    impact: str = ""
    contributing: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------------------------------------------- gather
def gather(alert: dict, now: float | None = None, lookback_minutes: int = 45) -> dict:
    from agent import rca  # imported here: rca dispatches to this module

    now = now or time.time()
    labels = alert.get("labels", {})
    family = FAMILIES[labels["alertname"]]
    cluster = labels.get("cluster") or "platform"
    ns = labels.get("namespace", "")
    workload = _workload(labels)
    started = rca.parse_ts(alert["startsAt"]) if alert.get("startsAt") else now
    evidence: list[dict] = []
    raw: dict = {}
    sig: dict = {"family": family, "cluster": cluster, "namespace": ns, "workload": workload, "labels": labels, "tool_errors": [], "ongoing": True}

    def add(source, query, finding):
        evidence.append({"source": source, "query": query, "finding": finding})

    def pods_and_events(prefix: str, namespace: str):
        pods = tools.call_tool("get_pod_status", {"namespace": namespace, "name_prefix": prefix, "cluster": cluster})
        raw["pods"] = pods
        if "error" in pods:
            sig["tool_errors"].append(f"pod lookup in {cluster} failed: {pods['error'][:120]}")
            add("kubernetes", f"pods {namespace}/{prefix}* in {cluster}", f"lookup failed: {pods['error'][:120]}")
            sig["pods"] = []
        else:
            sig["pods"] = pods.get("pods", [])
            add("kubernetes", f"pods {namespace}/{prefix}* in {cluster}",
                f"{len(sig['pods'])} pods; " + ", ".join(f"{p['pod']}: {p['phase']}{'' if p['ready'] else ' not ready'}, restarts {p['restarts']}"
                                                         + (f", waiting {p['waiting']}" if p.get('waiting') else "")
                                                         + (f", last exit {p['last_terminated']['reason']}({p['last_terminated']['exit_code']})" if p.get('last_terminated') else "")
                                                         for p in sig["pods"][:4]))
        ev = tools.call_tool("get_k8s_events", {"namespace": namespace, "minutes": lookback_minutes, "cluster": cluster})
        raw["events"] = ev
        names = {p["pod"] for p in sig["pods"]} | {prefix}
        sig["events"] = [e for e in ev.get("events", []) if any(e["object"].startswith(n) or n.startswith(e["object"]) for n in names)] if "error" not in ev else []
        if "error" in ev:
            sig["tool_errors"].append(f"event lookup in {cluster} failed: {ev['error'][:120]}")
        if sig["events"]:
            add("kubernetes", f"events {namespace} in {cluster}", "; ".join(f"{e['reason']} {e['object']}: {e['message'][:110]}" for e in sig["events"][:4]))
        sig["nodes"] = sorted({p["node"] for p in sig["pods"] if p.get("node")})

    def logs_for(selector: str, label: str):
        q = f'{{{selector}}} |~ "{ERROR_LINES}"'
        logs = tools.call_tool("query_logql", {"query": q, "minutes": lookback_minutes, "limit": 150})
        raw["logs"] = logs
        sig["log_total"] = logs.get("total", 0)
        sig["top_log_messages"] = logs.get("top_messages", [])
        sig["log_fields"] = logs.get("fields_present", [])
        sig["log_breakdown"] = logs.get("breakdown", {})
        if "error" in logs:
            sig["tool_errors"].append(f"log lookup failed: {logs['error'][:120]}")
        add("loki", q, f"{label}: " + ("; ".join(f"{m[:110]!r} x{c}" for m, c in sig["top_log_messages"][:4]) if sig["top_log_messages"] else "no error lines in the window"))

    def node_facts(node_names: list[str]):
        if not node_names:
            return
        rx = "|".join(node_names)
        cond = _q(f'max by (node, condition) (kube_node_status_condition{{cluster="{cluster}", node=~"{rx}", status="true"}})')
        ready = _q(f'min by (node) (kube_node_status_condition{{cluster="{cluster}", node=~"{rx}", condition="Ready", status="true"}})')
        sig["node_not_ready"] = (_num(ready, "min", min, 1.0) or 0) < 1 if ready.get("series") else False
        sig["node_pressure"] = sorted({s["labels"].get("condition") for s in cond.get("series", []) if s["labels"].get("condition") in ("MemoryPressure", "DiskPressure", "PIDPressure") and s["max"] >= 1})
        cpu = _q(f'sum by (node) (rate(container_cpu_usage_seconds_total{{cluster="{cluster}", node=~"{rx}", container!=""}}[5m])) / on (node) sum by (node) (kube_node_status_allocatable{{cluster="{cluster}", node=~"{rx}", resource="cpu"}})')
        mem = _q(f'sum by (node) (container_memory_working_set_bytes{{cluster="{cluster}", node=~"{rx}", container!=""}}) / on (node) sum by (node) (kube_node_status_allocatable{{cluster="{cluster}", node=~"{rx}", resource="memory"}})')
        sig["node_cpu"], sig["node_mem"] = _num(cpu, "max") or 0.0, _num(mem, "max") or 0.0
        add("mimir", "kube_node_status_condition / node usage", f"node(s) {', '.join(node_names)}: " + ("NotReady" if sig["node_not_ready"] else "Ready")
            + (f", pressure {', '.join(sig['node_pressure'])}" if sig["node_pressure"] else ", no pressure")
            + f", CPU {sig['node_cpu']:.0%} and memory {sig['node_mem']:.0%} of allocatable")

    def deploys(app_names: list[str]):
        for name in app_names:
            res = tools.call_tool("get_recent_deploys", {"application": name})
            if "error" not in res:
                raw["deploys"] = res
                sig["source_files"] = res.get("source_files", [])
                recent = [d for d in res.get("history", []) if d.get("deployed_at") and rca.parse_ts(d["deployed_at"]) > started - 3600]
                sig["recent_deploy"] = recent[0] if recent else None
                add("argocd", f"history {name}", f"deploy {recent[0]['revision']} at {recent[0]['deployed_at']}" if recent else "no deploy in the hour before the alert")
                return
        add("argocd", f"history {app_names[0] if app_names else '?'}", "no Argo CD application of that name could be read")

    # ---------------------------------------------------------------- per family
    if family in ("container", "scheduling", "rollout", "job", "memory", "cpu", "target"):
        pod_prefix = labels.get("pod") or workload
        pods_and_events(workload or pod_prefix, ns)
        sel = _sel(cluster, ns, f'pod=~"{workload}-.*|{labels["pod"]}"' if labels.get("pod") else f'pod=~"{workload}-.*"')
        rst = _q(f'sum(increase(kube_pod_container_status_restarts_total{{{sel}}}[{lookback_minutes}m]))', 5)
        sig["restarts"] = _num(rst, "last") or 0.0
        mem = _q(f'sum(container_memory_working_set_bytes{{{sel}, container!=""}})')
        pts = [v for _, v in ((mem.get("series") or [{}])[0].get("points") or [])]
        sig["mem_growth"] = (pts[-1] / pts[0]) if len(pts) >= 4 and pts[0] > 0 else None
        ratio = _q(f'max(container_memory_working_set_bytes{{{sel}, container!=""}} / on (cluster, namespace, pod, container) group_left kube_pod_container_resource_limits{{{sel}, resource="memory"}})')
        sig["mem_ratio"] = _num(ratio, "max")
        thr = _q(f'max(rate(container_cpu_cfs_throttled_periods_total{{{sel}, container!=""}}[2m]) / rate(container_cpu_cfs_periods_total{{{sel}, container!=""}}[2m]))')
        sig["throttle"] = _num(thr, "max") or 0.0
        add("mimir", sel, f"restarts in {lookback_minutes}m: {sig['restarts']:.0f}"
            + (f"; memory at {sig['mem_ratio']:.0%} of its limit" if sig["mem_ratio"] is not None else "")
            + (f"; memory grew {sig['mem_growth']:.1f}x over the window" if sig["mem_growth"] else "")
            + f"; CPU throttled up to {sig['throttle']:.0%}")
        if sig["restarts"] or any(not p["ready"] for p in sig["pods"]) or family != "memory":
            logs_for(f'cluster="{cluster}", namespace="{ns}", pod=~"{workload}-.*|{labels["pod"]}"' if labels.get("pod") else f'cluster="{cluster}", namespace="{ns}", pod=~"{workload}-.*"', "error lines of the workload")
        node_facts(sig.get("nodes", []))
        deploys([f"prod-{workload}", workload])
        sig["ongoing"] = any(not p["ready"] for p in sig["pods"]) or bool(sig["restarts"] and sig["restarts"] > 0)

    elif family == "volume":
        pvc = labels.get("persistentvolumeclaim", workload)
        sel = f'cluster="{cluster}", namespace="{ns}", persistentvolumeclaim="{pvc}"'
        free = _q(f'min(kubelet_volume_stats_available_bytes{{{sel}}} / kubelet_volume_stats_capacity_bytes{{{sel}}})', 60)
        used = _q(f'max(kubelet_volume_stats_used_bytes{{{sel}}})', 60)
        cap = _q(f'max(kubelet_volume_stats_capacity_bytes{{{sel}}})', 60)
        sig["free_ratio"] = _num(free, "last") if free.get("series") else None
        sig["used_bytes"], sig["cap_bytes"] = _num(used, "last"), _num(cap, "last")
        pts = [v for _, v in ((used.get("series") or [{}])[0].get("points") or [])]
        sig["growth_per_hour"] = (pts[-1] - pts[0]) * (60 / 60) if len(pts) >= 2 else None
        users = _q(f'kube_pod_spec_volumes_persistentvolumeclaims_info{{{sel}}}', 5)
        sig["volume_pods"] = _label_values(users, "pod")
        add("mimir", f"kubelet_volume_stats {pvc}", f"{(sig['free_ratio'] or 0):.1%} free" + (f" ({(sig['used_bytes'] or 0) / 2**20:.0f} MiB of {(sig['cap_bytes'] or 0) / 2**20:.0f} MiB)" if sig["cap_bytes"] else "")
            + (f", used space changed by {sig['growth_per_hour'] / 2**20:+.0f} MiB over the last hour" if sig["growth_per_hour"] is not None else "") + f"; mounted by {', '.join(sig['volume_pods']) or 'no running pod'}")
        if sig["volume_pods"]:
            pods_and_events(re.sub(r"-[a-z0-9]{8,10}-[a-z0-9]{5}$|-\d+$", "", sig["volume_pods"][0]), ns)

    elif family == "node":
        node = labels.get("node", workload)
        node_facts([node])
        pods_on = _q(f'count(kube_pod_info{{cluster="{cluster}", node="{node}"}})', 5)
        sig["pods_on_node"] = int(_num(pods_on, "last") or 0)
        top = _q(f'topk(5, sum by (namespace, pod) (container_memory_working_set_bytes{{cluster="{cluster}", node="{node}", container!=""}}))', 5)
        sig["top_memory_pods"] = [f"{s['labels'].get('namespace')}/{s['labels'].get('pod')} ({s['last'] / 2**20:.0f} MiB)" for s in top.get("series", [])[:5]]
        ev = tools.call_tool("get_k8s_events", {"namespace": "default", "minutes": lookback_minutes, "cluster": cluster})
        sig["events"] = [e for e in ev.get("events", []) if node in e["object"]] if "error" not in ev else []
        add("mimir", f"pods on {node}", f"{sig['pods_on_node']} pods run on the node; biggest memory users: {', '.join(sig['top_memory_pods']) or 'unknown'}")
        sig["user_pods_on_node"] = _label_values(_q(f'kube_pod_info{{cluster="{cluster}", node="{node}", namespace=~"demo"}}', 5), "pod")
        sig["ongoing"] = bool(sig.get("node_not_ready") or sig.get("node_pressure") or sig.get("node_cpu", 0) > 0.9 or sig.get("node_mem", 0) > 0.9)

    elif family == "ingress":
        ing = labels.get("ingress", workload)
        sel = f'cluster="{cluster}", namespace="{ns}", ingress="{ing}"'
        err = _q(f'sum(rate(nginx_ingress_controller_requests{{{sel}, status=~"5.."}}[2m])) / sum(rate(nginx_ingress_controller_requests{{{sel}}}[2m]))')
        p95 = _q(f'histogram_quantile(0.95, sum by (le) (rate(nginx_ingress_controller_request_duration_seconds_bucket{{{sel}}}[2m])))')
        by_status = _q(f'sum by (status) (rate(nginx_ingress_controller_requests{{{sel}}}[5m]))', 10)
        rps = _q(f'sum(rate(nginx_ingress_controller_requests{{{sel}}}[2m]))')
        sig["error_ratio_peak"], sig["error_ratio_last"] = _num(err, "max") or 0.0, _num(err, "last") or 0.0
        sig["p95_peak"] = _num(p95, "max") or 0.0
        sig["rps"] = _num(rps, "last") or 0.0
        sig["statuses"] = {s["labels"].get("status"): round(s["last"], 3) for s in by_status.get("series", [])}
        add("mimir", f"nginx_ingress_controller_requests {ing}", f"{sig['rps']:.2f} req/s, 5xx ratio peaked at {sig['error_ratio_peak']:.0%} (now {sig['error_ratio_last']:.0%}), p95 up to {sig['p95_peak'] * 1000:.0f} ms; by status {sig['statuses']}")
        backend = _q(f'max by (service_name) (kube_ingress_path{{{sel}}})', 5)
        names = _label_values(backend, "service_name")
        sig["backend"] = names[0] if names else ing
        pods_and_events(sig["backend"], ns)
        logs_for(f'cluster="{cluster}", namespace="{ns}", pod=~"{sig["backend"]}-.*"', f"error lines of backend {sig['backend']}")
        node_facts(sig.get("nodes", []))
        deploys([f"prod-{sig['backend']}", sig["backend"]])
        sig["ongoing"] = sig["error_ratio_last"] > 0.02 or (family == "ingress" and sig["p95_peak"] > 1 and labels["alertname"] == "IngressSlow")

    elif family == "argocd":
        app = labels.get("name", workload)
        res = tools.call_tool("get_argocd_app", {"name": app})
        raw["argocd"] = res
        sig["app"] = res
        if "error" in res:
            sig["tool_errors"].append(f"Argo CD lookup failed: {res['error'][:120]}")
        add("argocd", f"application {app}", f"sync {res.get('sync_status')}, health {res.get('health_status')}, destination {res.get('destination')}"
            + (f"; operation {res.get('operation', {}).get('phase')}: {res.get('operation', {}).get('message', '')[:160]}" if res.get("operation") else "")
            + (f"; conditions: {'; '.join(c[:120] for c in res.get('conditions', [])[:3])}" if res.get("conditions") else "")
            + (f"; unhealthy resources: {', '.join(res.get('unhealthy', [])[:4])}" if res.get("unhealthy") else ""))
        sig["ongoing"] = res.get("sync_status") != "Synced" or res.get("health_status") not in ("Healthy", None)
        dest = res.get("destination_cluster") or ""
        if res.get("unhealthy") and res.get("destination_namespace") and dest:
            first = res["unhealthy"][0]
            if "/" in first:
                w = first.split(":")[0].split("/")[-1]
                pods_and_events(w, res["destination_namespace"])

    elif family in ("controlplane", "dns"):
        a_err = _q(f'sum by (code) (rate(apiserver_request_total{{cluster="{cluster}", code=~"5.."}}[2m]))')
        a_ratio = _q(f'sum(rate(apiserver_request_total{{cluster="{cluster}", code=~"5.."}}[2m])) / sum(rate(apiserver_request_total{{cluster="{cluster}"}}[2m]))')
        a_p99 = _q(f'histogram_quantile(0.99, sum by (le) (rate(apiserver_request_duration_seconds_bucket{{cluster="{cluster}", verb!~"WATCH|CONNECT"}}[2m])))')
        etcd = _q(f'histogram_quantile(0.99, sum by (le) (rate(etcd_request_duration_seconds_bucket{{cluster="{cluster}"}}[2m])))')
        dns = _q(f'sum(rate(coredns_dns_responses_total{{cluster="{cluster}", rcode="SERVFAIL"}}[2m])) / sum(rate(coredns_dns_responses_total{{cluster="{cluster}"}}[2m]))')
        sig["apiserver_error_ratio"] = _num(a_ratio, "max") or 0.0
        sig["apiserver_p99"] = _num(a_p99, "max") or 0.0
        sig["etcd_p99"] = _num(etcd, "max") or 0.0
        sig["dns_servfail"] = _num(dns, "max") or 0.0
        add("mimir", f"apiserver / etcd / coredns in {cluster}", f"API 5xx ratio peaked at {sig['apiserver_error_ratio']:.1%}, API p99 {sig['apiserver_p99'] * 1000:.0f} ms, etcd p99 {sig['etcd_p99'] * 1000:.0f} ms, DNS SERVFAIL ratio {sig['dns_servfail']:.1%}")
        prefix = "coredns" if family == "dns" else "kube-apiserver"
        pods_and_events(prefix, "kube-system")
        node_facts(sorted({p["node"] for p in sig["pods"] if p.get("node")}))
        sig["ongoing"] = (sig["apiserver_error_ratio"] > 0.02 if family == "controlplane" else sig["dns_servfail"] > 0.02) or sig["apiserver_p99"] > 1

    elif family == "telemetry":
        target = labels.get("cluster") or cluster
        up = _q(f'max by (job) (up{{cluster="{target}"}})', 15)
        age = _q(f'time() - max(timestamp(up{{job="alloy", cluster="{target}"}}))', 5)
        fail = _q(f'sum(rate(prometheus_remote_storage_samples_failed_total{{cluster="{target}"}}[5m]))', 15)
        drop = _q(f'sum(rate(loki_write_dropped_entries_total{{cluster="{target}"}}[5m]))', 15)
        sig["telemetry_age"] = _num(age, "last")
        sig["remote_write_failed"], sig["log_dropped"] = _num(fail, "max") or 0.0, _num(drop, "max") or 0.0
        sig["jobs_down"] = [s["labels"].get("job") for s in up.get("series", []) if s["last"] == 0]
        add("mimir", f"alloy in {target}", (f"last sample {sig['telemetry_age']:.0f}s ago" if sig["telemetry_age"] is not None else "no sample from the collector in the window")
            + f"; failed remote writes {sig['remote_write_failed']:.2f}/s, dropped log lines {sig['log_dropped']:.2f}/s; scrape jobs down: {', '.join(sig['jobs_down']) or 'none'}")
        cluster = target
        pods_and_events("alloy", "observability")
        logs_for(f'cluster="{target}", namespace="observability", service="alloy"', "collector error lines")
        sig["ongoing"] = sig["telemetry_age"] is None or sig["telemetry_age"] > 120 or sig["remote_write_failed"] > 0 or sig["log_dropped"] > 0

    # --- generic extras for every family: traces are looked up only when the workload speaks HTTP and is traced
    if workload and family in ("container", "rollout", "ingress"):
        q_err = f'{{ resource.service.name = "{sig.get("backend", workload)}" && status = error }}'
        errs = tools.call_tool("search_traces", {"traceql": q_err, "minutes": lookback_minutes, "limit": 5})
        sig["error_traces"] = len(errs.get("traces", [])) if "error" not in errs else 0
        sig["has_traces"] = sig["error_traces"] > 0
        add("tempo", q_err, f"{sig['error_traces']} error traces in the window")

    sig.setdefault("has_traces", None)
    return {"service": workload or labels.get("alertname", ""), "namespace": ns, "started": started, "now": now, "signals": sig, "evidence": evidence, "raw": raw, "family": "generic"}


# ---------------------------------------------------------------------------------------------------------------- diagnose
def _manifest(sig: dict, default: str) -> str:
    files = sig.get("source_files") or []
    return ", ".join(f"`{f}`" for f in files) or default


def _msgs(sig: dict) -> str:
    return " ".join(m for m, _ in sig.get("top_log_messages", [])).lower()


def _ev(sig: dict, *reasons: str) -> list[dict]:
    return [e for e in sig.get("events", []) if e["reason"] in reasons]


def _bigger(limit: str | None) -> str:
    m = re.fullmatch(r"(\d+)(Mi|Gi)", limit or "")
    if not m:
        return "about double the current limit"
    return f"{int(m.group(1)) * (1024 if m.group(2) == 'Gi' else 1) * 2}Mi"


def _ctl(cluster: str) -> str:
    return f"kubectl --context kind-{cluster}"


def diagnose(alert: dict, ctx: dict) -> Diag:
    sig, labels = ctx["signals"], ctx["signals"]["labels"]
    family, cluster, ns, wl = sig["family"], sig["cluster"], sig["namespace"], sig["workload"]
    alertname = labels.get("alertname", "")
    pods = sig.get("pods", [])
    k = _ctl(cluster)
    manifest = _manifest(sig, f"the manifest of `{wl}` in Git")
    pod = pods[0] if pods else {}

    # ------------------------------------------------------------------ containers that cannot start or keep dying
    if family in ("container", "job", "rollout", "target", "scheduling", "memory", "cpu") and (family in ("memory", "cpu") or labels.get("reason") or pod.get("waiting") or sig.get("restarts") or _ev(sig, "Failed", "BackOff", "FailedScheduling", "Unhealthy", "FailedMount")):
        reason = labels.get("reason") or pod.get("waiting") or ""
        msgs = _msgs(sig)
        exit_code = (pod.get("last_terminated") or {}).get("exit_code")
        term = (pod.get("last_terminated") or {}).get("reason")
        top = sig["top_log_messages"][0][0][:140] if sig.get("top_log_messages") else ""
        where = f"`{ns}/{wl}` in the {cluster} cluster"

        sched = _ev(sig, "FailedScheduling")
        if family == "scheduling" or (sched and not pods):
            text = (sched[0]["message"] if sched else "").lower()
            if "insufficient" in text:
                res = "cpu" if "insufficient cpu" in text else "memory"
                return Diag("scheduling_failure", "infrastructure", f"capacity of the {cluster} cluster ({res})", "high",
                            f"No node has enough {res} for the pod: {sched[0]['message'][:160]}.",
                            f"The scheduler cannot place a pod of {where} because no node has enough free {res}. The pod stays Pending, so the workload runs with fewer replicas than wanted. Node usage at the time: CPU {sig.get('node_cpu', 0):.0%}, memory {sig.get('node_mem', 0):.0%} of allocatable.",
                            ["FailedScheduling event: " + sched[0]["message"][:160], "the problem is cluster capacity, not the workload's code"],
                            [f"{k} describe nodes | grep -A8 'Allocated resources'  # see what is requested on each node", f"Free capacity: scale down or delete unneeded workloads, or add a node (kind: add a worker to bootstrap/kind-config*.yaml and recreate; cloud: scale the node pool)"],
                            [f"Right-size requests of `{wl}` in {manifest}: requests are what the scheduler reserves", "Add a Kyverno/LimitRange rule that caps requests, and alert on `kube_node_status_allocatable` vs requested at 85%"],
                            [f"{k} -n {ns} get pods -l app.kubernetes.io/name={wl}  # all Running"],
                            f"{wl} is below its desired replicas until capacity is added.")
            if "taint" in text or "affinity" in text or "selector" in text:
                return Diag("scheduling_failure", "application", f"scheduling constraints of {wl}", "high", f"The pod's node selector, affinity or tolerations match no node: {sched[0]['message'][:160]}.",
                            f"The pod of {where} cannot be placed because its scheduling constraints (nodeSelector, affinity or tolerations) match no node, so it stays Pending.",
                            ["FailedScheduling event names a taint, affinity or selector", "the workload spec asks for something no node offers"],
                            [f"{k} -n {ns} describe pod {pod.get('pod', wl)} | grep -A5 Events"], [f"Fix the constraint in {manifest}, or label/taint the nodes to match"], [f"{k} -n {ns} get pods  # Running"], f"{wl} cannot start.")
            if "persistentvolumeclaim" in text or "volume" in text:
                return Diag("scheduling_failure", "infrastructure", "storage for the pod's volume", "medium", f"The pod waits for a volume: {sched[0]['message'][:160]}.",
                            f"The pod of {where} cannot be scheduled because its PersistentVolumeClaim is not bound or the volume lives on another node.",
                            ["FailedScheduling event mentions a volume or claim"], [f"{k} -n {ns} get pvc,pv", f"{k} get storageclass  # is there a default class with a provisioner"], ["Make sure the storage class can provision (cloud: use a zonal class and WaitForFirstConsumer)"], [f"{k} -n {ns} get pvc  # Bound"], f"{wl} cannot start without its volume.")

        pull = _ev(sig, "Failed", "ErrImagePull", "ImagePullBackOff") if reason in ("ImagePullBackOff", "ErrImagePull", "InvalidImageName") else []
        if reason in ("ImagePullBackOff", "ErrImagePull", "InvalidImageName"):
            m = (pull[0]["message"] if pull else "").lower()
            if "not found" in m or "manifest unknown" in m or "invalid" in reason.lower():
                comp, layer, why = "image reference of the workload (tag or name does not exist)", "application", "the registry says the image or tag does not exist"
                fix = [f"Find the broken reference: {k} -n {ns} get pod {pod.get('pod', '')} -o jsonpath='{{.spec.containers[*].image}}'", f"Point {manifest} at an image that exists (check the CI run that builds it) and let Argo CD sync", "Or revert the commit that changed the image"]
            elif "unauthorized" in m or "denied" in m or "authentication" in m:
                comp, layer, why = "registry credentials (imagePullSecret)", "infrastructure", "the registry refused the credentials or the image is private"
                fix = ["Create or fix the image pull secret in the namespace and reference it in the pod spec (cloud: use workload identity / managed identity, not a static secret)"]
            else:
                comp, layer, why = f"network path to the registry from the {cluster} cluster", "infrastructure", "the node cannot reach the registry (timeout, DNS or rate limit)"
                fix = [f"From a node: docker exec {cluster}-control-plane crictl pull <image>  # reproduces the error", "Check DNS and egress; if rate limited, use a pull-through cache or authenticated pulls"]
            return Diag("image_pull_failure", layer, comp, "high" if pull else "medium",
                        f"The container image cannot be pulled ({reason}): {why}.",
                        f"Pods of {where} cannot start because their image cannot be pulled ({reason}): {why}. The message from the kubelet: {pull[0]['message'][:200] if pull else 'not captured'}. No new pod becomes ready, so a rollout stalls and replicas that die are not replaced.",
                        ["waiting reason " + reason, (pull[0]["message"][:160] if pull else "no pull event captured")] + ([f"a deploy ({sig['recent_deploy']['revision']}) landed in the last hour"] if sig.get("recent_deploy") else []),
                        fix, ["Pin images by digest and make CI fail when the tag it writes into Git does not exist in the registry", "Add a pull-through cache so a registry outage does not stop restarts"],
                        [f"{k} -n {ns} get pods  # no ImagePullBackOff, pods Running"], f"{wl} cannot roll out or recover from restarts.")

        if reason == "CreateContainerConfigError" or "secret" in msgs and "not found" in msgs or re.search(r"(secret|configmap) \"[^\"]+\" not found", " ".join(e["message"] for e in sig.get("events", [])).lower()):
            evt = " ".join(e["message"] for e in sig.get("events", []))
            m = re.search(r"(secret|configmap) \"([^\"]+)\" not found", evt, re.I)
            what = f"{m.group(1)} `{m.group(2)}`" if m else "a Secret or ConfigMap it references"
            return Diag("config_error", "application", f"missing {what} in namespace {ns}", "high" if m else "medium", f"The pod references {what}, which does not exist.",
                        f"Containers of {where} cannot be created because {what} is missing in namespace {ns}. This is a configuration problem of the workload or of how its secrets are provisioned.",
                        ["waiting reason CreateContainerConfigError or events naming a missing secret/configmap", "the cluster and node are healthy, the pod spec refers to something that does not exist"],
                        [f"{k} -n {ns} get secret,configmap | grep {m.group(2) if m else '<name>'}", "Create it (secrets: re-run bootstrap.py for the ones it seeds, or restore from the secret manager)"],
                        ["Provision secrets with the secret manager (External Secrets / Sealed Secrets) and make the app sync wait for them (sync waves)"], [f"{k} -n {ns} get pods  # Running"], f"{wl} cannot start.")

        if term == "OOMKilled" or (exit_code == 137 and sig.get("mem_ratio") and sig["mem_ratio"] > 0.85) or labels.get("alertname") == "ContainerMemoryNearLimit" or family == "memory":
            leak = bool(sig.get("mem_growth") and sig["mem_growth"] > 1.3)
            limit = pod.get("memory_limit")
            if leak:
                return Diag("resource_exhaustion", "application", f"memory growth in {wl} (leak or unbounded cache)", "medium",
                            f"Memory of {wl} keeps growing ({sig['mem_growth']:.1f}x in the window) until the {limit or 'configured'} limit kills it.",
                            f"{where} uses more memory every minute (x{sig['mem_growth']:.1f} over the window) and is {'OOMKilled' if term == 'OOMKilled' else 'close to its limit'}. Raising the limit would only delay the kill, so this points at the application's memory behaviour.",
                            [f"memory grew {sig['mem_growth']:.1f}x without levelling off", "restarts reset the growth, which repeats"],
                            [f"Buy time: raise the memory limit of `{wl}` in {manifest} (suggested {_bigger(limit)}) and sync", f"Capture the cause: {k} -n {ns} logs {pod.get('pod', wl)} --previous | tail -50"],
                            ["Find the leak with profiling (add Pyroscope or a heap profile endpoint), bound caches and queues, and set a limit with 25% headroom over the steady state"],
                            [f"max(container_memory_working_set_bytes{{cluster=\"{cluster}\",namespace=\"{ns}\",pod=~\"{wl}-.*\"}}) stays flat for 30 minutes"], f"{wl} restarts repeatedly and drops requests while it does.")
            return Diag("resource_exhaustion", "infrastructure", f"memory limit of {wl} ({limit or 'unknown'}) is below what it needs", "high" if term == "OOMKilled" else "medium",
                        f"{wl} needs more memory than its limit ({limit or 'unknown'}) allows.",
                        f"{where} uses {(sig.get('mem_ratio') or 0):.0%} of its memory limit ({limit or 'unknown'}){' and was OOMKilled' if term == 'OOMKilled' else ''}. Memory is flat rather than climbing, so the limit is simply too small for the workload.",
                        [f"memory at {(sig.get('mem_ratio') or 0):.0%} of the limit", "usage plateaus instead of growing, which is sizing, not a leak"],
                        [f"Raise the memory limit of `{wl}` in {manifest} to {_bigger(limit)} (requests too) and let Argo CD sync"], ["Size limits from observed peak plus 25%, and keep the alert ContainerMemoryNearLimit as the early warning"],
                        [f"{k} -n {ns} get pods  # no new OOMKilled, restarts stop increasing"], f"{wl} restarts while it hits the limit.")

        if family == "cpu" or alertname == "ContainerCpuThrottled":
            return Diag("resource_exhaustion", "infrastructure", f"CPU limit of {wl}", "high" if sig.get("throttle", 0) > 0.5 else "medium", "The container is throttled by its CPU limit.",
                        f"{where} is CPU throttled in up to {sig.get('throttle', 0):.0%} of scheduling periods. The kernel pauses the container once it uses its limit, which adds latency to every request without any error showing.",
                        [f"throttled periods up to {sig.get('throttle', 0):.0%}", "no restarts needed: the symptom is latency"], [f"Raise or remove the CPU limit of `{wl}` in {manifest} (keep a request), or add replicas"],
                        ["Prefer CPU requests without limits for latency-sensitive services, and scale on CPU with an HPA"], [f"rate(container_cpu_cfs_throttled_periods_total{{cluster=\"{cluster}\",namespace=\"{ns}\",pod=~\"{wl}-.*\"}}[5m]) is near 0"], f"{wl} responds slower than it should.")

        if reason == "CrashLoopBackOff" or sig.get("restarts") or term in ("Error", "Completed"):
            cfg = re.search(r"permission denied|read-only file system|eacces|erofs", msgs)
            dns = re.search(r"name resolution|no such host|could not translate|temporary failure in name", msgs)
            conn = re.search(r"connection refused|connect: |dial tcp|timed out|timeout|no route to host|unavailable", msgs)
            probe = _ev(sig, "Unhealthy")
            if cfg:
                return Diag("config_error", "application", f"filesystem permissions of {wl}", "high", f"The process fails on a permission or read-only filesystem error: {top}.",
                            f"{where} keeps crashing (exit {exit_code}) because it cannot write where it tries to: `{top}`. The pod runs with a read-only root filesystem and a non-root user, so every path it writes must be a mounted volume.",
                            ["log lines show permission denied / read-only file system", "restarts began when this build started"], [f"Mount an emptyDir for the path in the log line in {manifest}", f"{k} -n {ns} logs {pod.get('pod', wl)} --previous | tail -30"],
                            ["Test the image with the same securityContext in CI (docker run --read-only --user 10001)"], [f"{k} -n {ns} get pods  # Running, restarts stable"], f"{wl} cannot stay up.")
            if dns:
                return Diag("dns_failure", "infrastructure", f"DNS resolution in the {cluster} cluster", "medium", f"The process cannot resolve a name: {top}.",
                            f"{where} crashes because name resolution fails ({top}). Other pods are probably affected too.",
                            ["log lines show name resolution failures"], [f"{k} -n kube-system get pods -l k8s-app=kube-dns", f"{k} -n kube-system logs -l k8s-app=kube-dns --tail=30"], ["Alert on CoreDnsErrors; check the network policy allows egress to kube-dns on 53"], [f"{k} -n {ns} run dnscheck --rm -it --image=busybox:1.37.0 --restart=Never -- nslookup kubernetes.default"], f"{wl} cannot reach anything by name.")
            if probe and not top:
                return Diag("probe_failure", "application", f"health probe of {wl}", "medium", "The liveness or readiness probe fails and the kubelet restarts the container.",
                            f"{where} is restarted by the kubelet because its probe fails: {probe[0]['message'][:160]}.", ["Unhealthy events from the kubelet"], [f"{k} -n {ns} describe pod {pod.get('pod', wl)} | grep -B2 -A6 Liveness"],
                            ["Give the probe a startupProbe and realistic timeouts, and make the endpoint cheap"], [f"{k} -n {ns} get pods  # restarts stable"], f"{wl} restarts and drops traffic.")
            if conn:
                return Diag("dependency_unreachable", "application", f"a dependency {wl} needs at start-up", "medium", f"The process fails to reach something it depends on: {top}.",
                            f"{where} exits (code {exit_code}) because a connection fails: `{top}`. The process cannot start without that dependency, and Kubernetes keeps restarting it with a growing delay (CrashLoopBackOff).",
                            ["connection errors in the logs before each exit", "no node or resource problem found"], [f"{k} -n {ns} logs {pod.get('pod', wl)} --previous | tail -30  # name the dependency", "Check that the dependency is running and that a NetworkPolicy allows the traffic"],
                            ["Make the app retry with backoff instead of exiting, and add a startupProbe"], [f"{k} -n {ns} get pods  # Running"], f"{wl} is down until its dependency answers.")
            return Diag("pod_crash", "application", f"process of {wl} exits with code {exit_code}", "medium" if top else "low", f"The container keeps exiting ({term or 'Error'}, code {exit_code}): {top or 'no error line captured'}.",
                        f"{where} is in a restart loop. The last exit was {term or 'Error'} with code {exit_code}" + (f" and the most frequent error line is `{top}`" if top else " and its logs hold no error line") + ". Nothing in the cluster explains it (nodes are healthy, memory is not at the limit), so the cause is inside the process.",
                        [f"exit code {exit_code}", "node and resource signals are normal"] + ([f"a deploy ({sig['recent_deploy']['revision']}) landed within the hour"] if sig.get("recent_deploy") else []),
                        [f"{k} -n {ns} logs {pod.get('pod', wl)} --previous | tail -50", ("Roll back the last change: revert the commit that changed " + manifest) if sig.get("recent_deploy") else f"Check the recent change of {manifest}"],
                        ["Add the failing path to CI (a smoke test that starts the container with the production settings)"], [f"{k} -n {ns} get pods  # Running and restarts stable for 10 minutes"], f"{wl} has fewer ready replicas than desired.")

    # ------------------------------------------------------------------ volumes
    if family == "volume":
        free = sig.get("free_ratio") or 0.0
        growth = sig.get("growth_per_hour")
        pvc = labels.get("persistentvolumeclaim", wl)
        eta = ""
        if growth and growth > 0 and sig.get("cap_bytes") and sig.get("used_bytes") is not None:
            hours = (sig["cap_bytes"] - sig["used_bytes"]) / growth
            eta = f" At the current growth it is full in about {hours * 60:.0f} minutes."
        return Diag("volume_full", "infrastructure", f"capacity of volume {pvc}", "high", f"Volume {pvc} has {free:.1%} free.",
                    f"The volume `{ns}/{pvc}` in the {cluster} cluster is {1 - free:.1%} full ({', '.join(sig.get('volume_pods', [])) or 'no running pod'} writes to it).{eta} When it fills up, writes fail and the workload errors or crashes.",
                    [f"{free:.1%} free", "usage grows steadily" if growth and growth > 0 else "usage is not shrinking"],
                    [f"Free space now: remove data the workload no longer needs (retention job, old reports/blocks)", f"Or grow the claim: {k} -n {ns} patch pvc {pvc} -p '{{\"spec\":{{\"resources\":{{\"requests\":{{\"storage\":\"<bigger>\"}}}}}}}}'  # needs a StorageClass with allowVolumeExpansion"],
                    ["Set retention on whatever fills it (Loki/Mimir/Tempo retention, report cleanup) and alert at 10% free (PersistentVolumeFillingUp) before 3% (page)"], [f"min(kubelet_volume_stats_available_bytes{{persistentvolumeclaim=\"{pvc}\"}}/kubelet_volume_stats_capacity_bytes{{persistentvolumeclaim=\"{pvc}\"}}) is above 0.25"],
                    f"Writers to {pvc} will fail when it is full.")

    # ------------------------------------------------------------------ nodes
    if family == "node":
        node = labels.get("node", wl)
        users = sig.get("user_pods_on_node", [])
        blast = f"{sig.get('pods_on_node', 0)} pods run there" + (f", including {', '.join(users)}" if users else "")
        if sig.get("node_not_ready"):
            return Diag("node_failure", "infrastructure", f"node {node} of the {cluster} cluster", "high", f"Node {node} is NotReady.",
                        f"Node `{node}` in the {cluster} cluster stopped reporting Ready, so its pods are not served and will be evicted after the grace period ({blast}). The kubelet is down, the node lost its network, or (in kind) the node container stopped.",
                        ["kube_node_status_condition Ready is false", "this is below the workload layer: every pod on the node is affected"],
                        [f"{k} describe node {node} | grep -A10 Conditions", f"kind: docker ps -a --filter name={node}  # then docker start {node}", f"Cloud: cordon and drain, then replace the instance"],
                        ["Run at least 2 replicas spread over nodes (topologySpreadConstraints), and use node auto-repair in the cloud"], [f"{k} get nodes  # {node} Ready", f"{k} get pods -A -o wide | grep {node}"],
                        blast)
        if sig.get("node_pressure"):
            cond = sig["node_pressure"][0]
            hint = {"MemoryPressure": f"biggest memory users: {', '.join(sig.get('top_memory_pods', [])[:3])}", "DiskPressure": "image layers, container logs or emptyDir volumes fill the node disk", "PIDPressure": "a workload spawns too many processes"}[cond]
            return Diag("node_pressure", "infrastructure", f"{cond} on node {node}", "high", f"Node {node} reports {cond}.",
                        f"Node `{node}` ({cluster}) reports {cond}: the kubelet starts evicting pods to protect the node ({blast}). {hint}.",
                        [f"kubelet condition {cond}=true", hint], [f"{k} describe node {node} | grep -A12 Conditions", "Move or restart the biggest users; for disk: prune images (crictl rmi --prune) and clean log volumes"],
                        ["Set memory limits on every container, add node capacity, and add an eviction-aware PodDisruptionBudget"], [f"{k} describe node {node} | grep -i pressure  # all False"], blast)
        return Diag("resource_exhaustion", "infrastructure", f"{'CPU' if alertname == 'NodeCpuSaturated' else 'memory'} capacity of node {node}", "medium", f"Node {node} is close to its capacity.",
                    f"Node `{node}` in the {cluster} cluster uses {sig.get('node_cpu', 0):.0%} of its CPU and {sig.get('node_mem', 0):.0%} of its memory ({blast}). Pods compete for the node and the next spike evicts or throttles them. Largest memory users: {', '.join(sig.get('top_memory_pods', [])[:3]) or 'unknown'}.",
                    [f"CPU {sig.get('node_cpu', 0):.0%}, memory {sig.get('node_mem', 0):.0%} of allocatable"], ["Spread load: add a node, or move the heaviest pod elsewhere (nodeAffinity / topology spread)", f"{k} top pods -A --sort-by=memory | head"],
                    ["Right-size requests so the scheduler packs nodes evenly; alert on requested vs allocatable"], [f"{k} top node  # below 80%"], blast)

    # ------------------------------------------------------------------ ingress
    if family == "ingress":
        ing = labels.get("ingress", wl)
        backend = sig.get("backend", ing)
        ready = [p for p in pods if p.get("ready")]
        top = sig["top_log_messages"][0][0][:140] if sig.get("top_log_messages") else ""
        shape = f"{sig.get('error_ratio_peak', 0):.0%} of requests through `{ing}` ended in 5xx (statuses {sig.get('statuses')}) and p95 reached {sig.get('p95_peak', 0) * 1000:.0f} ms"
        if pods and not ready:
            return Diag("ingress_backend_failure", "application" if not sig.get("node_not_ready") else "infrastructure", f"no ready pod behind service {backend}", "high", f"Users get errors because no pod of {backend} is ready.",
                        f"{shape}. The backend `{ns}/{backend}` has no ready pod, so the ingress controller has nothing to send traffic to (502/503). The pods' state: " + ", ".join(f"{p['pod']} {p['phase']}{' ' + p['waiting'] if p.get('waiting') else ''}" for p in pods[:3]) + ".",
                        ["ingress error rate matches the time the backend became unready", "backend pods not ready"], [f"{k} -n {ns} get pods -l app.kubernetes.io/name={backend}", f"{k} -n {ns} describe pod <pod> | tail -20", "Follow the playbook of the backend's own alert (crash loop, image pull, scheduling)"],
                        ["Run 2+ replicas with a PodDisruptionBudget and readiness probes that reflect real readiness"], [f"{k} -n {ns} get endpoints {backend}  # has addresses", "5xx ratio on the ingress below 1%"], f"Users of {ing} see errors until {backend} has a ready pod.")
        if sig.get("error_ratio_peak", 0) > 0.05:
            return Diag("ingress_backend_failure", "application", f"errors returned by service {backend}", "medium" if top else "low", f"The backend answers with errors: {top or 'see the evidence'}.",
                        f"{shape}. The backend `{ns}/{backend}` is running ({len(ready)} ready pods) but returns the errors itself, so the cause is in the application or its dependencies" + (f"; its most frequent error is `{top}`" if top else "") + ".",
                        [f"{len(ready)} ready backend pods, so the controller reaches them", "errors originate in the backend"] + ([f"a deploy ({sig['recent_deploy']['revision']}) landed in the last hour"] if sig.get("recent_deploy") else []),
                        [f"{k} -n {ns} logs -l app.kubernetes.io/name={backend} --tail=50", ("Roll back the last deploy: revert the commit that changed " + manifest) if sig.get("recent_deploy") else f"Check the dependencies named in the backend's error lines"],
                        ["Give the backend its own SLO alert (RED metrics) so this is caught before the ingress sees it"], ["5xx ratio on the ingress below 1%"], f"Users of {ing} get errors.")
        return Diag("ingress_backend_failure", "application", f"latency of service {backend}", "medium", f"Requests through {ing} are slow.",
                    f"{shape}. The ingress controller itself is healthy; the time is spent in the backend `{ns}/{backend}`.", ["ingress p95 high while errors are low"], [f"{k} -n {ns} top pods -l app.kubernetes.io/name={backend}", "Look at the backend's traces for the slowest span"],
                    ["Add RED metrics and tracing to the backend"], ["p95 on the ingress below 500 ms"], f"Users of {ing} wait longer than usual.")

    # ------------------------------------------------------------------ Argo CD
    if family == "argocd":
        app = sig.get("app") or {}
        name = labels.get("name", wl)
        op = (app.get("operation") or {}).get("message", "") or " ".join(app.get("conditions", []))
        low = op.lower()
        dest = f"{app.get('destination', '?')}/{app.get('destination_namespace', '?')}"
        if re.search(r"unable to connect|connection refused|x509|i/o timeout|no such host|cluster .* not found|dial tcp", low):
            return Diag("gitops_sync_failure", "infrastructure", f"connection from Argo CD to cluster {app.get('destination')}", "high", "Argo CD cannot reach the destination cluster.",
                        f"Argo CD cannot sync `{name}` because it cannot talk to the cluster `{app.get('destination')}`: {op[:160]}. Everything deployed to that cluster is frozen (not updated, not self-healed) until the connection works.",
                        ["the sync error is a connection or certificate error", "the application's Git source is fine"], ["argocd cluster list  # or: kubectl -n argocd get secret -l argocd.argoproj.io/secret-type=cluster", "Re-register the cluster: python3 bootstrap/bootstrap.py --only <cluster>"],
                        ["Alert on argocd_cluster_connection_status; use a stable DNS name and rotate the cluster token before it expires"], [f"argocd app get {name}  # Synced and Healthy"], f"Deployments to {app.get('destination')} are blocked.")
        if re.search(r"rendering|helm|kustomize|manifest generation|invalid|yaml|template|unable to resolve|repository|authentication|git", low) and app.get("sync_status") in ("Unknown", "OutOfSync") or "comparisonerror" in low:
            return Diag("gitops_sync_failure", "application", f"manifests of {name} in Git", "high", f"Argo CD cannot render the application: {op[:160]}.",
                        f"Argo CD cannot generate or compare the manifests of `{name}` (destination {dest}): {op[:200]}. The live state stays as it was; the last change in Git is not applied. This is a content problem in the repository, not in the cluster.",
                        ["ComparisonError / manifest generation error from the repo server", "the destination cluster is reachable"], [f"Reproduce locally: kustomize build or helm template for the app path ({', '.join(app.get('paths', [])) or 'see the Application'})", "Fix the file named in the error and push; or revert the last commit"],
                        ["CI already renders manifests (kubeconform): add this app's helm/kustomize render to it"], [f"argocd app get {name}  # no ComparisonError"], f"Changes to {name} are not rolling out.")
        if re.search(r"admission webhook|denied the request|violates podsecurity|forbidden|validation|policy", low):
            return Diag("gitops_sync_failure", "application", f"a policy rejects the manifests of {name}", "high", f"The cluster refuses the resources: {op[:160]}.",
                        f"The sync of `{name}` (destination {dest}) is rejected by an admission rule: {op[:200]}. The resource violates Pod Security or a Kyverno policy, so it is not created.",
                        ["the error comes from admission (Pod Security / Kyverno / RBAC)", "Git and the cluster connection are fine"], [f"Read the policy message, then change the manifest to comply (securityContext, limits, tag)", f"Check: {k} get clusterpolicy; {k} get events -A | grep -i violat"],
                        ["Run the Kyverno CLI against the manifests in CI so the PR fails before it merges"], [f"argocd app sync {name}  # succeeds"], f"{name} cannot be updated.")
        if app.get("health_status") in ("Degraded", "Missing") and app.get("unhealthy"):
            return Diag("deploy_regression", "application", f"resources of {name}: {', '.join(app['unhealthy'][:3])}", "medium", f"The application deployed but its resources are unhealthy: {', '.join(app['unhealthy'][:2])}.",
                        f"`{name}` is synced from Git but its health is {app.get('health_status')}: {', '.join(app['unhealthy'][:4])} (destination {dest}). The Git change itself applied; what it deployed does not run correctly" + (f". Recent deploy {sig['recent_deploy']['revision']}." if sig.get("recent_deploy") else "."),
                        ["sync is fine, health is not", "see the pod status and events of the unhealthy resource in the evidence"], [f"Look at the unhealthy resource ({app['unhealthy'][0]}): its pods, events and logs", "Revert the last commit to the app if it started with that deploy"],
                        ["Use sync waves and progressive delivery so a bad release stops early"], [f"argocd app get {name}  # Healthy"], f"{name} is partly or fully down.")
        return Diag("gitops_sync_failure", "application", f"drift between Git and {name}", "medium", f"{name} is {app.get('sync_status', 'not synced')}: live state differs from Git.",
                    f"`{name}` (destination {dest}) has been {app.get('sync_status', 'out of sync')} for a while. Either automated sync is paused, a sync keeps failing, or someone changed the live objects by hand and self-heal is not reverting them. Operation: {op[:160] or 'none recorded'}.",
                    ["sync status is not Synced", "no connection or rendering error found"], [f"argocd app diff {name}  # shows exactly what differs", f"argocd app sync {name}"], ["Keep automated sync with selfHeal on and forbid manual kubectl edits (RBAC)"], [f"argocd app get {name}  # Synced"], f"Changes in Git may not be live for {name}.")

    # ------------------------------------------------------------------ control plane and DNS
    if family == "dns":
        return Diag("dns_failure", "infrastructure", f"CoreDNS of the {cluster} cluster", "medium", f"CoreDNS answers SERVFAIL for {sig.get('dns_servfail', 0):.0%} of lookups.",
                    f"DNS lookups fail in the {cluster} cluster: {sig.get('dns_servfail', 0):.0%} of answers are SERVFAIL. Services cannot find each other or external hosts, so errors appear in many workloads at once. CoreDNS pods: " + (", ".join(f"{p['pod']} {p['phase']} restarts {p['restarts']}" for p in pods[:3]) or "not found") + ".",
                    [f"SERVFAIL ratio {sig.get('dns_servfail', 0):.0%}", "many workloads affected at once points to the platform"], [f"{k} -n kube-system logs -l k8s-app=kube-dns --tail=40", f"{k} -n kube-system rollout restart deploy/coredns  # if pods are stuck"],
                    ["Run 2+ CoreDNS replicas, add NodeLocal DNSCache, and check upstream resolvers"], [f"{k} run dnscheck --rm -it --image=busybox:1.37.0 --restart=Never -- nslookup kubernetes.default", "SERVFAIL ratio below 1%"], "Name resolution is failing for every workload of the cluster.")
    if family == "controlplane":
        why = "etcd is slow" if sig.get("etcd_p99", 0) > 0.5 else ("a node of the control plane is under pressure" if sig.get("node_pressure") else "no single component stands out")
        return Diag("control_plane_degraded", "infrastructure", f"API server of the {cluster} cluster", "medium", f"The API server of {cluster} returns errors or answers slowly ({why}).",
                    f"The API server of the {cluster} cluster has a 5xx ratio of {sig.get('apiserver_error_ratio', 0):.1%} and p99 {sig.get('apiserver_p99', 0) * 1000:.0f} ms (etcd p99 {sig.get('etcd_p99', 0) * 1000:.0f} ms). Controllers, deployments, Argo CD and every kubectl call are affected; running pods keep serving, but nothing can change.",
                    [f"API 5xx {sig.get('apiserver_error_ratio', 0):.1%}", why], [f"{k} -n kube-system get pods -l component=kube-apiserver", f"docker exec {cluster}-control-plane crictl logs $(docker exec {cluster}-control-plane crictl ps -a --name kube-apiserver -q | head -1) | tail -30", "Check the control plane node for disk or memory pressure"],
                    ["Run an HA control plane in the cloud, and alert on etcd latency and database size"], [f"{k} get --raw /readyz  # ok"], "No deployments or scaling can happen until the API server recovers.")

    # ------------------------------------------------------------------ telemetry
    if family == "telemetry":
        target = labels.get("cluster", cluster)
        pod_ok = any(p.get("ready") for p in pods)
        if pods and not pod_ok:
            return Diag("telemetry_gap", "infrastructure", f"the collector (Alloy) of the {target} cluster", "high", f"The collector in {target} is not running.",
                        f"No telemetry arrives from the {target} cluster because its collector is not ready ({', '.join(f'{p['pod']} {p['phase']}' for p in pods[:2])}). Dashboards and alerts for that cluster are blind until it is back.",
                        ["collector pod not ready", "the cluster API answers, so the cluster itself is up"], [f"{_ctl(target)} -n observability describe pod -l app.kubernetes.io/name=alloy | tail -20", f"{_ctl(target)} -n observability logs -l app.kubernetes.io/name=alloy --tail=40"],
                        ["Run the collector with a PodDisruptionBudget and alert on its absence from a second place"], [f"up{{job=\"alloy\",cluster=\"{target}\"}} == 1"], f"Nothing can be detected in {target} while it is blind.")
        if (sig.get("telemetry_age") is None or sig.get("telemetry_age", 0) > 120) and "error" in (raw := ctx["raw"].get("pods", {})):
            return Diag("telemetry_gap", "infrastructure", f"the {target} cluster or the network path to it", "medium", f"The {target} cluster is unreachable or down.",
                        f"No metrics arrive from {target} and its API does not answer ({raw.get('error', '')[:100]}). The cluster is down, stopped, or cut off from the network.",
                        ["no samples from the collector", "the API server of the cluster is unreachable too"], [f"docker ps -a --filter name={target}-  # kind: are the node containers running?", f"docker start {target}-control-plane  # if stopped"], ["Alert on the absence of a cluster from a place outside it"], [f"{_ctl(target)} get nodes"], f"Everything in {target} is unobserved, and possibly down.")
        return Diag("telemetry_gap", "infrastructure", f"delivery from {target} to the observability cluster", "medium", "The collector runs but its data does not arrive.",
                    f"The collector in {target} is running but writes fail (metrics failing {sig.get('remote_write_failed', 0):.2f}/s, logs dropped {sig.get('log_dropped', 0):.2f}/s). Mimir or Loki is down, overloaded or unreachable, or a network policy blocks the NodePort.",
                    ["remote write failures or dropped log lines"], [f"{_ctl('observability')} -n observability get pods  # Mimir and Loki Running?", f"{_ctl(target)} -n observability logs -l app.kubernetes.io/name=alloy --tail=40"],
                    ["Add a retry queue sized for 15 minutes, and alert on the failure rate (TelemetryNotDelivered)"], ["rate(prometheus_remote_storage_samples_failed_total[5m]) is 0"], f"Data from {target} is lost while writes fail.")

    # ------------------------------------------------------------------ fallback
    return Diag("unknown", "unknown", f"{wl or alertname}", "low", f"{alertname} fired but the collected signals do not point to a single cause.",
                f"The alert {alertname} fired for `{ns + '/' if ns else ''}{wl}` in the {cluster} cluster. The metrics, logs and events gathered do not isolate a cause; see the evidence below for what was checked.",
                ["no signal stood out"], [f"{k} -n {ns or 'default'} get pods,events", f"Open the dashboard 'Workload detail' for {wl}"], ["Add the missing signal named in the observability gaps"], ["the alert resolves"], "Unknown.")


# ---------------------------------------------------------------------------------------------------------------- priority, gaps, report
def _priority(alert: dict, ctx: dict, d: Diag) -> Priority:
    sig = ctx["signals"]
    labels = sig["labels"]
    severity = labels.get("severity", "ticket")
    level = 2 if severity == "page" else 3
    why = [f"alert severity is '{severity}'"]
    ns, wl, family = sig["namespace"], sig["workload"], sig["family"]
    user_facing = wl in config.USER_FACING_SERVICES or sig.get("backend") in config.USER_FACING_SERVICES or ns in ("demo",) or any(p.startswith(tuple(config.USER_FACING_SERVICES)) for p in sig.get("user_pods_on_node", []))
    ready = [p for p in sig.get("pods", []) if p.get("ready")]
    nothing_serving = bool(sig.get("pods")) and not ready

    if user_facing:
        why.append(f"`{wl}` or something on this path serves users")
        if nothing_serving or sig.get("error_ratio_peak", 0) >= 0.5 or sig.get("node_not_ready") and sig.get("user_pods_on_node"):
            level = 1
            why.append("it is effectively down: " + ("no pod is ready" if nothing_serving else f"{sig.get('error_ratio_peak', 0):.0%} of requests fail" if sig.get("error_ratio_peak", 0) >= 0.5 else "the node under user pods is gone"))
        elif d.category in ("image_pull_failure", "pod_crash", "scheduling_failure", "config_error") and ready:
            why.append(f"{len(ready)} replica(s) still serve, so users see reduced capacity, not an outage")
    else:
        why.append("no direct user traffic depends on it" if family not in ("controlplane", "dns", "telemetry", "argocd") else "it is platform infrastructure")

    if family in ("controlplane", "dns") and d.category in ("control_plane_degraded", "dns_failure"):
        level = 1 if (sig.get("apiserver_error_ratio", 0) >= 0.5 or sig.get("dns_servfail", 0) >= 0.5) else min(level, 2)
        why.append("cluster-wide: every workload of the cluster depends on it")
    if family == "telemetry":
        level = min(level, 2)
        why.append("while telemetry is missing, other incidents in that cluster go unnoticed")
    if family == "argocd" and d.category == "gitops_sync_failure":
        level = max(level, 3) if not (d.layer == "infrastructure" and "cluster" in d.component) else min(level, 2)

    ongoing = bool(sig.get("ongoing", True))
    if not ongoing and level > 1:
        level = min(level + 1, 4)
        why.append("the signals have recovered, so the urgency is lower")
    elif ongoing:
        why.append("it is still ongoing")
    respond = {1: "now: page the on-call and open an incident", 2: "within 1 hour", 3: "next business day", 4: "backlog, no action needed unless it repeats"}[level]
    return Priority(level=f"P{level}", respond_within=respond, ongoing=ongoing, rationale=why)


def _gaps(ctx: dict, d: Diag) -> list[str]:
    sig = ctx["signals"]
    gaps: list[str] = []
    wl, ns, cluster = sig["workload"], sig["namespace"], sig["cluster"]
    if sig["family"] in ("container", "rollout", "target", "memory", "cpu", "ingress") and wl:
        if sig.get("log_total", 0) == 0 and d.category in ("pod_crash", "unknown", "ingress_backend_failure"):
            gaps.append(f"No error log lines for `{wl}` reached Loki: make sure the process logs to stdout/stderr and mark errors with a level (the collector derives the `level` label from JSON `level` or from the text)")
        elif sig.get("log_fields") and "trace_id" not in sig["log_fields"]:
            gaps.append(f"Log lines of `{wl}` have no `trace_id`: add it to the log context so a failing request links to its trace")
        elif sig.get("log_fields") and "request_id" not in sig["log_fields"]:
            gaps.append(f"Log lines of `{wl}` have no `request_id`: add one per request to follow it across services")
        if sig.get("has_traces") is False and sig["family"] == "ingress":
            gaps.append(f"No traces for `{sig.get('backend', wl)}`: instrument it with OpenTelemetry (OTEL_EXPORTER_OTLP_ENDPOINT=http://alloy.observability:4318) so slow or failing requests show the span that caused it")
        if not sig.get("source_files") and sig["family"] != "ingress":
            gaps.append(f"`{wl}` has no Argo CD application named prod-{wl} or {wl}: label deploys so the report can name the change and the file")
        if sig.get("mem_ratio") is None and sig["family"] in ("memory", "container"):
            gaps.append(f"No memory limit found for `{wl}`: set resources.limits.memory (Kyverno require-limits would enforce it) so OOM and near-limit alerts have a reference")
    if sig["family"] == "ingress" and not sig.get("rps"):
        gaps.append("The ingress controller reports no request rate: check that ingress-nginx metrics are scraped (container port named `metrics`)")
    if sig["family"] == "argocd" and not sig.get("app"):
        gaps.append("The agent could not read the Argo CD application: grant the gitops-reader token access to Applications")
    if sig.get("tool_errors"):
        gaps.append("Some lookups failed, so this report may be incomplete: " + "; ".join(sig["tool_errors"]))
    return gaps or ["The telemetry available was enough for this investigation."]


def analyze(alert: dict, ctx: dict, now: float | None = None) -> RcaReport:
    from agent import rca

    now = now or time.time()
    sig = ctx["signals"]
    labels = sig["labels"]
    d = diagnose(alert, ctx)
    started = ctx["started"]
    alertname = labels.get("alertname", "unknown")
    wl = sig["workload"] or alertname
    timeline = [TimelineEntry(time=_iso(started), event=f"alert {alertname} started firing")]
    if sig.get("recent_deploy"):
        timeline.append(TimelineEntry(time=sig["recent_deploy"]["deployed_at"], event=f"deploy {sig['recent_deploy']['revision']} synced by Argo CD"))
    for e in sig.get("events", [])[:4]:
        timeline.append(TimelineEntry(time=e["ts"], event=f"{e['reason']}: {e['object']} {e['message'][:80]}"))
    timeline.append(TimelineEntry(time=_iso(now), event="investigation completed"))
    timeline.sort(key=lambda t: t.time)
    priority = _priority(alert, ctx, d)
    contrib = list(d.contributing)
    if sig.get("recent_deploy"):
        contrib.append(f"a deploy ({sig['recent_deploy']['revision']}) landed within the hour before the alert")
    if sig.get("tool_errors"):
        contrib.append("some lookups failed, so this analysis may be incomplete: " + "; ".join(sig["tool_errors"]))
    report = RcaReport(
        id=f"rca-{re.sub(r'[^a-z0-9-]+', '-', wl.lower()).strip('-')[:40] or 'alert'}-{datetime.fromtimestamp(started, timezone.utc).strftime('%Y%m%d-%H%M%S')}",
        alert=alertname, service=wl, severity=labels.get("severity", "unknown"), started_at=_iso(started), generated_at=_iso(now), mode="rule_based",
        summary=f"{alertname} on {wl} ({sig['cluster']}): {d.description}",
        what_happened=d.what,
        priority=priority,
        fault_domain=FaultDomain(layer=d.layer, component=d.component, reasoning=d.reasoning, confidence=d.confidence),
        impact=Impact(slo="no SLO defined for this signal" if sig["family"] != "ingress" else "ingress 5xx below 1% and p95 below 500 ms", summary=d.impact or d.what[:200]),
        timeline=timeline,
        root_cause=RootCause(category=d.category, description=d.description, confidence=d.confidence),
        evidence=[Evidence(**e) for e in ctx["evidence"]],
        contributing_factors=contrib,
        how_to_fix=HowToFix(immediate=d.immediate, permanent=d.permanent, verify=d.verify),
        observability_gaps=_gaps(ctx, d),
        rca_markdown="",
    )
    report.rca_markdown = rca.render_rca(report)
    return report
