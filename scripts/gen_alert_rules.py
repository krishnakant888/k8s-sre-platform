"""Generates gitops/observability/alerting/rules-generic.yaml: alert rules that cover every workload, node and cluster
without naming an application. Every query keeps the cluster, namespace and pod labels, so the SRE agent knows where to look."""
import os

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# (group, interval, [rule...]) where rule = (uid, title, expr, for, severity, extra_labels, summary, description)
RULES = [
    ("workloads", [
        ("generic-container-waiting", "ContainerWaiting",
         'max by (cluster, namespace, pod, container, reason) (kube_pod_container_status_waiting_reason{reason=~"CrashLoopBackOff|ImagePullBackOff|ErrImagePull|CreateContainerConfigError|CreateContainerError|InvalidImageName"}) == 1',
         "3m", "ticket", {},
         "{{ $labels.pod }} cannot start: {{ $labels.reason }}",
         "Container {{ $labels.container }} in {{ $labels.namespace }}/{{ $labels.pod }} ({{ $labels.cluster }}) is stuck waiting with {{ $labels.reason }}."),
        ("generic-pod-pending", "PodStuckPending",
         'sum by (cluster, namespace, pod) (kube_pod_status_phase{phase=~"Pending|Unknown"}) > 0',
         "10m", "ticket", {},
         "{{ $labels.pod }} has not started for 10 minutes",
         "Pod {{ $labels.namespace }}/{{ $labels.pod }} ({{ $labels.cluster }}) is Pending or Unknown."),
        ("generic-pod-unschedulable", "PodUnschedulable",
         "max by (cluster, namespace, pod) (kube_pod_status_unschedulable) == 1",
         "5m", "ticket", {},
         "{{ $labels.pod }} cannot be scheduled on any node",
         "The scheduler cannot place {{ $labels.namespace }}/{{ $labels.pod }} ({{ $labels.cluster }}): not enough CPU or memory, a taint, or an affinity rule."),
        ("generic-deployment-rollout-stuck", "DeploymentRolloutStuck",
         "max by (cluster, namespace, deployment) (kube_deployment_status_observed_generation != kube_deployment_metadata_generation)",
         "10m", "ticket", {},
         "Deployment {{ $labels.deployment }} did not finish rolling out",
         "The controller has not caught up with the latest spec of {{ $labels.namespace }}/{{ $labels.deployment }} ({{ $labels.cluster }})."),
        ("generic-statefulset-unavailable", "StatefulSetReplicasNotReady",
         "max by (cluster, namespace, statefulset) (kube_statefulset_status_replicas_ready) < max by (cluster, namespace, statefulset) (kube_statefulset_replicas)",
         "10m", "ticket", {},
         "StatefulSet {{ $labels.statefulset }} has replicas that are not ready",
         "Fewer replicas of {{ $labels.namespace }}/{{ $labels.statefulset }} ({{ $labels.cluster }}) are ready than desired."),
        ("generic-daemonset-unavailable", "DaemonSetPodsUnavailable",
         "max by (cluster, namespace, daemonset) (kube_daemonset_status_number_unavailable) > 0",
         "10m", "ticket", {},
         "DaemonSet {{ $labels.daemonset }} is not running on every node",
         "{{ $labels.namespace }}/{{ $labels.daemonset }} ({{ $labels.cluster }}) has pods unavailable on some nodes."),
        ("generic-job-failed", "JobFailed",
         "max by (cluster, namespace, job_name) (kube_job_status_failed) > 0",
         "1m", "ticket", {},
         "Job {{ $labels.job_name }} failed",
         "{{ $labels.namespace }}/{{ $labels.job_name }} ({{ $labels.cluster }}) has failed pods."),
        ("generic-memory-near-limit", "ContainerMemoryNearLimit",
         'max by (cluster, namespace, pod, container) (container_memory_working_set_bytes{container!=""}) / max by (cluster, namespace, pod, container) (kube_pod_container_resource_limits{resource="memory"}) > 0.9',
         "5m", "ticket", {},
         "{{ $labels.pod }} is using over 90% of its memory limit",
         "Container {{ $labels.container }} of {{ $labels.namespace }}/{{ $labels.pod }} ({{ $labels.cluster }}) will be OOMKilled if it grows further."),
        ("generic-cpu-throttled", "ContainerCpuThrottled",
         'sum by (cluster, namespace, pod, container) (rate(container_cpu_cfs_throttled_periods_total{container!=""}[5m])) / sum by (cluster, namespace, pod, container) (rate(container_cpu_cfs_periods_total{container!=""}[5m])) > 0.5',
         "10m", "ticket", {},
         "{{ $labels.pod }} is CPU throttled more than half the time",
         "Container {{ $labels.container }} of {{ $labels.namespace }}/{{ $labels.pod }} ({{ $labels.cluster }}) hits its CPU limit, which adds latency."),
        ("generic-pvc-filling", "PersistentVolumeFillingUp",
         "min by (cluster, namespace, persistentvolumeclaim) (kubelet_volume_stats_available_bytes / kubelet_volume_stats_capacity_bytes) < 0.1",
         "10m", "ticket", {},
         "Volume {{ $labels.persistentvolumeclaim }} has under 10% free",
         "{{ $labels.namespace }}/{{ $labels.persistentvolumeclaim }} ({{ $labels.cluster }}) is almost full."),
        ("generic-pvc-full", "PersistentVolumeAlmostFull",
         "min by (cluster, namespace, persistentvolumeclaim) (kubelet_volume_stats_available_bytes / kubelet_volume_stats_capacity_bytes) < 0.03",
         "5m", "page", {},
         "Volume {{ $labels.persistentvolumeclaim }} has under 3% free",
         "Writes to {{ $labels.namespace }}/{{ $labels.persistentvolumeclaim }} ({{ $labels.cluster }}) are about to fail."),
        ("generic-pod-failed", "PodFailed",
         'max by (cluster, namespace, pod) (kube_pod_status_phase{phase="Failed"}) == 1',
         "5m", "ticket", {},
         "{{ $labels.pod }} is in phase Failed",
         "Pod {{ $labels.namespace }}/{{ $labels.pod }} ({{ $labels.cluster }}) failed, usually evicted by the kubelet or terminated without a restart policy that brings it back."),
        ("generic-critical-addon-down", "CriticalAddonDown",
         'max by (cluster, namespace, deployment) (kube_deployment_status_replicas_available{namespace=~"kube-system|traefik|cert-manager|argocd|kyverno|argo-workflows|sre-agent"}) == 0',
         "2m", "page", {},
         "{{ $labels.deployment }} has no available replica",
         "{{ $labels.namespace }}/{{ $labels.deployment }} ({{ $labels.cluster }}) is a platform component with no available replica, including when it was scaled to zero."),
        ("generic-target-down", "TargetDown",
         'max by (cluster, namespace, pod, service, job) (up{job!=""}) == 0',
         "5m", "ticket", {},
         "{{ $labels.service }} stopped answering its metrics endpoint",
         "The metrics of {{ $labels.namespace }}/{{ $labels.pod }} ({{ $labels.cluster }}) cannot be scraped: the process is down, hung or blocked by a network policy."),
    ]),
    ("nodes", [
        ("generic-node-not-ready", "NodeNotReady",
         'max by (cluster, node) (kube_node_status_condition{condition="Ready",status="true"}) == 0',
         "2m", "page", {},
         "Node {{ $labels.node }} is not ready",
         "Pods on {{ $labels.node }} ({{ $labels.cluster }}) are not being served: the kubelet stopped reporting or the node is down."),
        ("generic-node-pressure", "NodePressure",
         'max by (cluster, node, condition) (kube_node_status_condition{condition=~"MemoryPressure|DiskPressure|PIDPressure",status="true"}) == 1',
         "2m", "ticket", {},
         "Node {{ $labels.node }} reports {{ $labels.condition }}",
         "The kubelet on {{ $labels.node }} ({{ $labels.cluster }}) is evicting pods or about to."),
        ("generic-node-cpu", "NodeCpuSaturated",
         'sum by (cluster, node) (rate(container_cpu_usage_seconds_total{container!=""}[5m])) / on (cluster, node) sum by (cluster, node) (kube_node_status_allocatable{resource="cpu"}) > 0.9',
         "10m", "ticket", {},
         "Node {{ $labels.node }} uses over 90% of its CPU",
         "Workloads on {{ $labels.node }} ({{ $labels.cluster }}) compete for CPU."),
        ("generic-node-memory", "NodeMemoryNearAllocatable",
         'sum by (cluster, node) (container_memory_working_set_bytes{container!=""}) / on (cluster, node) sum by (cluster, node) (kube_node_status_allocatable{resource="memory"}) > 0.9',
         "10m", "ticket", {},
         "Node {{ $labels.node }} uses over 90% of its memory",
         "{{ $labels.node }} ({{ $labels.cluster }}) is close to evicting pods for memory."),
    ]),
    ("control-plane", [
        ("generic-apiserver-errors", "ApiServerErrors",
         'sum by (cluster) (rate(apiserver_request_total{code=~"5.."}[5m])) / sum by (cluster) (rate(apiserver_request_total[5m])) > 0.05',
         "5m", "page", {"service": "kube-apiserver"},
         "The API server of {{ $labels.cluster }} returns errors",
         "Over 5% of API requests fail with 5xx in {{ $labels.cluster }}: controllers, deployments and Argo CD are affected."),
        ("generic-apiserver-slow", "ApiServerSlow",
         'histogram_quantile(0.99, sum by (cluster, le) (rate(apiserver_request_sli_duration_seconds_bucket{verb!~"WATCH|CONNECT"}[5m]))) > 1',
         "10m", "ticket", {"service": "kube-apiserver"},
         "The API server of {{ $labels.cluster }} is slow",
         "The 99th percentile of API requests in {{ $labels.cluster }} is over one second."),
        ("generic-coredns-errors", "CoreDnsErrors",
         'sum by (cluster) (rate(coredns_dns_responses_total{rcode="SERVFAIL"}[5m])) / sum by (cluster) (rate(coredns_dns_responses_total[5m])) > 0.05',
         "5m", "page", {"service": "coredns"},
         "DNS lookups fail in {{ $labels.cluster }}",
         "Over 5% of DNS answers are SERVFAIL in {{ $labels.cluster }}: services cannot find each other."),
    ]),
    # The gateway (Traefik) of every cluster: what users see on each route, what the gateway answers itself, and the
    # certificate it serves. The route rules read the router series, which count the 503 of a route with no backend.
    ("gateway", [
        ("generic-ingress-errors", "IngressHighErrorRate",
         'sum by (cluster, namespace, ingress) (rate(traefik_router_requests_total{ingress!="", code=~"5.."}[5m])) / sum by (cluster, namespace, ingress) (rate(traefik_router_requests_total{ingress!=""}[5m])) > 0.05 and sum by (cluster, namespace, ingress) (rate(traefik_router_requests_total{ingress!=""}[5m])) > 0.05',
         "5m", "page", {},
         "{{ $labels.ingress }} returns errors to users",
         "Over 5% of requests through the gateway route {{ $labels.namespace }}/{{ $labels.ingress }} ({{ $labels.cluster }}) end in 5xx."),
        # More than 5% of the requests that finish within 10 s take over 1 s (the 95th percentile of those is over one second).
        # Requests longer than 10 s are left out on purpose: they are streams (the live views of Argo CD, Grafana and the
        # Workflows UI keep a connection open for minutes), and the gateway records a stream's whole life as one request.
        ("generic-ingress-slow", "IngressSlow",
         '(sum by (cluster, namespace, ingress) (rate(traefik_router_request_duration_seconds_bucket{ingress!="", le=~"10|10.0"}[5m])) - sum by (cluster, namespace, ingress) (rate(traefik_router_request_duration_seconds_bucket{ingress!="", le=~"1|1.0"}[5m]))) / sum by (cluster, namespace, ingress) (rate(traefik_router_request_duration_seconds_bucket{ingress!="", le=~"10|10.0"}[5m])) > 0.05 and sum by (cluster, namespace, ingress) (rate(traefik_router_request_duration_seconds_bucket{ingress!="", le=~"10|10.0"}[5m])) > 0.05',
         "5m", "ticket", {},
         "Route {{ $labels.ingress }} is slow",
         "Over 5% of the requests through {{ $labels.namespace }}/{{ $labels.ingress }} ({{ $labels.cluster }}) take longer than one second (requests over 10 s are ignored, they are streams)."),
        ("generic-gateway-server-errors", "GatewayServerErrors",
         'sum by (cluster, entrypoint) (rate(traefik_entrypoint_requests_total{code=~"5.."}[5m])) / sum by (cluster, entrypoint) (rate(traefik_entrypoint_requests_total[5m])) > 0.05 and sum by (cluster, entrypoint) (rate(traefik_entrypoint_requests_total[5m])) > 0.1',
         "5m", "page", {"service": "traefik", "namespace": "traefik"},
         "The gateway of {{ $labels.cluster }} returns errors on {{ $labels.entrypoint }}",
         "Over 5% of the requests the gateway of {{ $labels.cluster }} answers on the {{ $labels.entrypoint }} entrypoint end in 5xx, counting the errors it produces itself (a route with no backend)."),
        ("generic-gateway-config-reload", "GatewayConfigReloadFailing",
         "sum by (cluster) (increase(traefik_config_reloads_failure_total[10m])) > 0",
         "1m", "ticket", {"service": "traefik", "namespace": "traefik"},
         "The gateway of {{ $labels.cluster }} cannot load its configuration",
         "Traefik failed to apply a new configuration in the last 10 minutes: a route, a middleware or the Gateway it was given is invalid, so changes to routes are not live."),
        ("generic-gateway-cert-not-ready", "GatewayCertificateNotReady",
         'max by (cluster, namespace, name, issuer_name) (certmanager_certificate_ready_status{condition!="True"}) == 1',
         "10m", "ticket", {},
         "Certificate {{ $labels.name }} is not ready",
         "cert-manager has not issued {{ $labels.namespace }}/{{ $labels.name }} ({{ $labels.cluster }}, issuer {{ $labels.issuer_name }}): the gateway cannot serve or renew TLS with it."),
        ("generic-gateway-cert-expiring", "GatewayCertificateExpiring",
         "max by (cluster, namespace, name) (certmanager_certificate_expiration_timestamp_seconds) < time() + 14 * 86400",
         "10m", "ticket", {},
         "Certificate {{ $labels.name }} expires within 14 days",
         "{{ $labels.namespace }}/{{ $labels.name }} ({{ $labels.cluster }}) expires within 14 days and has not been renewed: cert-manager is not renewing it, or its lifetime is very short."),
    ]),
    ("gitops", [
        ("generic-argocd-out-of-sync", "ArgoAppOutOfSync",
         'max by (cluster, name, dest_server, project) (argocd_app_info{sync_status!="Synced"}) == 1',
         "15m", "ticket", {"service": "argocd"},
         "Argo CD app {{ $labels.name }} is out of sync",
         "{{ $labels.name }} (project {{ $labels.project }}, destination {{ $labels.dest_server }}) differs from Git for 15 minutes."),
        ("generic-argocd-unhealthy", "ArgoAppUnhealthy",
         'max by (cluster, name, dest_server, project, health_status) (argocd_app_info{health_status=~"Degraded|Missing"}) == 1',
         "5m", "ticket", {"service": "argocd"},
         "Argo CD app {{ $labels.name }} is {{ $labels.health_status }}",
         "The resources of {{ $labels.name }} (destination {{ $labels.dest_server }}) are not healthy."),
        ("generic-argocd-sync-failing", "ArgoSyncFailing",
         'sum by (cluster, name, dest_server) (increase(argocd_app_sync_total{phase=~"Error|Failed"}[15m])) > 0',
         "1m", "ticket", {"service": "argocd"},
         "Argo CD sync of {{ $labels.name }} is failing",
         "A sync of {{ $labels.name }} (destination {{ $labels.dest_server }}) ended in an error in the last 15 minutes."),
    ]),
    ("telemetry", [
        ("generic-telemetry-missing-platform", "ClusterTelemetryMissing",
         'vector(1) unless on () count(up{job="alloy",cluster="platform"})',
         "5m", "page", {"cluster": "platform", "service": "alloy"},
         "No telemetry from the platform cluster",
         "The collector of the platform cluster stopped sending metrics: the cluster, its network path or the collector is down. Everything else in this view is stale until it is back."),
        ("generic-telemetry-missing-gitops", "ClusterTelemetryMissing",
         'vector(1) unless on () count(up{job="alloy",cluster="gitops"})',
         "5m", "page", {"cluster": "gitops", "service": "alloy"},
         "No telemetry from the gitops cluster",
         "The collector of the gitops cluster stopped sending metrics: Argo CD, which deploys everything, may be down."),
        ("generic-workflow-failed", "InvestigationWorkflowFailed",
         'max by (cluster) (increase(argo_workflows_total_count{phase=~"Failed|Error"}[10m])) > 0',
         "1m", "ticket", {"service": "sre-agent", "namespace": "sre-agent"},
         "An incident investigation workflow failed",
         "A workflow of the SRE agent ended in Failed or Error: an alert has no report, or only a partial one."),
        ("generic-remote-write-failing", "TelemetryNotDelivered",
         "sum by (cluster) (rate(prometheus_remote_storage_samples_failed_total[5m])) > 0",
         "5m", "ticket", {"service": "alloy"},
         "Metrics from {{ $labels.cluster }} are not reaching Mimir",
         "The collector of {{ $labels.cluster }} fails to write samples: Mimir is down or unreachable, or a network policy blocks it."),
        ("generic-log-write-dropping", "LogsDropped",
         "sum by (cluster) (rate(loki_write_dropped_entries_total[5m])) > 0",
         "5m", "ticket", {"service": "alloy"},
         "Logs from {{ $labels.cluster }} are being dropped",
         "The collector of {{ $labels.cluster }} drops log lines: Loki is down, unreachable or rate limiting."),
    ]),
]


