"""Investigation of alerts that are not about one service's SLO: any pod, node, volume, ingress, Argo CD application, control plane
or telemetry problem, in any cluster. Nothing here names an application: the alert labels (cluster, namespace, pod, node, ingress,
application name) say where to look, the same read-only tools collect the facts, and a rule table turns the facts into

  what happened, the impact, the layer (application or infrastructure) with the reasoning, the fix, the priority and the gaps.

The SLO path in rca.py keeps handling the orders-api burn alerts; rca.gather / rca.analyze_rules hand everything listed in
FAMILIES to this module.
"""
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from agent import config, tools
from agent.models import Evidence, FaultDomain, HowToFix, Impact, Priority, RcaReport, RootCause, TimelineEntry


def _calibrated_confidence(category: str, raw: str) -> str:
    """Downgrade confidence for families with a poor historical accuracy track record."""
    try:
        cal_path = Path(config.REPORTS_DIR) / ".calibration.json"
        if cal_path.exists():
            cal = json.loads(cal_path.read_text(encoding="utf-8"))
            weight = (cal.get(category) or {}).get("confidence_weight", 1.0)
            if weight < 0.4 and raw in ("high", "medium"):
                return "low"
            if weight < 0.5 and raw == "high":
                return "medium"
    except Exception:
        pass
    return raw

