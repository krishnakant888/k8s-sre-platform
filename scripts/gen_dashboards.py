#!/usr/bin/env python3
"""Generates the Grafana dashboards in gitops/observability/dashboards (one ConfigMap each, picked up by the Grafana sidecar).

The dashboards name no application. Every one is driven by variables (cluster, namespace, workload, node, ingress...) taken
from labels that the collectors put on every series, so a workload deployed tomorrow shows up in them with no change.
Edit this script, not the generated YAML, then run it from the repo root:  python3 scripts/gen_dashboards.py
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "gitops", "observability", "dashboards")

M = {"type": "prometheus", "uid": "mimir"}
L = {"type": "loki", "uid": "loki"}
T = {"type": "tempo", "uid": "tempo"}

GREEN, YELLOW, ORANGE, RED = "green", "yellow", "orange", "red"


def steps(*pairs):
    """steps((None,'green'),(1,'red')) -> grafana threshold steps"""
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for v, c in pairs]}


class Dash:
    def __init__(self, uid, title, folder, tags, description=""):
        self.uid, self.title, self.folder, self.tags, self.description = uid, title, folder, tags, description
        self.vars, self.panels = [], []
        self.x = self.y = self.row_h = 0
        self.next_id = 1

    # ---- variables
    def var_query(self, name, query, label=None, multi=True, include_all=True, ds=M, regex="", sort=1, depends=""):
        self.vars.append({
            "name": name, "label": label or name, "type": "query", "datasource": ds, "query": {"query": query, "refId": f"v_{name}"},
            "definition": query, "includeAll": include_all, "multi": multi, "allValue": ".+", "refresh": 2, "sort": sort,
            "regex": regex, "current": {"selected": True, "text": "All", "value": "$__all"} if include_all else {},
        })

    def var_custom(self, name, values, default):
        self.vars.append({"name": name, "type": "custom", "query": ",".join(values), "current": {"text": default, "value": default},
                          "options": [{"text": v, "value": v, "selected": v == default} for v in values]})

    def var_text(self, name, label, default=""):
        self.vars.append({"name": name, "label": label, "type": "textbox", "query": default, "current": {"text": default, "value": default}})

    # ---- layout
    def _place(self, w, h):
        if self.x + w > 24:
            self.x, self.y, self.row_h = 0, self.y + self.row_h, 0
        pos = {"x": self.x, "y": self.y, "w": w, "h": h}
        self.x += w
        self.row_h = max(self.row_h, h)
        return pos

    def _add(self, panel, w, h):
        panel["id"] = self.next_id
        self.next_id += 1
        panel["gridPos"] = self._place(w, h)
        self.panels.append(panel)

    def row(self, title):
        self.x, self.y, self.row_h = 0, self.y + self.row_h, 0
        self.panels.append({"id": self.next_id, "type": "row", "title": title, "collapsed": False, "panels": [], "gridPos": {"x": 0, "y": self.y, "w": 24, "h": 1}})
        self.next_id += 1
        self.y += 1

    @staticmethod
    def _targets(targets, ds, instant=False, fmt=None):
        out = []
        for i, t in enumerate(targets):
            expr, legend = (t, "") if isinstance(t, str) else t
            tgt = {"refId": chr(65 + i), "expr": expr, "legendFormat": legend, "datasource": ds}
            if instant:
                tgt["instant"] = True
            if fmt:
                tgt["format"] = fmt
            out.append(tgt)
        return out

    def stat(self, title, expr, unit="short", th=None, w=4, h=4, desc="", decimals=None, ds=M, color_mode="background"):
        defaults = {"unit": unit, "thresholds": th or steps((None, GREEN))}
        if decimals is not None:
            defaults["decimals"] = decimals
        self._add({"type": "stat", "title": title, "description": desc, "datasource": ds,
                   "targets": self._targets([expr], ds),
                   "fieldConfig": {"defaults": defaults, "overrides": []},
                   "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "colorMode": color_mode, "graphMode": "area", "textMode": "auto"}}, w, h)

    def ts(self, title, targets, unit="short", w=12, h=8, desc="", stack=False, ds=M, min_=None, max_=None, th=None, fill=12, legend="bottom", lines_only=False):
        defaults = {"unit": unit, "custom": {"drawStyle": "line", "lineWidth": 1, "fillOpacity": 0 if lines_only else fill, "showPoints": "never",
                                            "stacking": {"mode": "normal" if stack else "none"}, "spanNulls": True}}
        if min_ is not None:
            defaults["min"] = min_
        if max_ is not None:
            defaults["max"] = max_
        if th:
            defaults["thresholds"] = th
            defaults["custom"]["thresholdsStyle"] = {"mode": "line"}
        self._add({"type": "timeseries", "title": title, "description": desc, "datasource": ds, "targets": self._targets(targets, ds),
                   "fieldConfig": {"defaults": defaults, "overrides": []},
                   "options": {"legend": {"displayMode": "table" if legend == "table" else "list", "placement": "bottom", "calcs": ["lastNotNull", "max"] if legend == "table" else []},
                               "tooltip": {"mode": "multi", "sort": "desc"}}}, w, h)

    def table(self, title, targets, w=24, h=8, desc="", ds=M, hide=(), rename=None, units=None, sort_by=None, links=None, thresholds=None):
        """targets are instant queries merged on their labels; the value column of target N is named by rename[N]"""
        tg = self._targets(targets, ds, instant=True, fmt="table")
        overrides = []
        for name, unit in (units or {}).items():
            overrides.append({"matcher": {"id": "byName", "options": name}, "properties": [{"id": "unit", "value": unit}]})
        for name, th in (thresholds or {}).items():
            overrides.append({"matcher": {"id": "byName", "options": name}, "properties": [{"id": "thresholds", "value": th}, {"id": "custom.cellOptions", "value": {"type": "color-background"}}]})
        defaults = {"custom": {"align": "auto"}}
        if links:
            defaults["links"] = links
        exclude = {"Time": True, **{h_: True for h_ in hide}}
        rename_map = {f"Value #{chr(65 + i)}": n for i, n in enumerate(rename or [])}
        trans = [{"id": "merge", "options": {}}, {"id": "organize", "options": {"excludeByName": exclude, "renameByName": rename_map}}]
        if sort_by:
            trans.append({"id": "sortBy", "options": {"sort": [{"field": sort_by, "desc": True}]}})
        self._add({"type": "table", "title": title, "description": desc, "datasource": ds, "targets": tg,
                   "fieldConfig": {"defaults": defaults, "overrides": overrides}, "transformations": trans,
                   "options": {"showHeader": True, "cellHeight": "sm"}}, w, h)

    def bars(self, title, expr, legend, unit="short", w=12, h=8, ds=M, desc="", th=None):
        self._add({"type": "bargauge", "title": title, "description": desc, "datasource": ds,
                   "targets": [{"refId": "A", "expr": expr, "legendFormat": legend, "instant": True, "datasource": ds}],
                   "fieldConfig": {"defaults": {"unit": unit, "thresholds": th or steps((None, GREEN))}, "overrides": []},
                   "options": {"displayMode": "gradient", "orientation": "horizontal", "reduceOptions": {"calcs": ["lastNotNull"], "values": False}, "showUnfilled": True}}, w, h)

    def pie(self, title, expr, legend, w=6, h=8, ds=M, unit="short"):
        self._add({"type": "piechart", "title": title, "datasource": ds,
                   "targets": [{"refId": "A", "expr": expr, "legendFormat": legend, "instant": True, "datasource": ds}],
                   "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
                   "options": {"pieType": "donut", "legend": {"displayMode": "table", "placement": "right", "values": ["value"]}, "reduceOptions": {"calcs": ["lastNotNull"]}}}, w, h)

    def logs(self, title, expr, w=24, h=10, desc=""):
        self._add({"type": "logs", "title": title, "description": desc, "datasource": L, "targets": [{"refId": "A", "expr": expr, "datasource": L, "queryType": "range"}],
                   "options": {"showTime": True, "wrapLogMessage": True, "enableLogDetails": True, "sortOrder": "Descending", "dedupStrategy": "none", "prettifyLogMessage": False}}, w, h)

    def alerts(self, title, w=24, h=10, state_filter=None):
        self._add({"type": "alertlist", "title": title,
                   "options": {"viewMode": "list", "groupMode": "default", "maxItems": 30, "sortOrder": 1, "dashboardAlerts": False,
                               "alertInstanceLabelFilter": "", "alertName": "", "stateFilter": state_filter or {"firing": True, "pending": True, "noData": True, "normal": False, "error": True}}}, w, h)

    def text(self, content, w=24, h=3):
        self._add({"type": "text", "title": "", "options": {"mode": "markdown", "content": content}}, w, h)

    def build(self):
        return {
            "uid": self.uid, "title": self.title, "description": self.description, "tags": self.tags, "schemaVersion": 39, "version": 1,
            "editable": True, "refresh": "30s", "time": {"from": "now-1h", "to": "now"}, "timezone": "browser",
            "graphTooltip": 1, "templating": {"list": self.vars}, "panels": self.panels,
            "links": [{"type": "dashboards", "title": "Dashboards", "tags": ["obs"], "asDropdown": True, "includeVars": True, "keepTime": True}],
            "annotations": {"list": [{"name": "Alerts", "datasource": {"type": "datasource", "uid": "grafana"}, "enable": True, "iconColor": "red",
                                      "target": {"type": "tags", "tags": ["alert"]}}]},
        }

    def write(self):
        body = json.dumps(self.build(), indent=2)
        body = "\n".join("    " + line for line in body.split("\n"))
        name = f"dashboard-{self.uid}"
        text = (f"# Generated by scripts/gen_dashboards.py, edit the script, not this file.\n"
                f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {name}\n  namespace: observability\n  labels:\n    grafana_dashboard: \"1\"\n"
                f"  annotations:\n    grafana_folder: {self.folder}\ndata:\n  {self.uid}.json: |\n{body}\n")
        with open(os.path.join(OUT, f"{self.uid}.yaml"), "w") as f:
            f.write(text)


def cluster_vars(d, namespace=True):
    d.var_query("cluster", "label_values(kube_node_info, cluster)", "Cluster")
    if namespace:
        d.var_query("namespace", 'label_values(kube_pod_info{cluster=~"$cluster"}, namespace)', "Namespace")


C = 'cluster=~"$cluster"'
CN = 'cluster=~"$cluster", namespace=~"$namespace"'
LC = 'cluster=~"$cluster"'
LCN = 'cluster=~"$cluster", namespace=~"$namespace"'

# =====================================================================================================================
# 1. Clusters
d = Dash("obs-clusters", "Clusters overview", "Platform", ["obs", "kubernetes"], "Health, capacity and telemetry freshness of every cluster")
cluster_vars(d, namespace=False)
d.row("Health")
d.stat("Nodes ready", f'sum(kube_node_status_condition{{{C}, condition="Ready", status="true"}})', w=3)
d.stat("Nodes not ready", f'count(kube_node_status_condition{{{C}, condition="Ready", status="true"}} == 0) or vector(0)', th=steps((None, GREEN), (1, RED)), w=3)
d.stat("Pods running", f'sum(kube_pod_status_phase{{{C}, phase="Running"}})', w=3)
d.stat("Pods pending/failed", f'sum(kube_pod_status_phase{{{C}, phase=~"Pending|Failed|Unknown"}}) or vector(0)', th=steps((None, GREEN), (1, ORANGE), (5, RED)), w=3)
d.stat("Containers waiting", f'count(kube_pod_container_status_waiting_reason{{{C}, reason=~"CrashLoopBackOff|ImagePullBackOff|ErrImagePull|CreateContainerConfigError|CreateContainerError"}} == 1) or vector(0)', th=steps((None, GREEN), (1, RED)), desc="CrashLoopBackOff, image pull errors, config errors", w=3)
d.stat("Restarts (1h)", f'sum(increase(kube_pod_container_status_restarts_total{{{C}}}[1h]))', th=steps((None, GREEN), (3, ORANGE), (10, RED)), decimals=0, w=3)
d.stat("Firing alerts", 'sum(grafana_alerting_alerts{state="alerting"}) or vector(0)', th=steps((None, GREEN), (1, RED)), w=3)
d.stat("Argo apps not healthy", 'count(argocd_app_info{health_status!="Healthy"} == 1) or vector(0)', th=steps((None, GREEN), (1, ORANGE)), w=3)
d.row("Per cluster")
d.table("Clusters", [
    f'count by (cluster) (kube_node_info{{{C}}})',
    f'count by (cluster) (kube_node_status_condition{{{C}, condition="Ready", status="true"}} == 1)',
    f'sum by (cluster) (kube_pod_status_phase{{{C}, phase="Running"}})',
    f'sum by (cluster) (kube_pod_status_phase{{{C}, phase=~"Pending|Failed|Unknown"}})',
    f'sum by (cluster) (increase(kube_pod_container_status_restarts_total{{{C}}}[1h]))',
    'time() - max by (cluster) (timestamp(up{job="alloy"}))',
], rename=["Nodes", "Ready", "Running pods", "Pending/failed", "Restarts 1h", "Telemetry age"], units={"Telemetry age": "s"},
    thresholds={"Telemetry age": steps((None, GREEN), (90, ORANGE), (300, RED)), "Pending/failed": steps((None, GREEN), (1, ORANGE))}, h=6,
    desc="Telemetry age is the time since the last metric sample arrived from the cluster's collector: it grows when the cluster, the network path or the collector is down.")
d.row("Capacity")
d.ts("CPU requested / allocatable", [(f'sum by (cluster) (kube_pod_container_resource_requests{{{C}, resource="cpu"}}) / sum by (cluster) (kube_node_status_allocatable{{{C}, resource="cpu"}})', "{{cluster}}")], unit="percentunit", w=6, th=steps((None, GREEN), (0.85, RED)))
d.ts("Memory requested / allocatable", [(f'sum by (cluster) (kube_pod_container_resource_requests{{{C}, resource="memory"}}) / sum by (cluster) (kube_node_status_allocatable{{{C}, resource="memory"}})', "{{cluster}}")], unit="percentunit", w=6, th=steps((None, GREEN), (0.85, RED)))
d.ts("CPU used / allocatable", [(f'sum by (cluster) (rate(container_cpu_usage_seconds_total{{{C}, container!=""}}[5m])) / sum by (cluster) (kube_node_status_allocatable{{{C}, resource="cpu"}})', "{{cluster}}")], unit="percentunit", w=6, th=steps((None, GREEN), (0.85, RED)))
d.ts("Memory used / allocatable", [(f'sum by (cluster) (container_memory_working_set_bytes{{{C}, container!=""}}) / sum by (cluster) (kube_node_status_allocatable{{{C}, resource="memory"}})', "{{cluster}}")], unit="percentunit", w=6, th=steps((None, GREEN), (0.85, RED)))
d.row("Problems right now")
d.table("Workloads below their desired replicas", [
    f'kube_deployment_spec_replicas{{{C}}} - kube_deployment_status_replicas_available{{{C}}} > 0',
], rename=["Missing replicas"], hide=["job", "instance", "service", "pod", "node", "uid"], h=6)
d.table("Containers that cannot start", [
    f'max by (cluster, namespace, pod, container, reason) (kube_pod_container_status_waiting_reason{{{C}, reason=~"CrashLoopBackOff|ImagePullBackOff|ErrImagePull|CreateContainerConfigError|CreateContainerError|InvalidImageName"}} == 1)',
], rename=["Waiting"], h=6)
d.alerts("Firing alerts", h=8)
d.write()

# =====================================================================================================================
# 2. Namespace and workloads
d = Dash("obs-namespace", "Namespace and workloads", "Platform", ["obs", "kubernetes"], "Every workload of a namespace: replicas, resources, restarts, network, warnings")
cluster_vars(d)
d.row("Summary")
d.stat("Pods running", f'sum(kube_pod_status_phase{{{CN}, phase="Running"}})', w=4)
d.stat("Pods not ready", f'sum(kube_pod_status_ready{{{CN}, condition="false"}}) or vector(0)', th=steps((None, GREEN), (1, ORANGE)), w=4)
d.stat("Restarts (1h)", f'sum(increase(kube_pod_container_status_restarts_total{{{CN}}}[1h]))', th=steps((None, GREEN), (3, ORANGE), (10, RED)), decimals=0, w=4)
d.stat("CPU (cores)", f'sum(rate(container_cpu_usage_seconds_total{{{CN}, container!=""}}[5m]))', decimals=2, w=4)
d.stat("Memory", f'sum(container_memory_working_set_bytes{{{CN}, container!=""}})', unit="bytes", w=4)
d.stat("Error logs (5m)", f'sum(count_over_time({{{LCN}, level=~"error|fatal"}}[5m])) or vector(0)', ds=L, th=steps((None, GREEN), (1, ORANGE), (20, RED)), w=4)
d.row("Workloads")
d.table("Deployments", [
    f'kube_deployment_spec_replicas{{{CN}}}',
    f'kube_deployment_status_replicas_available{{{CN}}}',
    f'kube_deployment_status_replicas_unavailable{{{CN}}}',
], rename=["Desired", "Available", "Unavailable"], hide=["job", "instance", "service", "pod", "node", "uid"], h=7,
    thresholds={"Unavailable": steps((None, GREEN), (1, RED))},
    links=[{"title": "Open workload", "url": "/d/obs-workload?var-cluster=${__data.fields.cluster}&var-namespace=${__data.fields.namespace}&var-workload=${__data.fields.deployment}&${__url_time_range}"}])
d.table("StatefulSets and DaemonSets", [
    f'label_replace(kube_statefulset_replicas{{{CN}}}, "workload", "$1", "statefulset", "(.*)")',
    f'label_replace(kube_statefulset_status_replicas_ready{{{CN}}}, "workload", "$1", "statefulset", "(.*)")',
    f'label_replace(kube_daemonset_status_desired_number_scheduled{{{CN}}}, "workload", "$1", "daemonset", "(.*)")',
    f'label_replace(kube_daemonset_status_number_ready{{{CN}}}, "workload", "$1", "daemonset", "(.*)")',
], rename=["StatefulSet desired", "StatefulSet ready", "DaemonSet desired", "DaemonSet ready"], hide=["job", "instance", "service", "pod", "node", "uid", "statefulset", "daemonset"], h=7)
d.row("Resources per pod")
d.ts("CPU", [(f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{CN}, container!=""}}[5m]))', "{{pod}}")], unit="short", desc="cores")
d.ts("Memory working set", [(f'sum by (pod) (container_memory_working_set_bytes{{{CN}, container!=""}})', "{{pod}}")], unit="bytes")
d.ts("Network receive", [(f'sum by (pod) (rate(container_network_receive_bytes_total{{{CN}}}[5m]))', "{{pod}}")], unit="Bps")
d.ts("Network transmit", [(f'sum by (pod) (rate(container_network_transmit_bytes_total{{{CN}}}[5m]))', "{{pod}}")], unit="Bps")
d.bars("Memory vs limit (top 10)", f'topk(10, max by (pod, container) (container_memory_working_set_bytes{{{CN}, container!=""}}) / max by (pod, container) (kube_pod_container_resource_limits{{{CN}, resource="memory"}}))', "{{pod}} {{container}}", unit="percentunit", th=steps((None, GREEN), (0.8, ORANGE), (0.95, RED)))
d.bars("CPU throttling (top 10)", f'topk(10, sum by (pod, container) (rate(container_cpu_cfs_throttled_periods_total{{{CN}, container!=""}}[5m])) / sum by (pod, container) (rate(container_cpu_cfs_periods_total{{{CN}, container!=""}}[5m])))', "{{pod}} {{container}}", unit="percentunit", th=steps((None, GREEN), (0.25, ORANGE), (0.5, RED)))
d.row("Reliability")
d.ts("Restarts", [(f'sum by (pod) (increase(kube_pod_container_status_restarts_total{{{CN}}}[5m]))', "{{pod}}")], unit="short")
d.table("Last termination reason", [f'max by (cluster, namespace, pod, container, reason) (kube_pod_container_status_last_terminated_reason{{{CN}}} == 1)'], rename=["Terminated"], h=8, w=12)
d.row("Logs and events")
d.ts("Log lines by level", [(f'sum by (level) (rate({{{LCN}}}[1m]))', "{{level}}")], ds=L, unit="short", stack=True)
d.ts("Errors by service", [(f'sum by (service) (rate({{{LCN}, level=~"error|fatal"}}[1m]))', "{{service}}")], ds=L, unit="short")
d.logs("Warnings and errors", f'{{{LCN}, level=~"warn|error|fatal"}}')
d.logs("Kubernetes events", f'{{{LC}, service="kubernetes-events"}} | json | namespace=~"$namespace"', desc="scheduling, probes, image pulls, OOM, evictions")
d.write()

# =====================================================================================================================
# 3. Workload detail
d = Dash("obs-workload", "Workload detail", "Workloads", ["obs", "workload"], "Everything about one workload: availability, resources, restarts, logs, events, ingress traffic, traces")
d.var_query("cluster", "label_values(kube_deployment_spec_replicas, cluster)", "Cluster", multi=False, include_all=False)
d.var_query("namespace", 'label_values(kube_deployment_spec_replicas{cluster="$cluster"}, namespace)', "Namespace", multi=False, include_all=False)
d.var_query("workload", 'label_values(kube_deployment_spec_replicas{cluster="$cluster", namespace="$namespace"}, deployment)', "Workload (deployment)", multi=False, include_all=False)
POD = 'pod=~"$workload-[a-z0-9]{8,10}-[a-z0-9]{5}"'
CW = f'cluster="$cluster", namespace="$namespace", {POD}'
d.row("Is it healthy?")
d.stat("Desired replicas", 'max(kube_deployment_spec_replicas{cluster="$cluster", namespace="$namespace", deployment="$workload"})', w=4)
d.stat("Available", 'max(kube_deployment_status_replicas_available{cluster="$cluster", namespace="$namespace", deployment="$workload"})', th=steps((None, RED), (1, GREEN)), w=4)
d.stat("Rollout complete", 'max(kube_deployment_status_observed_generation{cluster="$cluster", namespace="$namespace", deployment="$workload"}) == bool max(kube_deployment_metadata_generation{cluster="$cluster", namespace="$namespace", deployment="$workload"})', th=steps((None, RED), (1, GREEN)), w=4)
d.stat("Restarts (1h)", f'sum(increase(kube_pod_container_status_restarts_total{{{CW}}}[1h])) or vector(0)', th=steps((None, GREEN), (1, ORANGE), (5, RED)), decimals=0, w=4)
d.stat("Error logs (5m)", 'sum(count_over_time({cluster="$cluster", namespace="$namespace", service="$workload", level=~"error|fatal"}[5m])) or vector(0)', ds=L, th=steps((None, GREEN), (1, ORANGE), (20, RED)), w=4)
d.stat("Version", 'max by (version) (kube_deployment_labels{cluster="$cluster", namespace="$namespace", deployment="$workload"})', w=4, color_mode="none", desc="app.kubernetes.io/version label")
d.row("Resources")
d.ts("CPU (cores) vs request", [
    (f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{CW}, container!=""}}[5m]))', "{{pod}}"),
    (f'sum(kube_pod_container_resource_requests{{{CW}, resource="cpu"}})', "requests (total)"),
], w=8)
d.ts("Memory working set vs limit", [
    (f'sum by (pod) (container_memory_working_set_bytes{{{CW}, container!=""}})', "{{pod}}"),
    (f'sum(kube_pod_container_resource_limits{{{CW}, resource="memory"}})', "limits (total)"),
], unit="bytes", w=8)
d.ts("CPU throttling", [(f'sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{{{CW}, container!=""}}[5m])) / sum by (pod) (rate(container_cpu_cfs_periods_total{{{CW}, container!=""}}[5m]))', "{{pod}}")], unit="percentunit", max_=1, w=8)
d.ts("Network receive / transmit", [
    (f'sum(rate(container_network_receive_bytes_total{{{CW}}}[5m]))', "receive"),
    (f'sum(rate(container_network_transmit_bytes_total{{{CW}}}[5m]))', "transmit"),
], unit="Bps", w=8)
d.ts("Filesystem writes", [(f'sum by (pod) (rate(container_fs_writes_bytes_total{{{CW}}}[5m]))', "{{pod}}")], unit="Bps", w=8)
d.ts("Restarts", [(f'sum by (pod) (increase(kube_pod_container_status_restarts_total{{{CW}}}[5m]))', "{{pod}}")], w=8)
d.row("Traffic through the ingress (any workload with an Ingress)")
ING = 'cluster="$cluster", namespace="$namespace", ingress=~"$workload.*"'
d.ts("Requests / s by status", [(f'sum by (status) (rate(nginx_ingress_controller_requests{{{ING}}}[5m]))', "{{status}}")], unit="reqps", stack=True, w=8)
d.ts("Error ratio (5xx)", [(f'sum(rate(nginx_ingress_controller_requests{{{ING}, status=~"5.."}}[5m])) / sum(rate(nginx_ingress_controller_requests{{{ING}}}[5m]))', "5xx")], unit="percentunit", w=8, th=steps((None, GREEN), (0.05, RED)))
d.ts("Latency percentiles", [
    (f'histogram_quantile(0.50, sum by (le) (rate(nginx_ingress_controller_request_duration_seconds_bucket{{{ING}}}[5m])))', "p50"),
    (f'histogram_quantile(0.95, sum by (le) (rate(nginx_ingress_controller_request_duration_seconds_bucket{{{ING}}}[5m])))', "p95"),
    (f'histogram_quantile(0.99, sum by (le) (rate(nginx_ingress_controller_request_duration_seconds_bucket{{{ING}}}[5m])))', "p99"),
], unit="s", w=8)
d.row("Application metrics (when the workload exposes http_request_duration_seconds)")
APP = 'cluster="$cluster", namespace="$namespace", service="$workload"'
d.ts("Requests / s by status", [(f'sum by (status) (rate(http_request_duration_seconds_count{{{APP}}}[5m]))', "{{status}}")], unit="reqps", stack=True, w=8)
d.ts("Latency p95 by route", [(f'histogram_quantile(0.95, sum by (le, route) (rate(http_request_duration_seconds_bucket{{{APP}}}[5m])))', "{{route}}")], unit="s", w=8)
d.ts("Errors / s by route", [(f'sum by (route) (rate(http_request_duration_seconds_count{{{APP}, status=~"5.."}}[5m]))', "{{route}}")], unit="reqps", w=8)
d.row("Logs, events and traces")
d.ts("Log lines by level", [('sum by (level) (rate({cluster="$cluster", namespace="$namespace", service="$workload"}[1m]))', "{{level}}")], ds=L, stack=True, w=12)
d.ts("Error rate by pod", [('sum by (pod) (rate({cluster="$cluster", namespace="$namespace", service="$workload", level=~"error|fatal"}[1m]))', "{{pod}}")], ds=L, w=12)
d.logs("Warnings and errors", '{cluster="$cluster", namespace="$namespace", service="$workload", level=~"warn|error|fatal"}')
d.logs("Kubernetes events", '{cluster=~"$cluster", service="kubernetes-events"} | json | namespace="$namespace" | involvedObject_name=~"$workload.*"' if False else '{cluster=~"$cluster", service="kubernetes-events"} |~ "$workload"', desc="events that mention this workload")
d._add({"type": "table", "title": "Slowest recent traces", "datasource": T,
        "targets": [{"refId": "A", "datasource": T, "queryType": "traceql", "query": '{resource.service.name="$workload"} | select(duration)', "limit": 20}],
        "fieldConfig": {"defaults": {}, "overrides": []}, "options": {"showHeader": True}}, 24, 8)
d.write()

# =====================================================================================================================
# 4. Nodes
d = Dash("obs-nodes", "Nodes and storage", "Platform", ["obs", "kubernetes", "infra"], "Node conditions, CPU, memory, pods per node, volumes, kubelet")
d.var_query("cluster", "label_values(kube_node_info, cluster)", "Cluster")
d.var_query("node", 'label_values(kube_node_info{cluster=~"$cluster"}, node)', "Node")
N = 'cluster=~"$cluster", node=~"$node"'
d.row("Conditions")
d.table("Node conditions", [
    f'max by (cluster, node) (kube_node_status_condition{{{N}, condition="Ready", status="true"}})',
    f'max by (cluster, node) (kube_node_status_condition{{{N}, condition="MemoryPressure", status="true"}})',
    f'max by (cluster, node) (kube_node_status_condition{{{N}, condition="DiskPressure", status="true"}})',
    f'max by (cluster, node) (kube_node_status_condition{{{N}, condition="PIDPressure", status="true"}})',
    f'count by (cluster, node) (kube_pod_info{{{N}}})',
], rename=["Ready", "Memory pressure", "Disk pressure", "PID pressure", "Pods"], h=6,
    thresholds={"Ready": steps((None, RED), (1, GREEN)), "Memory pressure": steps((None, GREEN), (1, RED)), "Disk pressure": steps((None, GREEN), (1, RED)), "PID pressure": steps((None, GREEN), (1, RED))})
d.row("Compute")
d.ts("CPU used / allocatable", [(f'sum by (cluster, node) (rate(container_cpu_usage_seconds_total{{{N}, container!=""}}[5m])) / on (cluster, node) sum by (cluster, node) (kube_node_status_allocatable{{{N}, resource="cpu"}})', "{{cluster}} {{node}}")], unit="percentunit", th=steps((None, GREEN), (0.9, RED)))
d.ts("Memory used / allocatable", [(f'sum by (cluster, node) (container_memory_working_set_bytes{{{N}, container!=""}}) / on (cluster, node) sum by (cluster, node) (kube_node_status_allocatable{{{N}, resource="memory"}})', "{{cluster}} {{node}}")], unit="percentunit", th=steps((None, GREEN), (0.9, RED)))
d.ts("CPU requested / allocatable", [(f'sum by (cluster, node) (kube_pod_container_resource_requests{{{N}, resource="cpu"}}) / on (cluster, node) sum by (cluster, node) (kube_node_status_allocatable{{{N}, resource="cpu"}})', "{{cluster}} {{node}}")], unit="percentunit")
d.ts("Memory requested / allocatable", [(f'sum by (cluster, node) (kube_pod_container_resource_requests{{{N}, resource="memory"}}) / on (cluster, node) sum by (cluster, node) (kube_node_status_allocatable{{{N}, resource="memory"}})', "{{cluster}} {{node}}")], unit="percentunit")
d.ts("Pods per node", [(f'count by (cluster, node) (kube_pod_info{{{N}}})', "{{cluster}} {{node}}")], unit="short", w=8)
d.ts("Network receive", [(f'sum by (cluster, node) (rate(container_network_receive_bytes_total{{{N}}}[5m]))', "{{cluster}} {{node}}")], unit="Bps", w=8)
d.ts("Network transmit", [(f'sum by (cluster, node) (rate(container_network_transmit_bytes_total{{{N}}}[5m]))', "{{cluster}} {{node}}")], unit="Bps", w=8)
d.row("Kubelet")
d.ts("Pod start duration p95", [(f'histogram_quantile(0.95, sum by (cluster, node, le) (rate(kubelet_pod_start_duration_seconds_bucket{{{N}}}[5m])))', "{{cluster}} {{node}}")], unit="s", w=8)
d.ts("PLEG relist p95", [(f'histogram_quantile(0.95, sum by (cluster, node, le) (rate(kubelet_pleg_relist_duration_seconds_bucket{{{N}}}[5m])))', "{{cluster}} {{node}}")], unit="s", w=8)
d.ts("Running pods / containers", [(f'kubelet_running_pods{{{N}}}', "pods {{node}}"), (f'kubelet_running_containers{{{N}, container_state="running"}}', "containers {{node}}")], w=8)
d.row("Volumes")
d.bars("Volume free space", f'min by (cluster, namespace, persistentvolumeclaim) (kubelet_volume_stats_available_bytes{{cluster=~"$cluster"}} / kubelet_volume_stats_capacity_bytes{{cluster=~"$cluster"}})', "{{cluster}} {{namespace}}/{{persistentvolumeclaim}}", unit="percentunit", th=steps((None, RED), (0.1, ORANGE), (0.25, GREEN)))
d.ts("Volume used", [(f'max by (cluster, namespace, persistentvolumeclaim) (kubelet_volume_stats_used_bytes{{cluster=~"$cluster"}})', "{{cluster}} {{namespace}}/{{persistentvolumeclaim}}")], unit="bytes")
d.write()

# =====================================================================================================================
# 5. Ingress
d = Dash("obs-ingress", "Ingress traffic", "Workloads", ["obs", "ingress", "workload"], "Golden signals of everything behind an Ingress, with no per-app work")
d.var_query("cluster", "label_values(nginx_ingress_controller_requests, cluster)", "Cluster")
d.var_query("ingress", 'label_values(nginx_ingress_controller_requests{cluster=~"$cluster"}, ingress)', "Ingress")
I = 'cluster=~"$cluster", ingress=~"$ingress"'
d.row("Golden signals")
d.stat("Requests / s", f'sum(rate(nginx_ingress_controller_requests{{{I}}}[5m]))', unit="reqps", decimals=2, w=4)
d.stat("Error ratio (5xx)", f'sum(rate(nginx_ingress_controller_requests{{{I}, status=~"5.."}}[5m])) / sum(rate(nginx_ingress_controller_requests{{{I}}}[5m]))', unit="percentunit", th=steps((None, GREEN), (0.01, ORANGE), (0.05, RED)), w=4)
d.stat("Client errors (4xx)", f'sum(rate(nginx_ingress_controller_requests{{{I}, status=~"4.."}}[5m])) / sum(rate(nginx_ingress_controller_requests{{{I}}}[5m]))', unit="percentunit", th=steps((None, GREEN), (0.1, ORANGE), (0.3, RED)), w=4)
d.stat("p95 latency", f'histogram_quantile(0.95, sum by (le) (rate(nginx_ingress_controller_request_duration_seconds_bucket{{{I}}}[5m])))', unit="s", th=steps((None, GREEN), (0.5, ORANGE), (1, RED)), w=4)
d.stat("p99 latency", f'histogram_quantile(0.99, sum by (le) (rate(nginx_ingress_controller_request_duration_seconds_bucket{{{I}}}[5m])))', unit="s", th=steps((None, GREEN), (1, ORANGE), (2, RED)), w=4)
d.stat("Config reload failures", f'sum(nginx_ingress_controller_config_last_reload_successful{{cluster=~"$cluster"}} == bool 0) or vector(0)', th=steps((None, GREEN), (1, RED)), w=4)
d.ts("Requests / s by ingress", [(f'sum by (cluster, ingress) (rate(nginx_ingress_controller_requests{{{I}}}[5m]))', "{{cluster}} {{ingress}}")], unit="reqps", stack=True)
d.ts("Requests / s by status", [(f'sum by (status) (rate(nginx_ingress_controller_requests{{{I}}}[5m]))', "{{status}}")], unit="reqps", stack=True)
d.ts("Error ratio by ingress", [(f'sum by (cluster, ingress) (rate(nginx_ingress_controller_requests{{{I}, status=~"5.."}}[5m])) / sum by (cluster, ingress) (rate(nginx_ingress_controller_requests{{{I}}}[5m]))', "{{cluster}} {{ingress}}")], unit="percentunit", th=steps((None, GREEN), (0.05, RED)))
d.ts("Latency percentiles", [
    (f'histogram_quantile(0.50, sum by (le) (rate(nginx_ingress_controller_request_duration_seconds_bucket{{{I}}}[5m])))', "p50"),
    (f'histogram_quantile(0.95, sum by (le) (rate(nginx_ingress_controller_request_duration_seconds_bucket{{{I}}}[5m])))', "p95"),
    (f'histogram_quantile(0.99, sum by (le) (rate(nginx_ingress_controller_request_duration_seconds_bucket{{{I}}}[5m])))', "p99"),
], unit="s")
d.row("Per ingress")
d.table("Ingresses", [
    f'sum by (cluster, namespace, ingress) (rate(nginx_ingress_controller_requests{{{I}}}[5m]))',
    f'sum by (cluster, namespace, ingress) (rate(nginx_ingress_controller_requests{{{I}, status=~"5.."}}[5m])) / sum by (cluster, namespace, ingress) (rate(nginx_ingress_controller_requests{{{I}}}[5m]))',
    f'histogram_quantile(0.95, sum by (cluster, namespace, ingress, le) (rate(nginx_ingress_controller_request_duration_seconds_bucket{{{I}}}[5m])))',
], rename=["Requests/s", "Error ratio", "p95 (s)"], units={"Requests/s": "reqps", "Error ratio": "percentunit", "p95 (s)": "s"},
    thresholds={"Error ratio": steps((None, GREEN), (0.01, ORANGE), (0.05, RED)), "p95 (s)": steps((None, GREEN), (0.5, ORANGE), (1, RED))}, h=7)
d.row("Controller")
d.ts("Request size / response size", [
    (f'sum(rate(nginx_ingress_controller_request_size_sum{{{I}}}[5m]))', "request"),
    (f'sum(rate(nginx_ingress_controller_response_size_sum{{{I}}}[5m]))', "response"),
], unit="Bps", w=8)
d.ts("Active connections", [(f'sum by (cluster, state) (nginx_ingress_controller_nginx_process_connections{{cluster=~"$cluster"}})', "{{cluster}} {{state}}")], w=8)
d.ts("Controller memory", [(f'sum by (cluster, pod) (container_memory_working_set_bytes{{cluster=~"$cluster", namespace="ingress-nginx", container="controller"}})', "{{cluster}} {{pod}}")], unit="bytes", w=8)
d.logs("Access log: errors", '{cluster=~"$cluster", namespace="ingress-nginx"} | json | status >= 500', desc="ingress-nginx writes one JSON line per request")
d.write()

# =====================================================================================================================
# 6. Control plane
d = Dash("obs-control-plane", "Control plane and DNS", "Platform", ["obs", "kubernetes", "infra"], "API server, etcd, CoreDNS and controller work queues")
d.var_query("cluster", "label_values(apiserver_request_total, cluster)", "Cluster")
K = 'cluster=~"$cluster"'
d.row("API server")
d.ts("Requests / s by code", [(f'sum by (cluster, code) (rate(apiserver_request_total{{{K}}}[5m]))', "{{cluster}} {{code}}")], unit="reqps", stack=True)
d.ts("5xx ratio", [(f'sum by (cluster) (rate(apiserver_request_total{{{K}, code=~"5.."}}[5m])) / sum by (cluster) (rate(apiserver_request_total{{{K}}}[5m]))', "{{cluster}}")], unit="percentunit", th=steps((None, GREEN), (0.05, RED)))
d.ts("Latency p99 by verb", [(f'histogram_quantile(0.99, sum by (cluster, verb, le) (rate(apiserver_request_duration_seconds_bucket{{{K}, verb!~"WATCH|CONNECT"}}[5m])))', "{{cluster}} {{verb}}")], unit="s", th=steps((None, GREEN), (1, RED)))
d.ts("Requests in flight", [(f'sum by (cluster, request_kind) (apiserver_current_inflight_requests{{{K}}})', "{{cluster}} {{request_kind}}")])
d.ts("Objects stored (top 10)", [(f'topk(10, max by (cluster, resource) (apiserver_storage_objects{{{K}}}))', "{{cluster}} {{resource}}")], w=12)
d.ts("Webhook rejections", [(f'sum by (cluster, name) (rate(apiserver_admission_webhook_rejection_count{{{K}}}[5m]))', "{{cluster}} {{name}}")], w=12)
d.row("etcd")
d.ts("Request latency p99", [(f'histogram_quantile(0.99, sum by (cluster, type, le) (rate(etcd_request_duration_seconds_bucket{{{K}}}[5m])))', "{{cluster}} {{type}}")], unit="s", w=12)
d.ts("Database size", [(f'max by (cluster) (etcd_db_total_size_in_bytes{{{K}}})', "{{cluster}}")], unit="bytes", w=12)
d.row("DNS (CoreDNS)")
d.ts("Queries / s by rcode", [(f'sum by (cluster, rcode) (rate(coredns_dns_responses_total{{{K}}}[5m]))', "{{cluster}} {{rcode}}")], unit="reqps", stack=True, w=8)
d.ts("SERVFAIL ratio", [(f'sum by (cluster) (rate(coredns_dns_responses_total{{{K}, rcode="SERVFAIL"}}[5m])) / sum by (cluster) (rate(coredns_dns_responses_total{{{K}}}[5m]))', "{{cluster}}")], unit="percentunit", w=8, th=steps((None, GREEN), (0.05, RED)))
d.ts("Lookup latency p99", [(f'histogram_quantile(0.99, sum by (cluster, le) (rate(coredns_dns_request_duration_seconds_bucket{{{K}}}[5m])))', "{{cluster}}")], unit="s", w=8)
d.row("Controllers")
d.ts("Work queue depth", [(f'topk(10, max by (cluster, name) (workqueue_depth{{{K}}}))', "{{cluster}} {{name}}")], w=12)
d.ts("Work queue retries", [(f'topk(10, sum by (cluster, name) (rate(workqueue_retries_total{{{K}}}[5m])))', "{{cluster}} {{name}}")], w=12)
d.write()

# =====================================================================================================================
# 7. Argo CD
d = Dash("obs-argocd", "Argo CD and GitOps", "Platform", ["obs", "gitops"], "Sync and health of every application, controller work, repo server, cluster connections")
d.var_query("project", "label_values(argocd_app_info, project)", "Project")
d.var_query("dest", 'label_values(argocd_app_info{project=~"$project"}, dest_name)', "Destination cluster")
A = 'project=~"$project", dest_name=~"$dest"'
d.row("Applications")
d.stat("Applications", f'count(argocd_app_info{{{A}}})', w=4)
d.stat("Synced", f'count(argocd_app_info{{{A}, sync_status="Synced"}}) or vector(0)', th=steps((None, GREEN)), w=4)
d.stat("Out of sync", f'count(argocd_app_info{{{A}, sync_status!="Synced"}}) or vector(0)', th=steps((None, GREEN), (1, ORANGE)), w=4)
d.stat("Healthy", f'count(argocd_app_info{{{A}, health_status="Healthy"}}) or vector(0)', th=steps((None, GREEN)), w=4)
d.stat("Degraded / missing", f'count(argocd_app_info{{{A}, health_status=~"Degraded|Missing"}}) or vector(0)', th=steps((None, GREEN), (1, RED)), w=4)
d.stat("Sync failures (1h)", f'sum(increase(argocd_app_sync_total{{{A}, phase=~"Error|Failed"}}[1h])) or vector(0)', th=steps((None, GREEN), (1, RED)), decimals=0, w=4)
d.table("Applications", [
    f'max by (name, project, dest_name, sync_status, health_status) (argocd_app_info{{{A}}})',
], rename=["Present"], hide=["Present"], h=10,
    desc="sync_status and health_status per application, destination is the registered cluster it deploys to")
d.row("Controller and repo server")
d.ts("Syncs / min by outcome", [(f'sum by (phase) (increase(argocd_app_sync_total{{{A}}}[5m])) / 5', "{{phase}}")], stack=True, w=8)
d.ts("Reconciliation duration p95", [('histogram_quantile(0.95, sum by (le) (rate(argocd_app_reconcile_bucket[5m])))', "p95")], unit="s", w=8)
d.ts("Git requests / s", [('sum by (request_type) (rate(argocd_git_request_total[5m]))', "{{request_type}}")], unit="reqps", w=8)
d.ts("Repo server git latency p95", [('histogram_quantile(0.95, sum by (request_type, le) (rate(argocd_git_request_duration_seconds_bucket[5m])))', "{{request_type}}")], unit="s", w=8)
d.ts("Controller work queue depth", [('max by (name) (workqueue_depth{service="argocd-application-controller"})', "{{name}}")], w=8)
d.ts("Cluster connection", [('max by (server) (argocd_cluster_info)', "{{server}}")], w=8)
d.row("Resources")
d.ts("Argo CD memory", [('sum by (pod) (container_memory_working_set_bytes{cluster="gitops", namespace="argocd", container!=""})', "{{pod}}")], unit="bytes", w=12)
d.ts("Argo CD CPU", [('sum by (pod) (rate(container_cpu_usage_seconds_total{cluster="gitops", namespace="argocd", container!=""}[5m]))', "{{pod}}")], w=12)
d.logs("Argo CD errors", '{cluster="gitops", namespace="argocd"} |~ "(?i)error|fail"')
d.write()

# =====================================================================================================================
# 8. Observability stack
d = Dash("obs-stack", "Observability stack health", "Platform", ["obs", "observability"], "Mimir, Loki, Tempo, Grafana and the collectors: is the monitoring itself working?")
d.var_query("cluster", "label_values(up, cluster)", "Cluster")
S = 'cluster=~"$cluster"'
d.row("Collectors (every cluster)")
d.table("Telemetry per cluster", [
    'time() - max by (cluster) (timestamp(up{job="alloy"}))',
    f'sum by (cluster) (rate(prometheus_remote_storage_samples_total{{{S}}}[5m]))',
    f'sum by (cluster) (rate(prometheus_remote_storage_samples_failed_total{{{S}}}[5m]))',
    f'sum by (cluster) (rate(loki_write_sent_entries_total{{{S}}}[5m]))',
    f'sum by (cluster) (rate(loki_write_dropped_entries_total{{{S}}}[5m]))',
    f'count by (cluster) (up{{{S}}} == 0)',
], rename=["Metric age", "Samples/s sent", "Samples/s failed", "Log lines/s sent", "Log lines/s dropped", "Scrape targets down"],
    units={"Metric age": "s"}, thresholds={"Samples/s failed": steps((None, GREEN), (0.01, RED)), "Log lines/s dropped": steps((None, GREEN), (0.01, RED)), "Scrape targets down": steps((None, GREEN), (1, ORANGE)), "Metric age": steps((None, GREEN), (90, ORANGE), (300, RED))}, h=5)
d.table("Scrape targets that are down", ['max by (cluster, namespace, pod, service, job) (up == 0)'], rename=["Up"], h=5)
d.row("Mimir (metrics)")
d.ts("Samples received / s", [('sum by (cluster) (rate(cortex_distributor_received_samples_total[5m]))', "{{cluster}}")], w=6)
d.ts("Active series", [('sum(cortex_ingester_active_series)', "series")], w=6)
d.ts("Query latency p99", [('histogram_quantile(0.99, sum by (le) (rate(cortex_request_duration_seconds_bucket{route=~"prometheus_api_v1_query.*"}[5m])))', "p99")], unit="s", w=6)
d.ts("Request errors", [('sum by (status_code) (rate(cortex_request_duration_seconds_count{status_code=~"5.."}[5m]))', "{{status_code}}")], unit="reqps", w=6)
d.row("Loki (logs)")
d.ts("Ingested bytes / s", [('sum(rate(loki_distributor_bytes_received_total[5m]))', "bytes")], unit="Bps", w=6)
d.ts("Ingested lines / s", [('sum(rate(loki_distributor_lines_received_total[5m]))', "lines")], w=6)
d.ts("Query latency p99", [('histogram_quantile(0.99, sum by (le) (rate(loki_request_duration_seconds_bucket{route=~"loki_api_v1_query.*"}[5m])))', "p99")], unit="s", w=6)
d.ts("Request errors", [('sum by (status_code) (rate(loki_request_duration_seconds_count{status_code=~"5.."}[5m]))', "{{status_code}}")], unit="reqps", w=6)
d.row("Tempo (traces)")
d.ts("Spans received / s", [('sum(rate(tempo_distributor_spans_received_total[5m]))', "spans")], w=8)
d.ts("Discarded spans / s", [('sum by (reason) (rate(tempo_discarded_spans_total[5m]))', "{{reason}}")], w=8)
d.ts("Query latency p99", [('histogram_quantile(0.99, sum by (le) (rate(tempo_request_duration_seconds_bucket{route=~"api_.*"}[5m])))', "p99")], unit="s", w=8)
d.row("Grafana and alerting")
d.ts("Rule evaluations / failures", [('sum(rate(grafana_alerting_rule_evaluations_total[5m]))', "evaluations"), ('sum(rate(grafana_alerting_rule_evaluation_failures_total[5m]))', "failures")], w=8)
d.ts("Alerts by state", [('sum by (state) (grafana_alerting_alerts)', "{{state}}")], w=8)
d.ts("HTTP request latency p95", [('histogram_quantile(0.95, sum by (le) (rate(grafana_http_request_duration_seconds_bucket[5m])))', "p95")], unit="s", w=8)
d.row("Storage of the stack")
d.ts("Volume used", [('max by (persistentvolumeclaim) (kubelet_volume_stats_used_bytes{cluster="observability"})', "{{persistentvolumeclaim}}")], unit="bytes", w=12)
d.ts("Volume free", [('min by (persistentvolumeclaim) (kubelet_volume_stats_available_bytes{cluster="observability"} / kubelet_volume_stats_capacity_bytes{cluster="observability"})', "{{persistentvolumeclaim}}")], unit="percentunit", w=12)
d.write()

# =====================================================================================================================
# 9. Logs and events
d = Dash("obs-logs", "Logs and events", "Logs", ["obs", "logs"], "Search every log line of every cluster, volume by level, top errors, Kubernetes events")
d.var_query("cluster", "label_values(cluster)", "Cluster", ds=L)
d.var_query("namespace", 'label_values({cluster=~"$cluster"}, namespace)', "Namespace", ds=L)
d.var_query("service", 'label_values({cluster=~"$cluster", namespace=~"$namespace"}, service)', "Service", ds=L)
d.var_query("level", 'label_values({cluster=~"$cluster"}, level)', "Level", ds=L)
d.var_text("search", "Contains", "")
LS = 'cluster=~"$cluster", namespace=~"$namespace", service=~"$service"'
d.row("Volume")
d.stat("Lines / s", f'sum(rate({{{LS}}}[1m]))', ds=L, unit="short", decimals=1, w=4, th=steps((None, GREEN)))
d.stat("Errors (5m)", f'sum(count_over_time({{{LS}, level=~"error|fatal"}}[5m])) or vector(0)', ds=L, th=steps((None, GREEN), (1, ORANGE), (20, RED)), w=4)
d.stat("Warnings (5m)", f'sum(count_over_time({{{LS}, level="warn"}}[5m])) or vector(0)', ds=L, th=steps((None, GREEN), (10, ORANGE)), w=4)
d.stat("Services logging", f'count(sum by (service) (count_over_time({{{LS}}}[5m])))', ds=L, w=4)
d.stat("Streams with trace_id (5m)", f'sum(count_over_time({{{LS}}} |= "trace_id" [5m])) or vector(0)', ds=L, w=4)
d.stat("Events (warning, 15m)", f'sum(count_over_time({{cluster=~"$cluster", service="kubernetes-events"}} |= "Warning" [15m])) or vector(0)', ds=L, th=steps((None, GREEN), (1, ORANGE)), w=4)
d.ts("Lines by level", [(f'sum by (level) (rate({{{LS}}}[1m]))', "{{level}}")], ds=L, stack=True, w=12)
d.ts("Errors by service", [(f'sum by (cluster, service) (rate({{{LS}, level=~"error|fatal"}}[1m]))', "{{cluster}} {{service}}")], ds=L, w=12)
d.row("Top errors")
d.table("Services with the most errors (15m)", [f'topk(15, sum by (cluster, namespace, service) (count_over_time({{{LS}, level=~"error|fatal"}}[15m])))'], ds=L, rename=["Errors"], h=8, sort_by="Errors")
d.row("Search")
d.logs("Matching log lines", f'{{{LS}, level=~"$level"}} |~ "(?i)$search"', h=14)
d.row("Kubernetes events")
d.ts("Events by reason", [('sum by (reason) (count_over_time({cluster=~"$cluster", service="kubernetes-events"} | json [5m]))', "{{reason}}")], ds=L, stack=True, w=12)
d.logs("Events", '{cluster=~"$cluster", service="kubernetes-events"} |~ "(?i)$search"', w=12, h=8)
d.write()

# =====================================================================================================================
# 10. Alerts and incidents
d = Dash("obs-alerts", "Alerts and incidents", "Alerts", ["obs", "alerts"], "What is firing, how often, and what the SRE agent concluded")
d.row("Now")
d.stat("Firing", 'sum(grafana_alerting_alerts{state="alerting"}) or vector(0)', th=steps((None, GREEN), (1, RED)), w=4)
d.stat("Pending", 'sum(grafana_alerting_alerts{state="pending"}) or vector(0)', th=steps((None, GREEN), (1, ORANGE)), w=4)
d.stat("Rules", 'sum(grafana_alerting_rule_group_rules{state="active"}) or vector(0)', w=4)
d.stat("Investigations started (24h)", 'sum(increase(investigations_started_total[24h])) or vector(0)', decimals=0, w=4)
d.stat("Evaluation failures (1h)", 'sum(increase(grafana_alerting_rule_evaluation_failures_total[1h])) or vector(0)', th=steps((None, GREEN), (1, RED)), decimals=0, w=4)
d.stat("Notification failures (1h)", 'sum(increase(grafana_alerting_notifications_failed_total[1h])) or vector(0)', th=steps((None, GREEN), (1, RED)), decimals=0, w=4)
d.alerts("Active alerts", h=10)
d.row("History")
d.ts("Alerts by state", [('sum by (state) (grafana_alerting_alerts)', "{{state}}")], w=12)
d.ts("Investigations started by alert", [('sum by (alertname) (increase(investigations_started_total[15m]))', "{{alertname}}")], w=12)
d.logs("Agent and webhook logs", '{cluster="observability", namespace="sre-agent"}', h=10)
d.write()

# =====================================================================================================================
# 11. Workflows (Argo Workflows and the investigation pipeline)
d = Dash("obs-workflows", "Argo Workflows and investigations", "Alerts", ["obs", "workflows"], "The incident pipeline: controller health, workflow outcomes, step durations")
d.row("Controller")
d.stat("Workflows running", 'sum(argo_workflows_count{status="Running"}) or vector(0)', w=4)
d.stat("Succeeded (24h)", 'sum(argo_workflows_count{status="Succeeded"}) or vector(0)', w=4, th=steps((None, GREEN)))
d.stat("Failed", 'sum(argo_workflows_count{status="Failed"}) or vector(0)', w=4, th=steps((None, GREEN), (1, RED)))
d.stat("Errors", 'sum(argo_workflows_count{status="Error"}) or vector(0)', w=4, th=steps((None, GREEN), (1, RED)))
d.stat("Queue depth", 'sum(argo_workflows_queue_depth_count) or vector(0)', w=4)
d.stat("Controller up", 'min(up{service=~"argo-workflows.*", namespace="argo-workflows"}) or vector(0)', th=steps((None, RED), (1, GREEN)), w=4)
d.ts("Workflows by status", [('sum by (status) (argo_workflows_count)', "{{status}}")], stack=True, w=12)
d.ts("Operation duration p95", [('histogram_quantile(0.95, sum by (le) (rate(argo_workflows_operation_duration_seconds_bucket[5m])))', "p95")], unit="s", w=12)
d.row("Agent")
d.ts("Webhook alerts received", [('sum by (alertname) (increase(investigations_started_total[15m]))', "{{alertname}}")], w=12)
d.ts("Agent pod memory", [('sum by (pod) (container_memory_working_set_bytes{cluster="observability", namespace="sre-agent", container!=""})', "{{pod}}")], unit="bytes", w=12)
d.logs("Workflow controller logs", '{cluster="observability", namespace="argo-workflows"}', h=8)
d.logs("Investigation step logs", '{cluster="observability", namespace="sre-agent"} | json', h=10)
d.write()

# =====================================================================================================================
# remove dashboards replaced by the ones above
for old in ("platform-overview.yaml", "infrastructure.yaml"):
    p = os.path.join(OUT, old)
    if os.path.exists(p):
        os.remove(p)
print("dashboards written to", OUT)
