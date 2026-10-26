#!/usr/bin/env bash
# Fault injection for the generic alerts and the SRE agent. Each scenario creates a small workload in the sandbox namespace of
# the platform cluster that is broken in one specific way. The workloads are not managed by Argo CD, so nothing heals them:
# delete them with `scripts/faults.sh clear`.
#
#   scripts/faults.sh crashloop    process exits with an error at start        -> ContainerWaiting (CrashLoopBackOff), application level
#   scripts/faults.sh badimage     image tag that does not exist               -> ContainerWaiting (ImagePullBackOff)
#   scripts/faults.sh nosecret     env from a secret that does not exist       -> ContainerWaiting (CreateContainerConfigError)
#   scripts/faults.sh unschedulable asks for 200 Gi of memory                  -> PodUnschedulable, infrastructure (capacity)
#   scripts/faults.sh oom          allocates past its memory limit             -> PodOOMKilled, resource limit
#   scripts/faults.sh errors       backend answers 503, with user traffic      -> IngressHighErrorRate
#   scripts/faults.sh argobad      Argo CD app whose Git path does not exist   -> ArgoAppOutOfSync, manifests in Git
#   scripts/faults.sh node         stops a worker node of the platform cluster -> NodeNotReady, infrastructure   (undo: faults.sh node-up)
#   scripts/faults.sh all          the first seven at once
#   scripts/faults.sh clear        removes everything created here
set -euo pipefail

CTX=${KUBE_CONTEXT:-kind-platform}
HUB=${HUB_CONTEXT:-kind-gitops}
NS=sandbox
k() { kubectl --context "$CTX" -n "$NS" "$@"; }

SEC='securityContext: {runAsNonRoot: true, runAsUser: 10001, runAsGroup: 10001, seccompProfile: {type: RuntimeDefault}}'
CSEC='securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: [ALL]}}'

deploy() { # name image resources env-yaml command-yaml
  local name=$1 image=$2 res=$3 env=${4:-} cmd=${5:-}
  kubectl --context "$CTX" -n "$NS" apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: $name
  namespace: $NS
  labels: {app.kubernetes.io/name: $name, app.kubernetes.io/version: "0.0.1", team: fault-drill}
spec:
  replicas: 1
  selector: {matchLabels: {app.kubernetes.io/name: $name}}
  template:
    metadata:
      labels: {app.kubernetes.io/name: $name, app.kubernetes.io/version: "0.0.1", team: fault-drill}
    spec:
      automountServiceAccountToken: false
      $SEC
      containers:
        - name: app
          image: $image
          $cmd
          $env
          resources: $res
          $CSEC
EOF
}

crashloop()      { deploy fault-crashloop busybox:1.37.0 '{requests: {cpu: 10m, memory: 16Mi}, limits: {memory: 64Mi}}' '' "command: [sh, -c, \"echo 'KeyError: DATABASE_URL is not set' >&2; exit 1\"]"; }
badimage()       { deploy fault-badimage nginxinc/nginx-unprivileged:9.9.9-does-not-exist '{requests: {cpu: 10m, memory: 16Mi}, limits: {memory: 64Mi}}'; }
nosecret()       { deploy fault-nosecret busybox:1.37.0 '{requests: {cpu: 10m, memory: 16Mi}, limits: {memory: 64Mi}}' 'env: [{name: API_KEY, valueFrom: {secretKeyRef: {name: payments-api-key, key: key}}}]' 'command: [sleep, "3600"]'; }
unschedulable()  { deploy fault-unschedulable busybox:1.37.0 '{requests: {cpu: 10m, memory: 200Gi}, limits: {memory: 200Gi}}' '' 'command: [sleep, "3600"]'; }
oom()            { deploy fault-oom python:3.12-alpine '{requests: {cpu: 10m, memory: 32Mi}, limits: {memory: 64Mi}}' '' "command: [python, -c, \"import time; a=[]; time.sleep(20); [(a.append(bytearray(5000000)), time.sleep(0.2)) for _ in range(1000)]\"]"; }