def q(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


out = ["""# Alert rules that cover every workload, node and cluster, with no application named. Generated by scripts/gen_alert_rules.py;
# edit the script, not this file. Every query keeps the cluster, namespace and pod labels, which the SRE agent uses to
# decide where to look. The SLO rules of orders-api are in rules-orders-api.yaml.
apiVersion: v1
kind: ConfigMap
metadata:
  name: alerting-rules-generic
  namespace: observability
  labels:
    grafana_alert: "1"
data:
  rules-generic.yaml: |
    apiVersion: 1
    groups:"""]
for group, rules in RULES:
    out.append(f"      - orgId: 1\n        name: {group}\n        folder: Alerts\n        interval: 1m\n        rules:")
    for uid, title, expr, for_, sev, extra, summary, desc in rules:
        labels = {"severity": sev, **extra}
        lab = ", ".join(f"{k}: {v}" for k, v in labels.items())
        out.append(f"""          - uid: {uid}
            title: {title}
            condition: C
            data:
              - refId: A
                relativeTimeRange: {{from: 600, to: 0}}
                datasourceUid: mimir
                model: {{refId: A, expr: "{q(expr)}", instant: true}}
              - refId: C
                datasourceUid: __expr__
                model: {{refId: C, type: math, expression: "$A >= 0"}}
            noDataState: OK
            execErrState: Error
            for: {for_}
            labels: {{{lab}}}
            annotations:
              summary: "{q(summary)}"
              description: "{q(desc)}\"""")
open("gitops/observability/alerting/rules-generic.yaml", "w").write("\n".join(out) + "\n")
print(sum(len(r) for _, r in RULES), "rules")