# alert name -> what kind of problem it is
FAMILIES = {
    "ContainerWaiting": "container", "PodStuckPending": "scheduling", "PodUnschedulable": "scheduling",
    "DeploymentRolloutStuck": "rollout", "StatefulSetReplicasNotReady": "rollout", "DaemonSetPodsUnavailable": "rollout",
    "JobFailed": "job", "ContainerMemoryNearLimit": "memory", "ContainerCpuThrottled": "cpu",
    "PersistentVolumeFillingUp": "volume", "PersistentVolumeAlmostFull": "volume", "TargetDown": "target",
    "NodeNotReady": "node", "NodePressure": "node", "NodeCpuSaturated": "node", "NodeMemoryNearAllocatable": "node",
    "ApiServerErrors": "controlplane", "ApiServerSlow": "controlplane", "CoreDnsErrors": "dns",
    "IngressHighErrorRate": "ingress", "IngressSlow": "ingress",
    "GatewayServerErrors": "gateway", "GatewayConfigReloadFailing": "gateway",
    "GatewayCertificateNotReady": "certificate", "GatewayCertificateExpiring": "certificate",
    "ArgoAppOutOfSync": "argocd", "ArgoAppUnhealthy": "argocd", "ArgoSyncFailing": "argocd",
    "ClusterTelemetryMissing": "telemetry", "TelemetryNotDelivered": "telemetry", "LogsDropped": "telemetry",
    "PodCrashLooping": "container", "PodOOMKilled": "memory", "DeploymentReplicasUnavailable": "rollout",
    "PodFailed": "container", "CriticalAddonDown": "rollout", "InvestigationWorkflowFailed": "pipeline",
    "DatasourceError": "datasource", "DatasourceNoData": "datasource",   # Grafana's own alerts when a rule cannot query its datasource
    "PodRestartStorm": "container",   # sustained restart burst across a deployment
    "HpaAtMaxReplicas": "hpa",        # HPA ceiling-bound: needs maxReplicas raised
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
    for key in ("deployment", "statefulset", "daemonset", "job_name", "name", "service"):   # name: the Argo CD application
        if labels.get(key):
            return labels[key]
    if labels.get("pod"):
        return re.sub(r"-[a-z0-9]{8,10}-[a-z0-9]{5}$|-[a-z0-9]{5}$|-\d+$", "", labels["pod"])
    return labels.get("ingress") or labels.get("name") or labels.get("node") or labels.get("persistentvolumeclaim") or ""


def _pod_rx(workload: str) -> str:
    """Pod names of one workload: `orders-api` must not also match `orders-api-chaos-...` (a Deployment, StatefulSet or DaemonSet pod)."""
    return f"{workload}(-[a-z0-9]{{8,10}})?-[a-z0-9]{{5}}|{workload}-[0-9]+"


def _obj_rx(workload: str) -> str:
    """Names of events' objects of one workload: the workload itself, its ReplicaSets and its pods."""
    return f"{workload}(-[a-z0-9]{{8,10}})?(-[a-z0-9]{{5}})?|{workload}-[0-9]+"


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
            own = [p for p in sig["pods"] if re.fullmatch(_pod_rx(prefix), p["pod"])]
            sig["pods"] = own or sig["pods"]   # the prefix may also match sibling workloads (`app` and `app-worker`)
            add("kubernetes", f"pods {namespace}/{prefix}* in {cluster}",
                f"{len(sig['pods'])} pods; " + ", ".join(f"{p['pod']}: {p['phase']}{'' if p['ready'] else ' not ready'}, restarts {p['restarts']}"
                                                         + (f", waiting {p['waiting']}" if p.get('waiting') else "")
                                                         + (f", last exit {p['last_terminated']['reason']}({p['last_terminated']['exit_code']})" if p.get('last_terminated') else "")
                                                         for p in sig["pods"][:4]))
        ev = tools.call_tool("get_k8s_events", {"namespace": namespace, "minutes": 120, "cluster": cluster, "name_prefix": prefix})
        raw["events"] = ev
        names = {p["pod"] for p in sig["pods"]}
        sig["events"] = [e for e in ev.get("events", []) if e["object"] in names or e["object"].startswith(tuple(names)) or re.fullmatch(_obj_rx(prefix), e["object"])] if "error" not in ev else []
        if "error" in ev:
            sig["tool_errors"].append(f"event lookup in {cluster} failed: {ev['error'][:120]}")
        if sig["events"]:
            add("kubernetes", f"events {namespace} in {cluster}", "; ".join(f"{e['reason']} {e['object']}: {e['message'][:110]}" for e in sig["events"][:4]))
        sig["nodes"] = sorted({p["node"] for p in sig["pods"] if p.get("node")})

    def logs_for(selector: str, label: str, everything: bool = False, contains: str = ""):
        narrow = f' |= "{contains}"' if contains else ""
        q = f'{{{selector}}}{narrow}' if everything else f'{{{selector}}}{narrow} |~ "{ERROR_LINES}"'
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

    def deploys(app_names: list[str], lookback_seconds: int = 7200, fallback_ns: str | None = None):
        for name in app_names:
            res = tools.call_tool("get_recent_deploys", {"application": name})
            if "error" not in res:
                raw["deploys"] = res
                sig["source_files"] = res.get("source_files", [])
                recent = [d for d in res.get("history", []) if d.get("deployed_at") and rca.parse_ts(d["deployed_at"]) > started - lookback_seconds]
                sig["recent_deploy"] = recent[0] if recent else None
                if recent:
                    d0 = recent[0]
                    deploy_ts = rca.parse_ts(d0["deployed_at"])
                    mins_before = int((started - deploy_ts) / 60)
                    paths = ", ".join(res.get("source_files", [])[:3]) or "unknown path"
                    add("argocd", f"history {name}",
                        f"deploy {d0['revision']} at {d0['deployed_at']} ({mins_before}m before the alert); "
                        f"source: {paths}")
                    return
                # no recent deploy for this app — keep trying others
        # fallback: scan all apps targeting the namespace — use 2× the named-app window
        if fallback_ns:
            ns_minutes = max(int(lookback_seconds / 60) * 2, 240)  # at least 4 h
            ns_res = tools.call_tool("list_namespace_deploys", {"namespace": fallback_ns, "minutes": ns_minutes})
            if "error" not in ns_res:
                raw["deploys"] = ns_res
                recent_any = [a for a in ns_res.get("apps", []) if a.get("history")]
                if recent_any:
                    summary = "; ".join(f"{a['application']} at {a['history'][0]['deployed_at']}" for a in recent_any[:3])
                    sig["recent_deploy"] = recent_any[0]["history"][0]
                    mins_before = int((started - rca.parse_ts(recent_any[0]["history"][0]["deployed_at"])) / 60) if rca.parse_ts(recent_any[0]["history"][0].get("deployed_at", "")) else None
                    window_label = f"{ns_minutes // 60}h" if ns_minutes >= 60 else f"{ns_minutes}m"
                    add("argocd", f"namespace deploys {fallback_ns}",
                        f"{len(recent_any)} app(s) deployed in the {window_label} window: {summary}"
                        + (f" ({mins_before}m before alert)" if mins_before is not None else ""))
                    return
        add("argocd", f"history {app_names[0] if app_names else '?'}", "no deploy in the 2 hours before the alert")

    # ---------------------------------------------------------------- per family
    if family in ("container", "scheduling", "rollout", "job", "memory", "cpu", "target"):
        pod_prefix = labels.get("pod") or workload
        pods_and_events(workload or pod_prefix, ns)
        sig["desired"] = _num(_q(f'max(kube_deployment_spec_replicas{{cluster="{cluster}", namespace="{ns}", deployment="{workload}"}})', 5), "last")
        sig["dns_servfail"] = _num(_q(f'sum(rate(coredns_dns_responses_total{{cluster="{cluster}", rcode="SERVFAIL"}}[5m])) / sum(rate(coredns_dns_responses_total{{cluster="{cluster}"}}[5m]))', 15), "max") or 0.0
        sel = _sel(cluster, ns, f'pod=~"{_pod_rx(workload)}|{labels["pod"]}"' if labels.get("pod") else f'pod=~"{_pod_rx(workload)}"')
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
            logs_for(f'cluster="{cluster}", namespace="{ns}", pod=~"{_pod_rx(workload)}|{labels["pod"]}"' if labels.get("pod") else f'cluster="{cluster}", namespace="{ns}", pod=~"{_pod_rx(workload)}"', "last log lines of the workload", everything=True)
        node_facts(sig.get("nodes", []))
        deploys([f"platform-{workload}", workload])
        scaled_down = sig.get("desired") == 0 or (family == "rollout" and not sig["pods"])   # no pod at all is an outage, not a recovery
        sig["ongoing"] = scaled_down or any(not p["ready"] for p in sig["pods"]) or bool(sig["restarts"] and sig["restarts"] > 0)

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
        err = _q(f'sum(rate(traefik_router_requests_total{{{sel}, code=~"5.."}}[2m])) / sum(rate(traefik_router_requests_total{{{sel}}}[2m]))')
        p95 = _q(f'histogram_quantile(0.95, sum by (le) (rate(traefik_router_request_duration_seconds_bucket{{{sel}}}[2m])))')
        by_status = _q(f'sum by (code) (rate(traefik_router_requests_total{{{sel}}}[5m]))', 10)
        rps = _q(f'sum(rate(traefik_router_requests_total{{{sel}}}[2m]))')
        sig["error_ratio_peak"], sig["error_ratio_last"] = _num(err, "max") or 0.0, _num(err, "last") or 0.0
        sig["p95_peak"] = _num(p95, "max") or 0.0
        sig["rps"] = _num(rps, "last") or 0.0
        sig["statuses"] = {s["labels"].get("code"): round(s["last"], 3) for s in by_status.get("series", [])}
        add("mimir", f"traefik_router_requests_total {ing}", f"{sig['rps']:.2f} req/s, 5xx ratio peaked at {sig['error_ratio_peak']:.0%} (now {sig['error_ratio_last']:.0%}), p95 up to {sig['p95_peak'] * 1000:.0f} ms; by status {sig['statuses']}")
        # the backend Service is a label of the gateway's per-service series (set in Alloy from the route's service name);
        # with no backend pod there is no such series, and the route is named after its Service by convention
        backend = _q(f'count by (backend) (traefik_service_requests_total{{{sel}}})', 5)
        names = _label_values(backend, "backend")
        sig["backend"] = names[0] if names else ing
        pods_and_events(sig["backend"], ns)
        sig["desired"] = _num(_q(f'max(kube_deployment_spec_replicas{{cluster="{cluster}", namespace="{ns}", deployment="{sig["backend"]}"}})', 5), "last")
        logs_for(f'cluster="{cluster}", namespace="{ns}", pod=~"{_pod_rx(sig["backend"])}"', f"error lines of backend {sig['backend']}")
        node_facts(sig.get("nodes", []))
        deploys([f"platform-{sig['backend']}", sig["backend"]], fallback_ns=ns)
        sig["ongoing"] = sig["error_ratio_last"] > 0.02 or (family == "ingress" and sig["p95_peak"] > 1 and labels["alertname"] == "IngressSlow")

    elif family == "gateway":
        ns, workload = "traefik", "traefik"
        sig.update({"namespace": ns, "workload": workload})
        pods_and_events("traefik", ns)
        entry = labels.get("entrypoint", "")
        esel = f'cluster="{cluster}"' + (f', entrypoint="{entry}"' if entry else "")
        err = _q(f'sum(rate(traefik_entrypoint_requests_total{{{esel}, code=~"5.."}}[2m])) / sum(rate(traefik_entrypoint_requests_total{{{esel}}}[2m]))')
        rps = _q(f'sum(rate(traefik_entrypoint_requests_total{{{esel}}}[2m]))')
        top = _q(f'topk(5, sum by (namespace, ingress) (rate(traefik_router_requests_total{{cluster="{cluster}", ingress!="", code=~"5.."}}[5m])))', 10)
        fails = _q(f'sum(increase(traefik_config_reloads_failure_total{{cluster="{cluster}"}}[15m]))', 15)
        sig["error_ratio_peak"], sig["error_ratio_last"] = _num(err, "max") or 0.0, _num(err, "last") or 0.0
        sig["rps"] = _num(rps, "last") or 0.0
        sig["failing_routes"] = sorted(((s["labels"].get("namespace", ""), s["labels"].get("ingress", ""), s["last"]) for s in top.get("series", []) if s["last"] > 0), key=lambda r: -r[2])
        sig["reload_failures"] = _num(fails, "max") or 0.0
        add("mimir", f"traefik_entrypoint_requests_total {entry or cluster}", f"{sig['rps']:.2f} req/s, 5xx ratio peaked at {sig['error_ratio_peak']:.0%} (now {sig['error_ratio_last']:.0%}); "
            + ("routes with 5xx: " + ", ".join(f"{r[0]}/{r[1]} {r[2]:.2f}/s" for r in sig["failing_routes"][:3]) if sig["failing_routes"] else "no route stands out")
            + f"; configuration reload failures in 15m: {sig['reload_failures']:.0f}")
        logs_for(f'cluster="{cluster}", namespace="{ns}"', "gateway error lines")
        top_route = sig["failing_routes"][0] if sig["failing_routes"] else None
        top_backend = top_route[1] if top_route else None
        top_backend_ns = top_route[0] if top_route else None
        backend_apps = ([f"platform-{top_backend}", top_backend] if top_backend else []) + ["platform-traefik", "traefik"]
        deploys(backend_apps, fallback_ns=top_backend_ns or ns)
        # a configuration error in the log (a middleware that does not exist) lasts until someone fixes it
        config_error = bool(re.search(r"does not exist|failed to load|error while reading", _msgs(sig)))
        sig["ongoing"] = sig["error_ratio_last"] > 0.02 or sig["reload_failures"] > 0 or config_error

    elif family == "certificate":
        cert, cert_ns = labels.get("name", workload), labels.get("namespace") or "traefik"
        ns, workload = cert_ns, cert
        sig.update({"namespace": cert_ns, "workload": cert, "certificate": cert})
        csel = f'cluster="{cluster}", namespace="{cert_ns}", name="{cert}"'
        cond = _q(f'max by (condition) (certmanager_certificate_ready_status{{{csel}}})', 15)
        sig["cert_condition"] = next((s["labels"].get("condition") for s in cond.get("series", []) if s["last"] == 1), None)
        sig["issuer"] = (_label_values(_q(f'max by (issuer_name) (certmanager_certificate_ready_status{{{csel}}})', 5), "issuer_name") or [""])[0]
        left = _num(_q(f'max(certmanager_certificate_expiration_timestamp_seconds{{{csel}}}) - time()', 5), "last")
        renew = _num(_q(f'max(certmanager_certificate_renewal_timestamp_seconds{{{csel}}}) - time()', 5), "last")
        sig["days_left"] = left / 86400 if left is not None else None
        sig["renew_in_hours"] = renew / 3600 if renew is not None else None
        add("mimir", f"certmanager_certificate_* {cert_ns}/{cert}", f"ready condition: {sig['cert_condition'] or 'no series'}; issuer {sig['issuer'] or 'unknown'}; "
            + (f"expires in {sig['days_left']:.2f} days" if sig["days_left"] is not None else "no expiry recorded (never issued)")
            + (f"; renewal due in {sig['renew_in_hours']:.1f} h" if sig["renew_in_hours"] is not None else ""))
        pods_and_events("cert-manager", "cert-manager")
        ev = tools.call_tool("get_k8s_events", {"namespace": cert_ns, "minutes": 120, "cluster": cluster, "name_prefix": cert})
        sig["cert_events"] = ev.get("events", []) if "error" not in ev else []
        if "error" in ev:
            sig["tool_errors"].append(f"event lookup in {cluster} failed: {ev['error'][:120]}")
        if sig["cert_events"]:
            add("kubernetes", f"events {cert_ns}/{cert} in {cluster}", "; ".join(f"{e['reason']} {e['object']}: {e['message'][:110]}" for e in sig["cert_events"][:4]))
        sig["events"] = sorted(sig["cert_events"] + sig.get("events", []), key=lambda e: e["ts"], reverse=True)
        logs_for(f'cluster="{cluster}", namespace="cert-manager"', "cert-manager lines about this certificate", contains=cert)
        sig["ongoing"] = sig["cert_condition"] not in (None, "True") or (labels.get("alertname") == "GatewayCertificateExpiring" and sig["days_left"] is not None and sig["days_left"] < 14)

    elif family == "argocd":
        app = labels.get("name", workload)
        res = tools.call_tool("get_argocd_app", {"name": app})
        raw["argocd"] = res
        sig["app"] = res
        if "error" in res:
            sig["tool_errors"].append(f"Argo CD lookup failed: {res['error'][:120]}")
        last_rev = res.get("last_revision", "")
        last_dep = res.get("last_deployed_at", "")
        add("argocd", f"application {app}",
            f"sync {res.get('sync_status')}, health {res.get('health_status')}, destination {res.get('destination')}"
            + (f"; last deploy: {last_rev} at {last_dep}" if last_rev else "")
            + (f"; operation {res.get('operation', {}).get('phase')}: {res.get('operation', {}).get('message', '')[:160]}" if res.get("operation") else "")
            + (f"; conditions: {'; '.join(c[:120] for c in res.get('conditions', [])[:3])}" if res.get("conditions") else "")
            + (f"; unhealthy resources: {', '.join(res.get('unhealthy', [])[:4])}" if res.get("unhealthy") else ""))
        sig["ongoing"] = res.get("sync_status") != "Synced" or res.get("health_status") not in ("Healthy", None)
        # always fetch deploy history so the report can name the commit that caused the issue
        deploys([app, app.removeprefix("platform-").removeprefix("observability-").removeprefix("gitops-")])
        dest = res.get("destination_cluster") or ""
        if res.get("unhealthy") and res.get("destination_namespace") and dest:
            first = res["unhealthy"][0]
            if "/" in first:
                w = first.split(":")[0].split("/")[-1]
                pods_and_events(w, res["destination_namespace"])

    elif family in ("controlplane", "dns"):
        a_err = _q(f'sum by (code) (rate(apiserver_request_total{{cluster="{cluster}", code=~"5.."}}[2m]))')
        a_ratio = _q(f'sum(rate(apiserver_request_total{{cluster="{cluster}", code=~"5.."}}[2m])) / sum(rate(apiserver_request_total{{cluster="{cluster}"}}[2m]))')
        a_p99 = _q(f'histogram_quantile(0.99, sum by (le) (rate(apiserver_request_sli_duration_seconds_bucket{{cluster="{cluster}", verb!~"WATCH|CONNECT"}}[2m])))')
        etcd = _q(f'sum(rate(etcd_request_duration_seconds_sum{{cluster="{cluster}"}}[2m])) / sum(rate(etcd_request_duration_seconds_count{{cluster="{cluster}"}}[2m]))')
        dns = _q(f'sum(rate(coredns_dns_responses_total{{cluster="{cluster}", rcode="SERVFAIL"}}[2m])) / sum(rate(coredns_dns_responses_total{{cluster="{cluster}"}}[2m]))')
        sig["apiserver_error_ratio"] = _num(a_ratio, "max") or 0.0
        sig["apiserver_p99"] = _num(a_p99, "max") or 0.0
        sig["etcd_p99"] = _num(etcd, "max") or 0.0
        sig["dns_servfail"] = _num(dns, "max") or 0.0
        add("mimir", f"apiserver / etcd / coredns in {cluster}", f"API 5xx ratio peaked at {sig['apiserver_error_ratio']:.1%}, API p99 {sig['apiserver_p99'] * 1000:.0f} ms, etcd average {sig['etcd_p99'] * 1000:.0f} ms, DNS SERVFAIL ratio {sig['dns_servfail']:.1%}")
        prefix = "coredns" if family == "dns" else "kube-apiserver"
        pods_and_events(prefix, "kube-system")
        node_facts(sorted({p["node"] for p in sig["pods"] if p.get("node")}))
        sig["ongoing"] = (sig["apiserver_error_ratio"] > 0.02 if family == "controlplane" else sig["dns_servfail"] > 0.02) or sig["apiserver_p99"] > 1

    elif family == "datasource":
        uid = labels.get("datasource_uid") or labels.get("ref_id") or "mimir"
        backend = uid if uid in ("mimir", "loki", "tempo") else "mimir"
        cluster, ns, workload = "observability", "observability", backend
        sig.update({"cluster": cluster, "namespace": ns, "workload": backend, "datasource_backend": backend})
        pods_and_events(backend, ns)
        logs_for(f'cluster="observability", namespace="observability", service="grafana"', "Grafana error lines")
        sig["ongoing"] = any(not p["ready"] for p in sig["pods"]) or not sig["pods"]

    elif family == "pipeline":
        cluster, ns, workload = "observability", "sre-agent", "alert-webhook"
        sig.update({"cluster": cluster, "namespace": ns, "workload": "sre-agent"})
        pods_and_events("alert-webhook", ns)

        # Find the failed workflow by name from Argo metrics
        wf_info = _q('argo_workflow_info{namespace="sre-agent",phase="Failed"}', 30)
        failed_wf_name = next((s["labels"].get("name") for s in wf_info.get("series", []) if s["labels"].get("name")), None)
        if not failed_wf_name:
            wf_count = _q('argo_workflows_count{phase="Failed",namespace="sre-agent"}', 30)
            failed_wf_name = next(
                (s["labels"].get("workflowName") or s["labels"].get("workflow") for s in wf_count.get("series", []) if s.get("last", 0) > 0 or s.get("max", 0) > 0),
                None,
            )
        sig["failed_workflow_name"] = failed_wf_name
        if failed_wf_name:
            add("mimir", 'argo_workflow_info{namespace="sre-agent",phase="Failed"}', f"failed workflow: {failed_wf_name}")

        # Workflow step pod logs (container=main) — avoids picking up rca-viewer nginx access logs
        wf_step_logs = tools.call_tool("query_logql", {
            "query": '{cluster="observability", namespace="sre-agent", container="main"} |~ "(?i)traceback|Error|exception|failed"',
            "minutes": lookback_minutes, "limit": 50,
        })
        raw["workflow_step_logs"] = wf_step_logs
        sig["workflow_step_logs"] = wf_step_logs.get("top_messages", []) if "error" not in wf_step_logs else []
        if sig["workflow_step_logs"]:
            add("loki", '{namespace="sre-agent",container="main"}',
                "workflow step errors: " + "; ".join(f"{m[:110]!r} x{c}" for m, c in sig["workflow_step_logs"][:3]))

        # Check if a recent alert-webhook deploy preceded the failure (a rollout kills in-flight workflows)
        deploys(["observability-sre-agent", "sre-agent"])

        # Broad error log for overall context
        logs_for('cluster="observability", namespace="sre-agent"', "error lines of the investigation steps and the webhook")
        sig["ongoing"] = True

    elif family == "hpa":
        hpa_name = labels.get("horizontalpodautoscaler", workload)
        sel_hpa = f'cluster="{cluster}", namespace="{ns}", horizontalpodautoscaler="{hpa_name}"'
        cur = _q(f'max(kube_horizontalpodautoscaler_status_current_replicas{{{sel_hpa}}})', 30)
        max_r = _q(f'max(kube_horizontalpodautoscaler_spec_max_replicas{{{sel_hpa}}})', 5)
        min_r = _q(f'max(kube_horizontalpodautoscaler_spec_min_replicas{{{sel_hpa}}})', 5)
        cpu_util = _q(f'max(kube_horizontalpodautoscaler_status_target_metric_value{{cluster="{cluster}", namespace="{ns}", horizontalpodautoscaler="{hpa_name}", metric_name="cpu"}})', 30)
        sig["hpa_current"] = _num(cur, "last")
        sig["hpa_max"] = _num(max_r, "last")
        sig["hpa_min"] = _num(min_r, "last")
        sig["hpa_cpu_utilization"] = _num(cpu_util, "max")
        add("mimir", sel_hpa,
            f"HPA {hpa_name}: current={sig['hpa_current']}, max={sig['hpa_max']}, min={sig['hpa_min']}"
            + (f", cpu utilization={sig['hpa_cpu_utilization']:.0%}" if sig["hpa_cpu_utilization"] is not None else ""))
        pods_and_events(hpa_name, ns)
        deploys([f"platform-{hpa_name}", hpa_name])
        sig["desired"] = sig["hpa_current"]
        sig["ongoing"] = sig["hpa_current"] is not None and sig["hpa_max"] is not None and sig["hpa_current"] >= sig["hpa_max"]

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
        stores = {}
        for store in ("mimir", "loki", "tempo"):
            st = tools.call_tool("get_pod_status", {"namespace": "observability", "name_prefix": store, "cluster": "observability"})
            stores[store] = bool([p for p in st.get("pods", []) if p.get("ready")]) if "error" not in st else None
        sig["stores_down"] = [s for s, up_ in stores.items() if up_ is False or (up_ is not None and not up_)]
        if sig["stores_down"]:
            add("kubernetes", "mimir / loki / tempo pods in the observability cluster", f"no ready pod for: {', '.join(sig['stores_down'])}")
        cluster = target
        pods_and_events("alloy", "observability")
        logs_for(f'cluster="{target}", namespace="observability", service="alloy"', "collector error lines")
        sig["ongoing"] = sig["telemetry_age"] is None or sig["telemetry_age"] > 120 or sig["remote_write_failed"] > 0 or sig["log_dropped"] > 0

    if family == "target":
        upq = _q(f'min(up{{cluster="{cluster}", namespace="{ns}", pod=~"{_pod_rx(workload)}"}})', 10)
        sig["target_up_now"] = _num(upq, "last")
        sig["ongoing"] = sig["target_up_now"] in (None, 0.0) or sig["ongoing"]

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

    # ------------------------------------------------------------------ faults that are decided before (or without) a container
    quota = [e for e in sig.get("events", []) if e["reason"] == "FailedCreate" and "quota" in e["message"].lower()]
    if quota:
        return Diag("quota_exceeded", "infrastructure", f"ResourceQuota of namespace {ns}", "high", f"The namespace quota refuses new pods: {quota[0]['message'][:160]}.",
                    f"The ReplicaSet of `{ns}/{wl}` ({cluster}) cannot create pods because the ResourceQuota of the namespace is used up: {quota[0]['message'][:200]}. The workload runs below its desired replicas and cannot scale or replace a pod that dies.",
                    ["FailedCreate events from the ReplicaSet name the quota", "nodes have capacity: the limit is a namespace guardrail"],
                    [f"{k} -n {ns} describe resourcequota", f"Raise the quota in the namespace definition (gitops/platform/base) if the workload is legitimate, or reduce replicas/requests of `{wl}` in {manifest}"],
                    ["Alert on quota use above 85% (kube_resourcequota) so the limit is raised before a rollout needs it"], [f"{k} -n {ns} get pods -l app.kubernetes.io/name={wl}  # desired replicas Running"], f"{wl} cannot reach its desired replicas.")
    evicted = _ev(sig, "Evicted")
    if evicted:
        m = evicted[0]["message"].lower()
        storage = "ephemeral" in m or "storage" in m
        return Diag("resource_exhaustion", "infrastructure" if "node was low" in m else "application", f"{'ephemeral storage' if storage else 'node resources'} of {wl}", "high", f"The pod was evicted: {evicted[0]['message'][:160]}.",
                    f"A pod of `{ns}/{wl}` ({cluster}) was evicted by the kubelet: {evicted[0]['message'][:200]}. " + ("It wrote more to its container filesystem or emptyDir than its ephemeral-storage limit allows (logs, temp files, caches)." if storage and "node was low" not in m else "The node ran low on a resource and the kubelet removed pods to protect itself."),
                    ["Evicted event from the kubelet", m[:120]],
                    [f"{k} -n {ns} get pods | grep Evicted  # then delete the evicted pod records: {k} -n {ns} delete pod --field-selector=status.phase=Failed", ("Find what fills the disk: " + f"{k} -n {ns} exec <pod> -- du -sh /tmp /var/log" if storage else f"{k} describe node <node> | grep -A10 Conditions")],
                    ["Set an ephemeral-storage request and limit that fits the workload, write logs to stdout and temp data to a sized emptyDir"], [f"{k} -n {ns} get pods  # no new Evicted"], f"{wl} loses pods when they hit the limit.")
    # a ReplicaSet scaled to 0 with no other one scaled up is a scale-to-zero, also after self-heal has put the replica back
    # (a rolling update scales a different ReplicaSet up)
    scaling = [(m.group(1), int(m.group(2)), e) for e in _ev(sig, "ScalingReplicaSet") if (m := re.search(r"replica set (\S+) (?:from \d+ )?to (\d+)", e["message"]))]
    zeroed = [s for s in scaling if s[1] == 0]
    rolled = [s for s in scaling if s[1] > 0 and not any(s[0] == z[0] for z in zeroed)]
    restored = sig.get("desired") not in (0, None) and bool(zeroed) and not rolled
    if family == "rollout" and (sig.get("desired") == 0 or restored):
        critical = ns in ("kube-system", "traefik", "cert-manager", "argocd", "kyverno", "argo-workflows", "sre-agent", "observability")
        now_state = f"has since been set back to {sig['desired']:.0f} replicas (by Argo CD self-heal or by hand), and its pod is starting again" if restored else "is still 0"
        return Diag("scaled_to_zero", "infrastructure" if critical else "application", f"{wl} was scaled to 0 replicas", "high", f"{wl} was scaled to zero." + (" It has been set back." if restored else ""),
                    f"`{ns}/{wl}` in the {cluster} cluster was scaled to zero ({zeroed[0][2]['message'][:100] if zeroed else 'its desired count is 0'}): someone ran `kubectl scale`, a HorizontalPodAutoscaler or a Git change set it. The desired count {now_state}" + ("; this is a component the cluster depends on" if critical else "") + ".",
                    ["kube_deployment_spec_replicas is 0" if not restored else "ScalingReplicaSet event scaled the ReplicaSet to 0 and nothing else was rolled out", "while it was at 0 no pod existed, so there are no logs or restarts to explain it"], [f"{k} -n {ns} scale deploy/{wl} --replicas=<n>   # or revert the change in Git, Argo CD restores it on the next sync if self-heal is on", f"{k} -n {ns} get events --sort-by=.lastTimestamp | tail"],
                    ["Keep automated sync with self-heal on for platform components and restrict `scale` rights with RBAC; alert on spec.replicas == 0 for critical add-ons (CriticalAddonDown)"], [f"{k} -n {ns} get deploy {wl}  # READY n/n"], f"Everything that depends on {wl} is affected.")

    # ------------------------------------------------------------------ containers that cannot start or keep dying
    if family in ("container", "job", "rollout", "target", "scheduling", "memory", "cpu") and (family in ("memory", "cpu", "job") or pod.get("phase") == "Failed" or labels.get("reason") or pod.get("waiting") or sig.get("restarts") or _ev(sig, "Failed", "BackOff", "FailedScheduling", "Unhealthy", "FailedMount")):
        reason = labels.get("reason") or pod.get("waiting") or ""
        msgs = _msgs(sig)
        exit_code = (pod.get("last_terminated") or {}).get("exit_code")
        term = (pod.get("last_terminated") or {}).get("reason")
        top = sig["top_log_messages"][0][0][:140] if sig.get("top_log_messages") else ""
        where = f"`{ns}/{wl}` in the {cluster} cluster"

        sched = _ev(sig, "FailedScheduling")
        unplaced = [p for p in pods if p.get("phase") == "Pending" and p.get("waiting") in (None, "", "Unschedulable")]   # Unschedulable is the scheduler's verdict, not a container's waiting reason
        if family == "scheduling" or (sched and (not pods or unplaced)):
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
        pull.sort(key=lambda e: (any(w in e["message"].lower() for w in ("manifest", "not found", "notfound", "pull access", "unauthorized", "denied", "no such host", "timeout", "lookup")), len(e["message"])), reverse=True)
        if reason in ("ImagePullBackOff", "ErrImagePull", "InvalidImageName"):
            m = (pull[0]["message"] if pull else "").lower()
            if "not found" in m or "notfound" in m or "manifest unknown" in m or "invalid" in reason.lower():
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

        not_ready_by_probe = [e for e in _ev(sig, "Unhealthy") if "readiness probe failed" in e["message"].lower()]
        if not_ready_by_probe and pod and pod.get("phase") == "Running" and not pod.get("ready") and reason != "CrashLoopBackOff":
            return Diag("probe_failure", "application", f"readiness probe of {wl}", "high", "The pod runs but its readiness probe fails, so it never receives traffic.",
                        f"{where} is Running but not Ready: {not_ready_by_probe[0]['message'][:160]}. Kubernetes keeps it out of the Service until the probe answers, so the workload has fewer (or no) usable replicas and a rollout cannot complete. The process itself is up" + (f"; the last request in its log is `{top}`" if top else "") + ".",
                        ["Unhealthy events say the readiness probe failed", "the container is running and not restarting"], [f"{k} -n {ns} describe pod {pod.get('pod', wl)} | grep -A6 Readiness  # path, port, timeout", f"{k} -n {ns} logs {pod.get('pod', wl)} --tail=20  # what the probe request got", f"Fix the probe path/port in {manifest}, or the endpoint it calls"],
                        ["Make the readiness endpoint report what the pod needs to serve (not a path that may not exist), and test it in CI"], [f"{k} -n {ns} get pods  # READY 1/1"], f"{wl} serves nothing until it is ready.")
        killed_by_probe = [e for e in _ev(sig, "Unhealthy") if "liveness probe failed" in e["message"].lower() or "startup probe failed" in e["message"].lower()]
        if killed_by_probe and (reason == "CrashLoopBackOff" or sig.get("restarts")) and exit_code in (0, 137, 143, None):
            return Diag("probe_failure", "application", f"liveness probe of {wl}", "high", "The kubelet keeps killing a running process because its liveness probe fails.",
                        f"{where} is restarted again and again by the kubelet: {killed_by_probe[0]['message'][:160]}. The process itself starts and runs (exit code {exit_code}); the probe asks for something it does not answer, so Kubernetes concludes it is dead.",
                        ["Unhealthy events say the liveness probe failed", f"exit code {exit_code} is what a terminated healthy process returns"], [f"{k} -n {ns} describe pod {pod.get('pod', wl)} | grep -A6 Liveness  # path, port, timeout", f"Fix the probe path/port in {manifest}, or the endpoint it calls"],
                        ["Give slow starters a startupProbe, keep liveness cheap and independent of dependencies, and test the probe path in CI"], [f"{k} -n {ns} get pods  # restarts stop increasing"], f"{wl} restarts every few seconds and serves little.")
        if exit_code == 0 and (reason == "CrashLoopBackOff" or sig.get("restarts")):
            return Diag("pod_crash", "application", f"command of {wl} finishes with exit code 0", "high", "The container exits normally, so Kubernetes restarts it forever.",
                        f"{where} is in a restart loop although nothing fails: the process exits with code 0 right after starting. A Deployment expects a long-running process, so Kubernetes restarts it with a growing delay (CrashLoopBackOff). The command or entrypoint finishes (a script, a one-shot task) instead of serving.",
                        ["exit code 0, reason Completed", "no error lines and no node or resource problem"], [f"{k} -n {ns} logs {pod.get('pod', wl)} --previous | tail -20  # what it ran before it ended", f"Check `command`/`args` in {manifest}"],
                        ["Run one-shot work as a Job or CronJob, and keep the long-running process in the Deployment"], [f"{k} -n {ns} get pods  # Running, restarts stable"], f"{wl} never stays up.")
        if reason == "CrashLoopBackOff" or sig.get("restarts") or term in ("Error", "Completed") or pod.get("phase") == "Failed":
            cfg = re.search(r"permission denied|read-only file system|eacces|erofs", msgs)
            dns = re.search(r"name resolution|no such host|could not translate|temporary failure in name|bad address|server misbehaving|can't resolve|nxdomain", msgs)
            conn = re.search(r"connection refused|connect: |dial tcp|timed out|timeout|no route to host|unavailable", msgs)
            probe = _ev(sig, "Unhealthy")
            if cfg:
                return Diag("config_error", "application", f"filesystem permissions of {wl}", "high", f"The process fails on a permission or read-only filesystem error: {top}.",
                            f"{where} keeps crashing (exit {exit_code}) because it cannot write where it tries to: `{top}`. The pod runs with a read-only root filesystem and a non-root user, so every path it writes must be a mounted volume.",
                            ["log lines show permission denied / read-only file system", "restarts began when this build started"], [f"Mount an emptyDir for the path in the log line in {manifest}", f"{k} -n {ns} logs {pod.get('pod', wl)} --previous | tail -30"],
                            ["Test the image with the same securityContext in CI (docker run --read-only --user 10001)"], [f"{k} -n {ns} get pods  # Running, restarts stable"], f"{wl} cannot stay up.")
            if dns and sig.get("dns_servfail", 0) < 0.02:
                return Diag("dns_failure", "application", f"a host name that {wl} cannot resolve", "medium", f"The process cannot resolve a name, but CoreDNS is healthy: {top}.",
                            f"{where} crashes because it cannot resolve a host name (`{top}`). CoreDNS answers normally (SERVFAIL ratio {sig.get('dns_servfail', 0):.1%}) and other lookups work, so the name itself is wrong or does not exist: a typo, a missing Service, or an external name that was never registered.",
                            ["log lines show a resolution failure", f"CoreDNS SERVFAIL ratio {sig.get('dns_servfail', 0):.1%}: the cluster DNS is not the problem"],
                            [f"{k} -n {ns} logs {pod.get('pod', wl)} --previous | tail -20  # the exact name", f"{k} -n {ns} get svc  # does a Service with that name exist in the namespace? (name.namespace.svc for another namespace)"],
                            ["Take host names from configuration that is validated in CI, and add the dependency to a readiness check"], [f"{k} -n {ns} get pods  # Running"], f"{wl} cannot start.")
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
        if not pods:
            gone = "scaled to zero" if sig.get("desired") == 0 else "missing"
            return Diag("ingress_backend_failure", "application", f"service {backend} has no pods ({gone})", "high", f"Users get errors because {backend} has no pods.",
                        f"{shape}. The backend `{ns}/{backend}` has no pods ({gone}), so the gateway answers 503 for every request to `{ing}`.",
                        ["ingress errors started when the backend disappeared", "no pod found for the backend"], [f"{k} -n {ns} get deploy {backend}", f"{k} -n {ns} scale deploy/{backend} --replicas=<n>   # if it was scaled down, or revert the change in Git"],
                        ["Alert on a Service without endpoints, and keep self-heal on so a manual scale-down is reverted"], [f"{k} -n {ns} get endpoints {backend}  # has addresses", "5xx ratio on the ingress below 1%"], f"Users of {ing} get errors until {backend} runs again.")
        if pods and not ready:
            return Diag("ingress_backend_failure", "application" if not sig.get("node_not_ready") else "infrastructure", f"no ready pod behind service {backend}", "high", f"Users get errors because no pod of {backend} is ready.",
                        f"{shape}. The backend `{ns}/{backend}` has no ready pod, so the gateway has nothing to send traffic to (502/503). The pods' state: " + ", ".join(f"{p['pod']} {p['phase']}{' ' + p['waiting'] if p.get('waiting') else ''}" for p in pods[:3]) + ".",
                        ["ingress error rate matches the time the backend became unready", "backend pods not ready"], [f"{k} -n {ns} get pods -l app.kubernetes.io/name={backend}", f"{k} -n {ns} describe pod <pod> | tail -20", "Follow the playbook of the backend's own alert (crash loop, image pull, scheduling)"],
                        ["Run 2+ replicas with a PodDisruptionBudget and readiness probes that reflect real readiness"], [f"{k} -n {ns} get endpoints {backend}  # has addresses", "5xx ratio on the ingress below 1%"], f"Users of {ing} see errors until {backend} has a ready pod.")
        if sig.get("error_ratio_peak", 0) > 0.05:
            return Diag("ingress_backend_failure", "application", f"errors returned by service {backend}", "medium" if top else "low", f"The backend answers with errors: {top or 'see the evidence'}.",
                        f"{shape}. The backend `{ns}/{backend}` is running ({len(ready)} ready pods) but returns the errors itself, so the cause is in the application or its dependencies" + (f"; its most frequent error is `{top}`" if top else "") + ".",
                        [f"{len(ready)} ready backend pods, so the controller reaches them", "errors originate in the backend"] + ([f"a deploy ({sig['recent_deploy']['revision']}) landed in the last hour"] if sig.get("recent_deploy") else []),
                        [f"{k} -n {ns} logs -l app.kubernetes.io/name={backend} --tail=50", ("Roll back the last deploy: revert the commit that changed " + manifest) if sig.get("recent_deploy") else f"Check the dependencies named in the backend's error lines"],
                        ["Give the backend its own SLO alert (RED metrics) so this is caught before the ingress sees it"], ["5xx ratio on the ingress below 1%"], f"Users of {ing} get errors.")
        return Diag("ingress_backend_failure", "application", f"latency of service {backend}", "medium", f"Requests through {ing} are slow.",
                    f"{shape}. The gateway itself is healthy; the time is spent in the backend `{ns}/{backend}`.", ["ingress p95 high while errors are low"], [f"{k} -n {ns} top pods -l app.kubernetes.io/name={backend}", "Look at the backend's traces for the slowest span"],
                    ["Add RED metrics and tracing to the backend"], ["p95 on the ingress below 500 ms"], f"Users of {ing} wait longer than usual.")

    # ------------------------------------------------------------------ Argo CD
    if family == "gateway":
        text = _msgs(sig)
        mw = re.search(r'middleware "?([\w@.-]+)"? does not exist', text)
        cfg = mw or re.search(r"failed to load|cannot find|does not exist|error while reading|invalid", text)
        if cfg or sig.get("reload_failures", 0) > 0:
            named = f"middleware `{mw.group(1)}`" if mw else "a route, middleware or Gateway"
            top = sig["top_log_messages"][0][0][:160] if sig.get("top_log_messages") else ""
            return Diag("gateway_config_error", "application", f"configuration of the gateway ({mw.group(1) if mw else 'see the Traefik log'})", "high" if cfg else "medium",
                        f"The gateway rejects part of its configuration: {named}.",
                        f"Traefik in the {cluster} cluster cannot use {named}" + (f": `{top}`" if top else "") + ". The routes that depend on it do not work (they answer 404 or 5xx) and changes to routes may not be applied"
                        + (f"; {sig['reload_failures']:.0f} configuration reload(s) failed in the last 15 minutes" if sig.get("reload_failures") else "") + ".",
                        ["Traefik logs a configuration error" if cfg else "the configuration reload counter increased", "the gateway pods are running"],
                        [f"{k} -n {ns} logs deploy/traefik --tail=50 | grep -i -E 'error|does not exist'", f"{k} get httproute,middleware -A  # does what the route references exist, in the right namespace?",
                         "Fix the HTTPRoute, Middleware or Secret named in the log in Git (gitops/gateway/*, gitops/workloads/*) and let Argo CD sync it"],
                        ["Validate routes before merge (kubeconform) and keep each Middleware in the same namespace as the HTTPRoute that references it"],
                        [f"{k} -n {ns} logs deploy/traefik --since=5m | grep -c -i error  # 0", "the affected URL answers 200 again"], "Routes that depend on the broken configuration fail while the gateway runs.")
        routes = sig.get("failing_routes", [])
        total = sum(r[2] for r in routes)
        if routes and routes[0][2] / total >= 0.6:   # one route produces most of the errors
            r_ns, route, rate = routes[0]
            return Diag("ingress_backend_failure", "application", f"backend of route {r_ns}/{route}", "medium", f"Most of the gateway's errors come from one route: {route}.",
                        f"The gateway of the {cluster} cluster answers 5xx on {sig.get('error_ratio_peak', 0):.0%} of its requests, and {rate:.2f} req/s of them belong to the route `{r_ns}/{route}`. The gateway itself is running; the backend of that route has no ready pod or answers errors.",
                        ["the gateway pods are ready", f"route {route} produces most of the 5xx"],
                        [f"{k} -n {r_ns} get pods,endpoints  # does the Service behind {route} have a ready pod?", f"Follow the IngressHighErrorRate report for {route}, which looks at the backend pods and logs"],
                        ["Run 2+ replicas with a PodDisruptionBudget and readiness probes behind every route"], ["5xx ratio of the gateway below 1%"], f"Users of {route} get errors.")

    if family == "certificate":
        cert = sig.get("certificate", wl)
        text = _msgs(sig) + " " + " ".join(e["message"].lower() for e in sig.get("events", []))
        ctrl = [p for p in pods if "webhook" not in p["pod"] and "cainjector" not in p["pod"]]
        cm_down = not ctrl or not any(p.get("ready") for p in ctrl)
        missing = re.search(r'issuer\.cert-manager\.io "([^"]+)" not found', text)
        expiring = alertname == "GatewayCertificateExpiring"
        days = sig.get("days_left")
        left = f"{days:.1f} days" if days is not None and days >= 1 else f"{(days or 0) * 24:.1f} hours"
        if cm_down:
            return Diag("certificate_expiring" if expiring else "certificate_not_ready", "infrastructure", "cert-manager controller", "high",
                        "cert-manager is not running, so certificates are not issued or renewed.",
                        f"The certificate `{ns}/{cert}` ({cluster}) " + (f"expires in {left}" if expiring else "is not ready") + f", and cert-manager has no ready controller pod ({', '.join(f'{p['pod']} {p['phase']}' for p in pods[:2]) or 'none found'}): nothing can issue or renew it.",
                        ["no ready cert-manager controller pod", "the certificate is not being reconciled"], [f"{k} -n cert-manager get pods", f"{k} -n cert-manager describe pod -l app.kubernetes.io/component=controller | tail -20"],
                        ["Run cert-manager with resource requests and a PodDisruptionBudget, and alert on certificates well before they expire (GatewayCertificateExpiring)"], [f"{k} -n cert-manager get pods  # 1/1 Running", f"{k} -n {ns} get certificate {cert}  # READY True"],
                        "New TLS handshakes fail once the certificate expires; existing HTTP (8080) is unaffected.")
        if missing:
            return Diag("certificate_expiring" if expiring else "certificate_not_ready", "application", f"issuerRef of certificate {cert} (issuer `{missing.group(1)}`)", "high",
                        f"The certificate names an issuer that does not exist: {missing.group(1)}.",
                        f"cert-manager cannot issue `{ns}/{cert}` ({cluster}) because the issuer `{missing.group(1)}` it references does not exist. Nothing is wrong with cert-manager itself, which is running; the Certificate (or the `cert-manager.io/cluster-issuer` annotation of the Gateway that created it) names the wrong issuer.",
                        [f"the event says the issuer `{missing.group(1)}` was not found", "cert-manager is running"], [f"{k} get clusterissuer,issuer -A  # which issuers exist?", f"Point `issuerRef` of `{cert}` (or the Gateway annotation) at an existing issuer, or create `{missing.group(1)}` (gitops/gateway/common/issuer.yaml)"],
                        ["Keep the issuer and everything that references it in the same Git change, and alert on certificates that are not ready (GatewayCertificateNotReady)"], [f"{k} -n {ns} get certificate {cert}  # READY True"], "The certificate is never issued, so nothing can use the secret it should create.")
        if expiring and (sig.get("renew_in_hours") or 0) > 0 and sig.get("cert_condition") == "True":
            return Diag("certificate_expiring", "application", f"lifetime of certificate {cert}", "medium", f"The certificate is valid and will be renewed, but its lifetime is shorter than the alert window.",
                        f"`{ns}/{cert}` ({cluster}) is ready and expires in {left}. cert-manager has scheduled its renewal in {sig['renew_in_hours']:.1f} hours, so it is not stuck: the certificate was issued with a short `spec.duration`, which puts it inside the 14-day warning window all the time. If that is on purpose (a test or a short-lived certificate), nothing is wrong.",
                        ["the certificate is Ready", f"renewal is due in {sig['renew_in_hours']:.1f} hours", "no issuer or cert-manager error"], [f"{k} -n {ns} get certificate {cert} -o jsonpath='{{.spec.duration}} {{.spec.renewBefore}}'", f"{k} -n {ns} get certificate {cert}  # READY True; wait for the renewal"],
                        ["Use the default 90 day duration with renewBefore of 30 days for real certificates, so the alert only fires when a renewal is overdue"], [f"{k} -n {ns} get certificate {cert} -o jsonpath='{{.status.notAfter}}'  # a new date after the renewal"], "None while the renewal succeeds.")
        if expiring and sig.get("cert_condition") == "True":
            return Diag("certificate_expiring", "infrastructure", f"renewal of certificate {cert}", "medium", "The certificate is close to expiry and its renewal is overdue.",
                        f"`{ns}/{cert}` ({cluster}) expires in {left} and cert-manager has not renewed it although the renewal time has passed. cert-manager is running and the issuer exists, so look at the cert-manager log for the failed order.",
                        ["the renewal time has passed", "cert-manager is running"], [f"{k} -n cert-manager logs deploy/cert-manager --tail=50 | grep {cert}", f"{k} -n {ns} describe certificate {cert} | tail -20"],
                        ["Alert on overdue renewals and give the issuer a health check"], [f"{k} -n {ns} get certificate {cert}  # a new notAfter date"], "TLS fails when it expires.")
        return Diag("certificate_not_ready", "application", f"certificate {cert}", "low", "The certificate is not ready and the signals do not say why.",
                    f"cert-manager reports `{ns}/{cert}` ({cluster}) as not ready (condition {sig.get('cert_condition') or 'unknown'}) and the events and log lines do not name a cause" + (f"; the last log line says `{sig['top_log_messages'][0][0][:120]}`" if sig.get("top_log_messages") else "") + ".",
                    ["the certificate is not Ready", "cert-manager is running"], [f"{k} -n {ns} describe certificate {cert}", f"{k} -n {ns} get certificaterequest,order,challenge"],
                    ["Alert on certificates that are not ready (GatewayCertificateNotReady) and keep issuers in Git"], [f"{k} -n {ns} get certificate {cert}  # READY True"], "The certificate is not issued.")

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
            rd = sig.get("recent_deploy")
            deploy_ctx = ""
            revert_cmd = "git revert HEAD && git push  # then wait for Argo CD to sync"
            if rd:
                deploy_ts = rca.parse_ts(rd["deployed_at"])
                mins_before = int((started - deploy_ts) / 60)
                paths = ", ".join(sig.get("source_files", [])[:3]) or "the app path"
                deploy_ctx = (
                    f" Commit {rd['revision']} was deployed {mins_before}m before the alert "
                    f"(at {rd['deployed_at']}) from {paths}."
                )
                revert_cmd = f"git revert {rd['revision']} && git push  # reverts commit {rd['revision']}; Argo CD syncs automatically"
            return Diag(
                "deploy_regression", "application",
                f"resources of {name}: {', '.join(app['unhealthy'][:3])}",
                "medium",
                f"The application deployed but its resources are unhealthy: {', '.join(app['unhealthy'][:2])}.",
                f"`{name}` is synced from Git but its health is {app.get('health_status')}: "
                f"{', '.join(app['unhealthy'][:4])} (destination {dest}). "
                f"The Git change applied; what it deployed does not run correctly.{deploy_ctx}",
                ["sync is fine, health is not",
                 "see the pod status and events of the unhealthy resource in the evidence"]
                + ([f"commit {rd['revision']} deployed {mins_before}m before the alert"] if rd else []),
                [f"Inspect the unhealthy resource: {k} -n {dest.split('/')[-1] if '/' in dest else ns} describe {app['unhealthy'][0].split(':')[0]}",
                 revert_cmd],
                ["Use sync waves and progressive delivery so a bad release stops early"],
                [f"argocd app get {name}  # Healthy"],
                f"{name} is partly or fully down.",
            )
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
                    f"The API server of the {cluster} cluster has a 5xx ratio of {sig.get('apiserver_error_ratio', 0):.1%} and p99 {sig.get('apiserver_p99', 0) * 1000:.0f} ms (etcd average {sig.get('etcd_p99', 0) * 1000:.0f} ms). Controllers, deployments, Argo CD and every kubectl call are affected; running pods keep serving, but nothing can change.",
                    [f"API 5xx {sig.get('apiserver_error_ratio', 0):.1%}", why], [f"{k} -n kube-system get pods -l component=kube-apiserver", f"docker exec {cluster}-control-plane crictl logs $(docker exec {cluster}-control-plane crictl ps -a --name kube-apiserver -q | head -1) | tail -30", "Check the control plane node for disk or memory pressure"],
                    ["Run an HA control plane in the cloud, and alert on etcd latency and database size"], [f"{k} get --raw /readyz  # ok"], "No deployments or scaling can happen until the API server recovers.")

    # ------------------------------------------------------------------ telemetry
    if family == "telemetry":
        target = labels.get("cluster", cluster)
        pod_ok = any(p.get("ready") for p in pods)
        if not pods and "error" not in ctx["raw"].get("pods", {}):
            return Diag("telemetry_gap", "infrastructure", f"the collector (Alloy) of the {target} cluster has no pod", "high", f"The collector in {target} does not exist.",
                        f"No telemetry arrives from the {target} cluster because its collector has no pod: it was scaled to zero or deleted. The cluster itself answers its API. Dashboards and alerts for {target} are blind until the collector is back.",
                        ["no collector pod in the cluster", "the cluster API answers"], [f"{_ctl(target)} -n observability get deploy alloy", f"{_ctl(target)} -n observability scale deploy/alloy --replicas=1   # or let Argo CD restore it (self-heal)"],
                        ["Keep self-heal on for the collector and alert on its absence from outside the cluster (this alert)"], [f"up{{job=\"alloy\",cluster=\"{target}\"}} == 1"], f"{target} is unobserved.")
        if pods and not pod_ok:
            return Diag("telemetry_gap", "infrastructure", f"the collector (Alloy) of the {target} cluster", "high", f"The collector in {target} is not running.",
                        f"No telemetry arrives from the {target} cluster because its collector is not ready ({', '.join(f'{p['pod']} {p['phase']}' for p in pods[:2])}). Dashboards and alerts for that cluster are blind until it is back.",
                        ["collector pod not ready", "the cluster API answers, so the cluster itself is up"], [f"{_ctl(target)} -n observability describe pod -l app.kubernetes.io/name=alloy | tail -20", f"{_ctl(target)} -n observability logs -l app.kubernetes.io/name=alloy --tail=40"],
                        ["Run the collector with a PodDisruptionBudget and alert on its absence from a second place"], [f"up{{job=\"alloy\",cluster=\"{target}\"}} == 1"], f"Nothing can be detected in {target} while it is blind.")
        if (sig.get("telemetry_age") is None or sig.get("telemetry_age", 0) > 120) and "error" in (raw := ctx["raw"].get("pods", {})):
            return Diag("telemetry_gap", "infrastructure", f"the {target} cluster or the network path to it", "medium", f"The {target} cluster is unreachable or down.",
                        f"No metrics arrive from {target} and its API does not answer ({raw.get('error', '')[:100]}). The cluster is down, stopped, or cut off from the network.",
                        ["no samples from the collector", "the API server of the cluster is unreachable too"], [f"docker ps -a --filter name={target}-  # kind: are the node containers running?", f"docker start {target}-control-plane  # if stopped"], ["Alert on the absence of a cluster from a place outside it"], [f"{_ctl(target)} get nodes"], f"Everything in {target} is unobserved, and possibly down.")
        if sig.get("stores_down"):
            names = ", ".join(sig["stores_down"])
            return Diag("telemetry_gap", "infrastructure", f"{names} in the observability cluster", "high", f"The collector runs, but {names} cannot receive its data.",
                        f"The collector in {target} is running but its writes fail (metrics failing {sig.get('remote_write_failed', 0):.2f}/s, logs dropped {sig.get('log_dropped', 0):.2f}/s) because `{names}` has no ready pod in the observability cluster. Telemetry from every cluster that writes to it is being lost until it is back.",
                        [f"no ready pod for {names}", "collector pod is ready"], [f"{_ctl('observability')} -n observability get pods", f"{_ctl('observability')} -n observability describe pod -l app.kubernetes.io/name={sig['stores_down'][0]} | tail -20", "If it was scaled down: scale it back, or let Argo CD restore it (self-heal)"],
                        ["Give the stores a PodDisruptionBudget and a persistent buffer in the collectors so a short outage loses nothing"], [f"{_ctl('observability')} -n observability get pods  # all Running", "rate(prometheus_remote_storage_samples_failed_total[5m]) is 0"], "Metrics or logs of every cluster are lost until the store is back.")
        return Diag("telemetry_gap", "infrastructure", f"delivery from {target} to the observability cluster", "medium", "The collector runs but its data does not arrive.",
                    f"The collector in {target} is running but writes fail (metrics failing {sig.get('remote_write_failed', 0):.2f}/s, logs dropped {sig.get('log_dropped', 0):.2f}/s). Mimir or Loki is down, overloaded or unreachable, or a network policy blocks the NodePort.",
                    ["remote write failures or dropped log lines"], [f"{_ctl('observability')} -n observability get pods  # Mimir and Loki Running?", f"{_ctl(target)} -n observability logs -l app.kubernetes.io/name=alloy --tail=40"],
                    ["Add a retry queue sized for 15 minutes, and alert on the failure rate (TelemetryNotDelivered)"], ["rate(prometheus_remote_storage_samples_failed_total[5m]) is 0"], f"Data from {target} is lost while writes fail.")

    if family == "pipeline":
        msgs = _msgs(sig)
        auth = re.search(r"401|invalid x-api-key|authentication|api key", msgs)
        rbac = re.search(r"403|forbidden", msgs)
        # prefer step container logs over nginx access logs for the error description
        step_msgs = sig.get("workflow_step_logs") or []
        broad_msgs = sig.get("top_log_messages") or []
        _is_nginx = lambda t: bool(re.match(r"\d+\.\d+\.\d+\.\d+\s+-\s+-\s+\[", t or ""))
        top = next((m for m, _ in step_msgs if not _is_nginx(m)), None) \
              or next((m for m, _ in broad_msgs if not _is_nginx(m)), None) \
              or (broad_msgs[0][0] if broad_msgs else "")
        top = (top or "")[:140]

        wf_name = sig.get("failed_workflow_name") or ""
        wf_label = f" `{wf_name}`" if wf_name else ""
        wf_logs_cmd = (f"{_ctl('observability')} -n sre-agent logs {wf_name} -c main --tail=60"
                       if wf_name else
                       f"{_ctl('observability')} -n sre-agent logs -l workflows.argoproj.io/workflow -c main --tail=60")
        wf_list_cmd = f"{_ctl('observability')} -n sre-agent get wf | grep -v Succeeded"

        # check if a webhook rollout killed the workflow (Killing / ScalingReplicaSet events on alert-webhook)
        kill_events = _ev(sig, "Killing", "ScalingReplicaSet")
        webhook_killed = any(e.get("object", "").startswith("alert-webhook") for e in kill_events)
        recent_deploy = sig.get("recent_deploy")

        if auth:
            return Diag("investigation_pipeline_failed", "application", "the Anthropic API key of the SRE agent", "high", "An investigation failed because the model API rejected the key.",
                        f"An incident workflow{wf_label} ended in Failed: the agent's call to the model API was refused (`{top}`). Reports fall back to the rule-based analysis, which is less detailed, until the key is fixed.",
                        ["error lines name an authentication failure", "the webhook accepts alerts and starts workflows"],
                        [f"{_ctl('observability')} -n sre-agent get secret anthropic-api-key", "Replace the key: ANTHROPIC_API_KEY=... python3 bootstrap/bootstrap.py --only observability   (or delete the secret to run rule-based only)"],
                        ["Alert on workflow failures (this alert) and rotate the key through the secret manager"],
                        [f"{_ctl('observability')} -n sre-agent get wf  # new workflows Succeeded"], "Investigations lose their LLM analysis.")
        if rbac:
            return Diag("investigation_pipeline_failed", "infrastructure", "read access of the SRE agent", "medium", "An investigation failed on a permission error.",
                        f"An incident workflow{wf_label} ended in Failed with a permission error (`{top}`): the agent's service account or one of its read-only tokens lacks a right.",
                        ["error lines contain 403 / forbidden"],
                        [wf_logs_cmd, "Check the ClusterRoles sre-agent-local-read (observability), sre-agent-read (platform) and sre-agent-hub-read (gitops)"],
                        ["Test the agent's access after every cluster rebuild (bootstrap creates the tokens)"],
                        ["next workflow Succeeded"], "Some investigations are incomplete.")
        if webhook_killed:
            kill_msg = kill_events[0].get("message", "")[:100] if kill_events else ""
            deploy_note = f" (deploy {recent_deploy['revision']} at {recent_deploy['deployed_at']})" if recent_deploy else ""
            return Diag("investigation_pipeline_failed", "application", f"alert-webhook rollout{' during active workflow' if wf_name else ''}", "medium",
                        f"A rolling restart of alert-webhook terminated workflow{wf_label} mid-run.",
                        f"The alert-webhook deployment was restarted{deploy_note}: {kill_msg}. The in-flight investigation workflow{wf_label} was killed before it could finish — there is no graceful shutdown handler. No data is lost; the workflow can be re-submitted.",
                        ["Killing / ScalingReplicaSet event on alert-webhook coincides with workflow failure", f"workflow{wf_label} phase is Failed"],
                        [wf_list_cmd, wf_logs_cmd,
                         f"{_ctl('observability')} -n sre-agent delete wf {wf_name}  # clean up, then re-trigger by re-firing the alert" if wf_name else f"{_ctl('observability')} -n sre-agent get wf  # find and delete the failed workflow"],
                        ["Add a preStop hook on alert-webhook that waits for running workflow pods to complete before the container exits, so a rolling deploy drains in-flight investigations gracefully"],
                        [f"{_ctl('observability')} -n sre-agent get wf  # new workflows Succeeded"],
                        "Investigations started during a webhook rollout are silently lost.")
        return Diag("investigation_pipeline_failed", "application", f"investigation workflow{wf_label or ''}", "low", f"Investigation workflow{wf_label} failed.",
                    f"An incident workflow{wf_label} ended in Failed" + (f" with `{top}`" if top else "") + ". The alert that started it has no report until it is re-run.",
                    ["the Argo Workflows metric counts a failed workflow"],
                    [wf_list_cmd, wf_logs_cmd],
                    ["Keep the steps idempotent so a failed workflow can be resubmitted"],
                    ["new workflows Succeeded"], "A report is missing.")

    if family == "datasource":
        be = sig.get("datasource_backend", "mimir")
        down = [p for p in pods if not p.get("ready")]
        recent = [p for p in pods if p.get("restarts", 0) > 0]
        if not pods or down:
            return Diag("telemetry_gap", "infrastructure", f"{be} in the observability cluster", "high", f"Grafana cannot query {be}: it is not running.",
                        f"Grafana's alert rules could not query `{be}` (alert {alertname}) because `{be}` has no ready pod ({', '.join(p['pod'] + ' ' + p['phase'] for p in pods[:2]) or 'none found'}). While it is down, no alert that depends on it can fire, so the observability stack is blind. Argo CD self-heal will restore `{be}` automatically if its replica count was changed outside of Git; if the pod is OOMKilled or crash-looping, self-heal will not help until the root cause (memory limit, bad config) is fixed in Git.",
                        [f"{be} pod not ready", "Grafana itself answers"], [f"{_ctl('observability')} -n observability describe pod -l app.kubernetes.io/name={be} | tail -20", f"{_ctl('observability')} -n observability logs -l app.kubernetes.io/name={be} --tail=50"],
                        ["Give the stores a PodDisruptionBudget and memory headroom (OOM is the usual cause), and alert on their absence from outside the cluster"], [f"{_ctl('observability')} -n observability get pods  # {be} Running"], "Alerting is blind until the datasource is back.")
        return Diag("telemetry_gap", "infrastructure", f"{be} restarted or was unreachable for a short time", "medium", f"{be} was briefly unreachable for Grafana.",
                    f"Grafana could not query `{be}` for a short time (alert {alertname}); `{be}` is ready now" + (f" and restarted {sum(p['restarts'] for p in recent)} time(s) recently" if recent else "") + ". Typical causes are a restart of the node or of the pod itself. Alert rules that evaluated in that window reported an error instead of a result.",
                    [f"{be} is ready now", "a short outage, not an ongoing one"] + ([f"{be} restarted"] if recent else []), [f"{_ctl('observability')} -n observability logs -l app.kubernetes.io/name={be} --previous --tail=30  # why it restarted"],
                    ["If it repeats, give the store more memory or a PodDisruptionBudget, and keep `noDataState`/`execErrState` on the rules deliberate"], [f"{_ctl('observability')} -n observability get pods  # no new restarts"], "A short gap in alert evaluation.")

    if family == "target" and pods and all(p.get("ready") for p in pods):
        return Diag("scrape_failure", "infrastructure", f"metrics endpoint of {wl} is not reachable by the collector", "low",
                    f"{wl} runs and is ready, but the collector cannot scrape its metrics.",
                    f"The pods of `{ns}/{wl}` ({cluster}) are Running and ready, yet the collector's scrape of them fails" + (" (and still does)" if sig.get("target_up_now") == 0 else " (it works again now)") + ". The process is not what is down: the metrics port, path or protocol is wrong, the endpoint is slow, or a network policy blocks the collector.",
                    ["pods are ready", "only the scrape fails"], [f"{k} -n {ns} get networkpolicy  # does one allow the collector (namespace observability, alloy) in?", f"{k} -n {ns} describe pod {pod.get('pod', wl)} | grep -i -A3 annotations  # port and path of the scrape"],
                    ["Name the metrics port `metrics` (the collector then finds it with no annotation) and allow the collector in the namespace's network policy"], [f"up{{cluster=\"{cluster}\",namespace=\"{ns}\"}} == 1"], f"No metrics (and no alerts based on them) for {wl}.")

    if family == "hpa":
        hpa_name = labels.get("horizontalpodautoscaler", wl)
        cur = int(sig.get("hpa_current") or 0)
        max_r = int(sig.get("hpa_max") or 0)
        cpu_util = sig.get("hpa_cpu_utilization")
        cpu_note = f"; CPU utilization is {cpu_util:.0%}" if cpu_util is not None else ""
        return Diag("hpa_saturation", "infrastructure", f"HPA {hpa_name} maxReplicas={max_r}", "medium",
                    f"The HPA {hpa_name} has been at its maximum of {max_r} replicas for over 30 minutes{cpu_note}.",
                    f"The HPA `{ns}/{hpa_name}` ({cluster}) has been running at its `maxReplicas` ceiling of {max_r} for at least 30 minutes{cpu_note}. The autoscaler wants to scale further but cannot; if load continues to rise, error rates or latency will degrade. This needs `maxReplicas` raised in the manifest.",
                    [f"kube_horizontalpodautoscaler_status_current_replicas >= kube_horizontalpodautoscaler_spec_max_replicas for 30m"
                     + (f"; CPU utilization {cpu_util:.0%}" if cpu_util is not None else "")],
                    [f"Raise `maxReplicas` for `{hpa_name}` in its GitOps manifest (gitops/workloads/{wl}/hpa.yaml)",
                     f"kubectl -n {ns} describe hpa {hpa_name}  # see current utilization and desired replicas"],
                    ["Set maxReplicas to at least 2× the typical peak current replicas with a 30% buffer; review monthly in the HPA saturation report"],
                    [f"kube_horizontalpodautoscaler_status_current_replicas{{namespace=\"{ns}\",horizontalpodautoscaler=\"{hpa_name}\"}} < {max_r}  # scaling room restored"],
                    f"Load cannot be absorbed; {wl} is at its replica ceiling.")

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
    if family == "certificate":
        # TLS on the gateway breaks only when the certificate expires, and plain http keeps working: a ticket, unless it expires within a day
        days = sig.get("days_left")
        if days is not None and days < 1 and sig.get("cert_condition") == "True" or (days is not None and days < 1 and d.category == "certificate_expiring" and d.layer == "infrastructure"):
            level = 2
            why.append(f"it expires in {days * 24:.1f} hours and nothing has renewed it")
        else:
            why.append("a certificate that is not ready or expires later breaks new TLS connections only when it expires, not now")
        ongoing = bool(sig.get("ongoing", True))
        why.append("it is still ongoing" if ongoing else "the signals have recovered, so the urgency is lower")
        if not ongoing:
            level = min(level + 1, 4)
        return Priority(level=f"P{level}", respond_within={1: "now: page the on-call and open an incident", 2: "within 1 hour", 3: "next business day", 4: "backlog, no action needed unless it repeats"}[level], ongoing=ongoing, rationale=why)
    user_facing = wl in config.USER_FACING_SERVICES or sig.get("backend") in config.USER_FACING_SERVICES or ns in ("demo", "traefik") or any(p.startswith(tuple(config.USER_FACING_SERVICES)) for p in sig.get("user_pods_on_node", []))
    ready = [p for p in sig.get("pods", []) if p.get("ready")]
    nothing_serving = (bool(sig.get("pods")) and not ready) or (d.category == "scaled_to_zero" and sig.get("desired") == 0)

    if user_facing:
        why.append(f"`{wl}` or something on this path serves users")
        if nothing_serving or sig.get("error_ratio_peak", 0) >= 0.5 or sig.get("node_not_ready") and sig.get("user_pods_on_node"):
            level = 1
            why.append("it is effectively down: " + ("no pod is ready" if nothing_serving else f"{sig.get('error_ratio_peak', 0):.0%} of requests fail" if sig.get("error_ratio_peak", 0) >= 0.5 else "the node under user pods is gone"))
        elif d.category in ("image_pull_failure", "pod_crash", "scheduling_failure", "config_error") and ready:
            why.append(f"{len(ready)} replica(s) still serve, so users see reduced capacity, not an outage")
    else:
        why.append("no direct user traffic depends on it" if family not in ("controlplane", "dns", "telemetry", "argocd") else "it is platform infrastructure")
        if family in ("container", "memory", "cpu", "rollout", "job", "target", "scheduling", "datasource", "hpa"):
            level = max(level, 3)   # a platform component without user impact never outranks next business day
            why.append("a platform component with no direct user impact is capped at P3")

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
            gaps.append(f"`{wl}` has no Argo CD application named platform-{wl} or {wl}: label deploys so the report can name the change and the file")
        if sig.get("mem_ratio") is None and sig["family"] in ("memory", "container"):
            gaps.append(f"No memory limit found for `{wl}`: set resources.limits.memory (Kyverno require-limits would enforce it) so OOM and near-limit alerts have a reference")
    if sig["family"] == "ingress" and not sig.get("rps"):
        gaps.append("The gateway reports no request rate for this route: check that Traefik's metrics are scraped (container port named `metrics`) and that the series carry the `ingress` label (gateway_routes in gitops/observability/alloy/config.alloy)")
    if sig["family"] == "argocd" and not sig.get("app"):
        gaps.append("The agent could not read the Argo CD application: grant the gitops-reader token access to Applications")
    if sig["family"] == "datasource" and d.category == "unknown":
        gaps.append(f"No signal identified which backend caused the datasource alert; add `grafana_alerting_rule_evaluation_failures_total` and `grafana_datasource_request_duration_seconds` to the alert context so the agent can name the failing datasource and the first rule that broke")
    if sig["family"] == "telemetry" and d.category == "unknown":
        gaps.append(f"No log or metric pinpointed why telemetry delivery failed; add a counter on the collector for `loki_write_failed_lines_total` and `prometheus_remote_storage_failed_samples_total` broken down by `cluster` and `component` so the first failing step is visible")
    if sig["family"] == "pipeline":
        step_msgs = sig.get("workflow_step_logs") or []
        broad_msgs = sig.get("top_log_messages") or []
        nginx_bleed = all(re.match(r"\d+\.\d+\.\d+\.\d+\s+-\s+-\s+\[", m) for m, _ in broad_msgs[:3]) if broad_msgs else False
        if nginx_bleed and not step_msgs:
            gaps.append("The error log query matched rca-viewer nginx access logs instead of the workflow step container output — no Python traceback was captured. Filter the Loki query to container=\"main\" to target only workflow step pods")
        if not sig.get("failed_workflow_name"):
            gaps.append("The failed workflow name was not found in `argo_workflow_info` metrics; expose the `name` label on that metric (or add a Loki structured log line in the exit-handler: `logger.error('workflow failed', extra={'workflow': name, 'step': step, 'error': msg})`) so the next report names the specific workflow")
        if d.category == "unknown":
            gaps.append("The investigation workflow failed but no structured error was found; add a `failed_step` label and `error_message` annotation to the workflow's exit handler so the next run names the step and the Python exception")
    if d.category == "unknown":
        evidence_sources = {e.get("source") for e in ctx.get("evidence", []) if isinstance(e, dict)}
        missing = [s for s in ("mimir", "loki", "tempo") if s not in evidence_sources]
        if missing:
            gaps.append(f"The investigation had no data from {', '.join(missing)}; check that those stores are reachable and that the workload's metrics are scraped (`up{{namespace=\"{ns}\"}}` series present) and its logs reach Loki (`{{namespace=\"{ns}\"}}` streams present)")
        gaps.append(f"The alert was not explained; add a recording rule or structured log field that captures the state at alert-fire time (the value that crossed the threshold, the pod name, the error message) so the agent has a named signal to anchor its diagnosis")
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
    d.confidence = _calibrated_confidence(d.category, d.confidence)
    priority = _priority(alert, ctx, d)
    contrib = list(d.contributing)
    if sig.get("recent_deploy"):
        contrib.append(f"a deploy ({sig['recent_deploy']['revision']}) landed within the hour before the alert")
    if sig.get("tool_errors"):
        contrib.append("some lookups failed, so this analysis may be incomplete: " + "; ".join(sig["tool_errors"]))
    run_id = re.sub(r"[^a-z0-9-]+", "-", os.getenv("WORKFLOW_NAME", "").lower()).strip("-") or str(int(now * 1_000_000))
    report = RcaReport(
        id=f"rca-{re.sub(r'[^a-z0-9-]+', '-', wl.lower()).strip('-')[:40] or 'alert'}-{datetime.fromtimestamp(started, timezone.utc).strftime('%Y%m%d-%H%M%S')}-{run_id}",
        alert=alertname, service=wl, severity=labels.get("severity", "unknown"), started_at=_iso(started), generated_at=_iso(now), mode="rule_based",
        summary=f"{alertname} on {wl} ({sig['cluster']}): {d.description}",
        what_happened=d.what,
        priority=priority,
        fault_domain=FaultDomain(layer=d.layer, component=d.component, reasoning=d.reasoning, confidence=d.confidence),
        impact=Impact(slo="no SLO defined for this signal" if sig["family"] not in ("ingress", "gateway") else "gateway 5xx below 1% and p95 below 500 ms", summary=d.impact or d.what[:200]),
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