errors() {
  kubectl --context "$CTX" -n "$NS" apply -f - <<EOF
apiVersion: v1
kind: ConfigMap
metadata: {name: fault-errors-nginx, namespace: $NS}
data:
  default.conf: |
    server {
      listen 8080;
      location / { return 503 "backend unavailable\n"; }
    }
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: fault-errors
  namespace: $NS
  labels: {app.kubernetes.io/name: fault-errors, app.kubernetes.io/version: "0.0.1", team: fault-drill}
spec:
  replicas: 1
  selector: {matchLabels: {app.kubernetes.io/name: fault-errors}}
  template:
    metadata:
      labels: {app.kubernetes.io/name: fault-errors, app.kubernetes.io/version: "0.0.1", team: fault-drill}
    spec:
      automountServiceAccountToken: false
      securityContext: {runAsNonRoot: true, runAsUser: 101, runAsGroup: 101, seccompProfile: {type: RuntimeDefault}}
      containers:
        - name: app
          image: nginxinc/nginx-unprivileged:1.27-alpine
          ports: [{name: http, containerPort: 8080}]
          resources: {requests: {cpu: 10m, memory: 16Mi}, limits: {memory: 64Mi}}
          securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: [ALL]}}
          volumeMounts:
            - {name: conf, mountPath: /etc/nginx/conf.d}
            - {name: tmp, mountPath: /tmp}
      volumes:
        - {name: conf, configMap: {name: fault-errors-nginx}}
        - {name: tmp, emptyDir: {}}
---
apiVersion: v1
kind: Service
metadata: {name: fault-errors, namespace: $NS}
spec:
  selector: {app.kubernetes.io/name: fault-errors}
  ports: [{name: http, port: 80, targetPort: http}]
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata: {name: fault-errors, namespace: $NS}
spec:
  ingressClassName: nginx
  rules:
    - host: fault-errors.localhost
      http:
        paths:
          - {path: /, pathType: Prefix, backend: {service: {name: fault-errors, port: {name: http}}}}
EOF
  echo "sending traffic for 12 minutes in the background (pid file /tmp/fault-traffic.pid)"
  ( for _ in $(seq 1 720); do curl -s -o /dev/null -H "Host: fault-errors.localhost" "http://localhost:${INGRESS_PORT:-8080}/"; sleep 1; done ) >/dev/null 2>&1 &
  echo $! > /tmp/fault-traffic.pid
}

argobad() {
  kubectl --context "$HUB" apply -f - <<EOF
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata: {name: fault-bad-path, namespace: argocd}
spec:
  project: prod-workloads
  source:
    repoURL: https://github.com/krishnakant888/k8s-sre-platform.git
    targetRevision: main
    path: gitops/workloads/this-path-does-not-exist
  destination: {name: platform, namespace: sandbox}
EOF
}

node()    { docker stop "${NODE:-platform-worker2}"; }
node_up() { docker start "${NODE:-platform-worker2}"; }

clear_all() {
  k delete deploy,svc,ingress,cm -l team=fault-drill --ignore-not-found
  k delete deploy fault-errors fault-crashloop fault-badimage fault-nosecret fault-unschedulable fault-oom svc/fault-errors ingress/fault-errors cm/fault-errors-nginx --ignore-not-found 2>/dev/null || true
  kubectl --context "$HUB" -n argocd delete application fault-bad-path --ignore-not-found
  [ -f /tmp/fault-traffic.pid ] && kill "$(cat /tmp/fault-traffic.pid)" 2>/dev/null || true
  node_up 2>/dev/null || true
}

case "${1:-}" in
  crashloop) crashloop ;;
  badimage) badimage ;;
  nosecret) nosecret ;;
  unschedulable) unschedulable ;;
  oom) oom ;;
  errors) errors ;;
  argobad) argobad ;;
  node) node ;;
  node-up) node_up ;;
  all) crashloop; badimage; nosecret; unschedulable; oom; errors; argobad ;;
  clear) clear_all ;;
  *) sed -n '2,16p' "$0"; exit 1 ;;
esac
