#!/usr/bin/env python3
"""Self-service changes behind the developer portal. The portal's templates start the `selfservice` workflow, which runs this
script on a fresh checkout and opens a pull request with what changed. Nothing here touches a cluster: every change is a file in Git
that Argo CD applies after the merge.

    selfservice.py <action> '<json parameters>'

    scale        service, replicas                                   replicas of a Deployment
    resources    service, memory_limit, [memory_request, cpu_request] container resources
    env          service, name, value                                a plain environment variable (secrets are refused)
    image        service, tag                                        the tag of the image
    rollback     service                                             the image tag that was deployed before the current one
    route        service, host                                       an extra host name on the service's HTTPRoute (*.localhost)
    promote      service                                             move a service from the sandbox namespace to demo
    alert        service, kind, [threshold, severity]                an alert rule for the service
    dashboard    service, [panels]                                   a Grafana dashboard for the service
    sandbox      name, owner, days, [cpu, memory]                    a short-lived namespace with quota and network policy
    access       user, namespace, days                               read-only access (the `view` role) to a namespace for a while
    drill        scenario, minutes                                   a broken workload that the alerts and the SRE agent should catch
    expire                                                           remove sandboxes, access and drills whose time is up

It prints one JSON object (branch, title, body, files) for the workflow. A request it cannot do safely fails with exit code 2 and the reason.
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(os.environ.get("SELFSERVICE_ROOT") or Path(__file__).resolve().parent.parent)
NAME = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
EXPIRES = re.compile(r"^# sre-platform/expires: (\S+)\s*$", re.M)
RESERVED = {"default", "kube-system", "kube-public", "kube-node-lease", "demo", "sandbox", "scenarios", "observability", "traefik",
            "cert-manager", "argocd", "kyverno", "sre-agent", "portal", "sealed-secrets", "argo-workflows", "local-path-storage"}
SCENARIOS = ["bad-image", "bad-probe", "crashloop", "oom", "pending"]


class Refused(Exception):
    pass


def need(p, key, pattern=None, kind=str):
    value = p.get(key)
    if value in (None, ""):
        raise Refused(f"`{key}` is required")
    if kind is int:
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise Refused(f"`{key}` must be a whole number")
    else:
        value = str(value).strip()
        if pattern and not re.fullmatch(pattern, value):
            raise Refused(f"`{key}` has an unsupported value: {value[:60]!r}")
    return value


def service_name(p):
    return need(p, "service", NAME.pattern)


# ---------------------------------------------------------------------------------------------------------------------
# finding and editing manifests as text, so comments and the flow style of the repo stay as written. Every edit is checked after
# by parsing both versions and comparing them, so an edit that changed anything else is refused.

def split_docs(text):
    parts = re.split(r"(^---[ \t]*\n)", text, flags=re.M)
    return parts  # documents at even positions, separators at odd ones


def load(text):
    return yaml.safe_load(text)


def find(kind, name, folder="gitops/workloads"):
    """(path, parts, index) of the object of that kind and name."""
    for path in sorted((ROOT / folder).rglob("*.yaml")):
        parts = split_docs(path.read_text())
        for i in range(0, len(parts), 2):
            try:
                doc = load(parts[i])
            except yaml.YAMLError:
                continue
            if isinstance(doc, dict) and doc.get("kind") == kind and doc.get("metadata", {}).get("name") == name:
                return path, parts, i
    return None, None, None


def find_deployment(name):
    path, parts, i = find("Deployment", name)
    if not path:
        raise Refused(f"no Deployment named `{name}` under gitops/workloads. Only services deployed by Argo CD from this repo can be changed here")
    return path, parts, i


def diff(a, b, path=""):
    """Paths at which two parsed documents differ."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b), key=str):
            out += diff(a.get(k), b.get(k), f"{path}/{k}")
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out += diff(x, y, f"{path}/{i}")
        return out
    return [] if a == b else [path or "/"]


def write_doc(path, parts, i, new_text, allowed):
    """Writes the document back after checking that only the paths that start with one of `allowed` changed."""
    before, after = load(parts[i]), load(new_text)
    changed = diff(before, after)
    stray = [c for c in changed if not any(c.startswith(a) for a in allowed)]
    if stray:
        raise Refused(f"the edit would change more than asked ({stray[0]}); make this change by hand")
    if not changed:
        raise Refused("nothing to change: the file already has this value")
    parts[i] = new_text
    path.write_text("".join(parts))
    return changed


def container_span(text):
    """Start and end of the first container block (the lines of its list item) in a Deployment document."""
    m = re.search(r"^(\s*)containers:\s*\n(\s*)- ", text, flags=re.M)
    if not m:
        raise Refused("this Deployment has no containers list in the usual layout")
    start = m.start(2)
    indent = len(m.group(2))
    end = len(text)
    for nxt in re.compile(rf"^ {{{indent}}}- |^ {{0,{indent - 1}}}\S", re.M).finditer(text, m.end(2) + 2):
        end = nxt.start()
        break
    return start, end


def replace_in_container(text, pattern, repl, what):
    s, e = container_span(text)
    block, n = re.subn(pattern, repl, text[s:e], count=1, flags=re.M)
    if not n:
        raise Refused(f"cannot find {what} in the container of this Deployment")
    return text[:s] + block + text[e:]


def flow(d):
    return "{" + ", ".join(f"{k}: {v}" for k, v in d.items()) + "}"


# ---------------------------------------------------------------------------------------------------------------------

def act_scale(p):
    name, n = service_name(p), need(p, "replicas", kind=int)
    if not 1 <= n <= 10:
        raise Refused("replicas must be between 1 and 10 (to remove a service, delete its files in a pull request)")
    path, parts, i = find_deployment(name)
    text, k = re.subn(r"^(  replicas: )\d+", rf"\g<1>{n}", parts[i], count=1, flags=re.M)
    if not k:
        raise Refused("this Deployment has no `replicas` line at the usual place")
    write_doc(path, parts, i, text, ["/spec/replicas"])
    return dict(files=[path], title=f"{name}: scale to {n} replicas", body=f"Sets `spec.replicas` of `{name}` to {n}.")


MEM = r"\d{1,5}(Mi|Gi)"
CPU = r"\d{1,5}m|\d{1,2}"


def to_bytes(v):
    return int(v[:-2]) * (1024 ** 2 if v.endswith("Mi") else 1024 ** 3)


def act_resources(p):
    name = service_name(p)
    limit = need(p, "memory_limit", MEM)
    req_mem = p.get("memory_request") or None
    req_cpu = p.get("cpu_request") or None
    if req_mem and not re.fullmatch(MEM, str(req_mem)):
        raise Refused("`memory_request` must look like 128Mi or 1Gi")
    if req_cpu and not re.fullmatch(CPU, str(req_cpu)):
        raise Refused("`cpu_request` must look like 100m or 1")
    path, parts, i = find_deployment(name)
    text = parts[i]
    s, e = container_span(text)
    block = text[s:e]
    m_req = re.search(r"^(\s*)requests: \{(.*)\}[ \t]*$", block, flags=re.M)
    m_lim = re.search(r"^(\s*)limits: \{(.*)\}[ \t]*$", block, flags=re.M)
    if not (m_req and m_lim):
        raise Refused("the resources of this container are not written as `requests: {...}` and `limits: {...}`; change them by hand")
    requests, limits = yaml.safe_load("{" + m_req.group(2) + "}"), yaml.safe_load("{" + m_lim.group(2) + "}")
    limits["memory"] = limit
    if req_mem:
        requests["memory"] = req_mem
    if req_cpu:
        requests["cpu"] = req_cpu
    if to_bytes(str(requests["memory"])) > to_bytes(limit):
        raise Refused(f"the memory request ({requests['memory']}) cannot be larger than the limit ({limit})")
    block = re.sub(r"^(\s*requests: )\{.*\}", lambda m: m.group(1) + flow(requests), block, count=1, flags=re.M)
    block = re.sub(r"^(\s*limits: )\{.*\}", lambda m: m.group(1) + flow(limits), block, count=1, flags=re.M)
    write_doc(path, parts, i, text[:s] + block + text[e:], ["/spec/template/spec/containers/0/resources"])
    return dict(files=[path], title=f"{name}: memory limit {limit}",
                body=f"Resources of `{name}`: requests {flow(requests)}, limits {flow(limits)}.")


SECRETISH = re.compile(r"(secret|token|password|passwd|credential|api[_-]?key|private)", re.I)


def act_env(p):
    name, var = service_name(p), need(p, "name", r"[A-Z][A-Z0-9_]{0,62}")
    value = str(p.get("value", ""))
    if len(value) > 300 or "\n" in value:
        raise Refused("the value must be one line of at most 300 characters")
    if SECRETISH.search(var):
        raise Refused(f"`{var}` looks like a secret. Secrets do not go into Git as plain text: add a SealedSecret (docs/secrets.md) and reference it")
    path, parts, i = find_deployment(name)
    text = parts[i]
    s, e = container_span(text)
    block = text[s:e]
    entry = "{name: %s, value: %s}" % (var, json.dumps(value))
    existing = re.search(rf"^(\s*- )\{{name: {re.escape(var)}, value: .*\}}[ \t]*$", block, flags=re.M)
    if existing:
        block = block[:existing.start()] + existing.group(1) + entry + block[existing.end():]
    elif re.search(rf"name: {re.escape(var)}\b", block):
        raise Refused(f"`{var}` is already set in a form this script does not rewrite (valueFrom or several lines); change it by hand")
    else:
        m = re.search(r"^(\s*)env:[ \t]*(#.*)?\n", block, flags=re.M)
        if m:
            item_indent = " " * (len(m.group(1)) + 2)
            pos = m.end()
            lines = block[pos:].splitlines(keepends=True)
            used = 0
            for line in lines:
                if line.startswith(item_indent) or not line.strip():
                    used += len(line)
                else:
                    break
            block = block[:pos] + block[pos:pos + used].rstrip("\n") + f"\n{item_indent}- {entry}\n" + block[pos + used:]
        else:
            im = re.search(r"^(\s*)image: .*\n", block, flags=re.M)
            if not im:
                raise Refused("cannot find the image line to add the variable after")
            ind = im.group(1)
            pull = re.compile(rf"^{ind}imagePullPolicy: .*\n", re.M).match(block, im.end())
            at = pull.end() if pull else im.end()
            block = block[:at] + f"{ind}env:\n{ind}  - {entry}\n" + block[at:]
    new = text[:s] + block + text[e:]
    write_doc(path, parts, i, new, ["/spec/template/spec/containers/0/env"])
    return dict(files=[path], title=f"{name}: set {var}", body=f"Sets the environment variable `{var}` of `{name}` to `{value}`.")


def current_image(text):
    m = re.search(r"^\s*image: (\S+)\s*$", text[slice(*container_span(text))], flags=re.M)
    if not m:
        raise Refused("cannot find the image of the container")
    return m.group(1)


def set_image(name, new_image):
    path, parts, i = find_deployment(name)
    text = parts[i]
    old = current_image(text)
    new = replace_in_container(text, r"^(\s*image: )\S+", lambda m: m.group(1) + new_image, "the image line")
    write_doc(path, parts, i, new, ["/spec/template/spec/containers/0/image"])
    return path, old


def act_image(p):
    name = service_name(p)
    tag = need(p, "tag", r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")
    if tag == "latest":
        raise Refused("Kyverno refuses the `latest` tag: use a version or a commit tag")
    path, parts, i = find_deployment(name)
    old = current_image(parts[i])
    repo = old.rsplit(":", 1)[0] if re.search(r":[^/]+$", old) else old
    path, old = set_image(name, f"{repo}:{tag}")
    return dict(files=[path], title=f"{name}: deploy {tag}", body=f"Image of `{name}`: `{old}` -> `{repo}:{tag}`.")


def act_rollback(p):
    name = service_name(p)
    path, parts, i = find_deployment(name)
    now = current_image(parts[i])
    rel = path.relative_to(ROOT).as_posix()
    revs = subprocess.run(["git", "-C", str(ROOT), "log", "--format=%H", "-n", "60", "--", rel], capture_output=True, text=True).stdout.split()
    for rev in revs:
        old = subprocess.run(["git", "-C", str(ROOT), "show", f"{rev}:{rel}"], capture_output=True, text=True).stdout
        for part in split_docs(old)[::2]:
            try:
                d = load(part)
            except yaml.YAMLError:
                continue
            if isinstance(d, dict) and d.get("kind") == "Deployment" and d["metadata"]["name"] == name:
                img = d["spec"]["template"]["spec"]["containers"][0]["image"]
                if img != now:
                    _, was = set_image(name, img)
                    return dict(files=[path], title=f"{name}: roll back to {img.rsplit(':', 1)[-1][:19]}",
                                body=f"Rolls `{name}` back: `{was}` -> `{img}`, the image of the previous change to this file ({rev[:9]}).")
    raise Refused(f"no earlier image of `{name}` in the last 60 changes of {rel}")


HOST = r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*\.localhost"


def act_route(p):
    name, host = service_name(p), need(p, "host", HOST)
    path, parts, i = find("HTTPRoute", name)
    if not path:
        raise Refused(f"no HTTPRoute named `{name}` under gitops/workloads")
    text = parts[i]
    m = re.search(r"^(\s*hostnames: )\[(.*)\]\s*$", text, flags=re.M)
    if not m:
        raise Refused("the hostnames of this route are not written as `hostnames: [a, b]`")
    hosts = [h.strip() for h in m.group(2).split(",") if h.strip()]
    if host in hosts:
        raise Refused(f"{host} is already a host of this route")
    for other in (ROOT / "gitops").rglob("*.yaml"):
        if re.search(rf"hostnames:.*[\[ ,]{re.escape(host)}[\],]", other.read_text()):
            raise Refused(f"{host} is already used by {other.relative_to(ROOT)}")
    text = text[:m.start()] + m.group(1) + "[" + ", ".join(hosts + [host]) + "]" + text[m.end():]
    write_doc(path, parts, i, text, ["/spec/hostnames"])
    return dict(files=[path], title=f"{name}: serve {host}",
                body=f"Adds `{host}` to the HTTPRoute `{name}`. The gateway's listener already carries a certificate for `*.localhost`, so https works on it.")


def act_promote(p):
    name = service_name(p)
    touched = []
    for path in sorted((ROOT / "gitops/workloads" / name).glob("*.yaml")) if (ROOT / "gitops/workloads" / name).is_dir() else []:
        text = path.read_text()
        new = re.sub(r"^(\s*namespace: )sandbox\s*$", r"\1demo", text, flags=re.M)
        new = new.replace("kubernetes.io/metadata.name: sandbox", "kubernetes.io/metadata.name: demo")
        if new != text:
            path.write_text(new)
            touched.append(path)
    app = ROOT / "gitops/apps" / f"{name}.yaml"
    if app.exists():
        text = app.read_text()
        new = re.sub(r"(destination:\n(?:.*\n)*?\s*namespace: )sandbox", r"\1demo", text)
        if new != text:
            app.write_text(new)
            touched.append(app)
    if not touched:
        raise Refused(f"`{name}` has no manifests in the sandbox namespace under gitops/workloads/{name}")
    return dict(files=touched, title=f"{name}: promote from sandbox to demo",
                body=f"Moves `{name}` to the `demo` namespace (restricted Pod Security, Kyverno enforced). The pods are replaced by Argo CD after the merge.\n\n"
                     "Check before merging: the image runs as non-root, has a memory limit, a version tag and the standard labels (Kyverno refuses it otherwise).")


# ---- alerts and dashboards --------------------------------------------------------------------------------------------

ALERTS = {
    "error-rate": ("{svc} returns many 5xx", 0.05, "5m",
                   'sum by (cluster, namespace, ingress) (rate(traefik_router_requests_total{{namespace="{ns}", ingress=~"{svc}.*", code=~"5.."}}[5m])) / sum by (cluster, namespace, ingress) (rate(traefik_router_requests_total{{namespace="{ns}", ingress=~"{svc}.*"}}[5m])) > {t}',
                   "More than {pct} of the requests to {svc} through the gateway end in a 5xx."),
    "latency": ("{svc} is slow", 1.0, "10m",
                'histogram_quantile(0.95, sum by (cluster, namespace, ingress, le) (rate(traefik_router_request_duration_seconds_bucket{{namespace="{ns}", ingress=~"{svc}.*"}}[5m]))) > {t}',
                "The 95th percentile of the requests to {svc} through the gateway is above {t} seconds."),
    "availability": ("{svc} has too few ready replicas", 1, "3m",
                     'max by (cluster, namespace, deployment) (kube_deployment_status_replicas_available{{namespace="{ns}", deployment="{svc}"}}) < {t}',
                     "Fewer than {t} replicas of {svc} are available."),
    "restarts": ("{svc} keeps restarting", 3, "5m",
                 'sum by (cluster, namespace, pod) (increase(kube_pod_container_status_restarts_total{{namespace="{ns}", pod=~"{svc}-[a-z0-9]+-[a-z0-9]+"}}[15m])) > {t}',
                 "Containers of {svc} restarted more than {t} times in 15 minutes."),
    "memory": ("{svc} is close to its memory limit", 0.9, "5m",
               'max by (cluster, namespace, pod, container) (container_memory_working_set_bytes{{namespace="{ns}", pod=~"{svc}-[a-z0-9]+-[a-z0-9]+", container!=""}}) / max by (cluster, namespace, pod, container) (kube_pod_container_resource_limits{{namespace="{ns}", pod=~"{svc}-[a-z0-9]+-[a-z0-9]+", resource="memory"}}) > {t}',
               "A container of {svc} uses more than {t} of its memory limit and will be OOMKilled if it grows."),
}


def workload_namespace(name):
    path, parts, i = find_deployment(name)
    return load(parts[i])["metadata"]["namespace"]


def q(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def act_alert(p):
    name, kind = service_name(p), need(p, "kind", "|".join(ALERTS))
    sev = p.get("severity") or "ticket"
    if sev not in ("ticket", "page"):
        raise Refused("severity is ticket or page")
    title, default, for_, expr, desc = ALERTS[kind]
    raw = p.get("threshold")
    try:
        t = float(raw) if raw not in (None, "") else float(default)
    except ValueError:
        raise Refused("`threshold` must be a number")
    if not 0 < t < 100000:
        raise Refused("`threshold` is out of range")
    t = int(t) if float(t).is_integer() and kind in ("availability", "restarts") else t
    ns = workload_namespace(name)
    out = ROOT / "gitops/observability/alerting" / f"request-{name}-{kind}.yaml"
    if out.exists():
        raise Refused(f"there is already a {kind} alert for {name}: edit {out.relative_to(ROOT)}")
    alert = "".join(w.capitalize() for w in re.split(r"[-_]", name)) + "".join(w.capitalize() for w in kind.split("-"))
    fmt = dict(svc=name, ns=ns, t=t, pct=f"{t:.0%}")
    uid = f"req-{name}-{kind}"[:40]
    out.write_text(f"""# Requested from the developer portal (scripts/selfservice.py alert). Alert rule for {name}: {kind}.
apiVersion: v1
kind: ConfigMap
metadata:
  name: alerting-request-{name}-{kind}
  namespace: observability
  labels:
    grafana_alert: "1"
data:
  request-{name}-{kind}.yaml: |
    apiVersion: 1
    groups:
      - orgId: 1
        name: requested-{name}
        folder: Alerts
        interval: 1m
        rules:
          - uid: {uid}
            title: {alert}
            condition: C
            data:
              - refId: A
                relativeTimeRange: {{from: 600, to: 0}}
                datasourceUid: mimir
                model: {{refId: A, expr: "{q(expr.format(**fmt))}", instant: true}}
              - refId: C
                datasourceUid: __expr__
                model: {{refId: C, type: math, expression: "$A >= 0"}}
            noDataState: OK
            execErrState: Error
            for: {for_}
            labels: {{severity: {sev}, service: {name}}}
            annotations:
              summary: "{q(title.format(**fmt))}"
              description: "{q(desc.format(**fmt))}"
""")
    load(out.read_text())
    return dict(files=[out], title=f"{name}: alert {alert}",
                body=f"New Grafana alert rule `{alert}` ({kind}, threshold {t}, severity {sev}) for `{name}` in `{ns}`.\n\n"
                     f"Query: `{expr.format(**fmt)}`\n\nWhen it fires, the SRE agent investigates and writes a report like for any other alert.")


def panel(pid, title, expr, unit, x, y, ds="mimir", kind="timeseries", legend=""):
    t = {"refId": "A", "expr": expr, "datasource": {"type": "prometheus" if ds == "mimir" else "loki", "uid": ds}}
    if legend:
        t["legendFormat"] = legend
    return {"id": pid, "type": kind, "title": title, "datasource": t["datasource"], "targets": [t],
            "gridPos": {"h": 8, "w": 12, "x": x, "y": y}, "fieldConfig": {"defaults": {"unit": unit}, "overrides": []}}


def act_dashboard(p):
    name = service_name(p)
    ns = workload_namespace(name)
    out = ROOT / "gitops/observability/dashboards" / f"service-{name}.yaml"
    if out.exists():
        raise Refused(f"there is already a dashboard for {name}: edit {out.relative_to(ROOT)}")
    pod = f'namespace="{ns}", pod=~"{name}-[a-z0-9]+-[a-z0-9]+"'
    ing = f'namespace="{ns}", ingress=~"{name}.*"'
    panels = [
        panel(1, "Available replicas", f'max(kube_deployment_status_replicas_available{{namespace="{ns}", deployment="{name}"}})', "short", 0, 0),
        panel(2, "Restarts (5m)", f'sum by (pod) (increase(kube_pod_container_status_restarts_total{{{pod}}}[5m]))', "short", 12, 0, legend="{{pod}}"),
        panel(3, "CPU (cores)", f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{pod}, container!=""}}[5m]))', "short", 0, 8, legend="{{pod}}"),
        panel(4, "Memory working set", f'sum by (pod) (container_memory_working_set_bytes{{{pod}, container!=""}})', "bytes", 12, 8, legend="{{pod}}"),
        panel(5, "Requests / s by status", f'sum by (code) (rate(traefik_router_requests_total{{{ing}}}[5m]))', "reqps", 0, 16, legend="{{code}}"),
        panel(6, "Latency p95", f'histogram_quantile(0.95, sum by (le) (rate(traefik_router_request_duration_seconds_bucket{{{ing}}}[5m])))', "s", 12, 16),
        panel(7, "Log lines by level", f'sum by (level) (rate({{namespace="{ns}", service="{name}"}}[1m]))', "short", 0, 24, ds="loki", legend="{{level}}"),
        panel(8, "Warnings and errors", f'{{namespace="{ns}", service="{name}", level=~"warn|error|fatal"}}', "short", 12, 24, ds="loki", kind="logs"),
    ]
    for n, extra in enumerate(p.get("panels") or [], start=9):
        title, expr = str(extra.get("title", ""))[:80], str(extra.get("expr", ""))[:600]
        if not title or not expr:
            raise Refused("each extra panel needs a title and a PromQL expression")
        panels.append(panel(n, title, expr, str(extra.get("unit") or "short"), 0 if n % 2 else 12, 32 + 8 * ((n - 9) // 2)))
    uid = f"svc-{name}"[:40]
    dash = {"uid": uid, "title": f"{name}", "description": f"Dashboard of {name} (requested from the portal)", "tags": ["service", "portal"], "schemaVersion": 39, "version": 1,
            "editable": True, "refresh": "30s", "time": {"from": "now-1h", "to": "now"}, "timezone": "browser", "panels": panels}
    body = "\n".join("    " + line for line in json.dumps(dash, indent=2).split("\n"))
    out.write_text(f"# Requested from the developer portal (scripts/selfservice.py dashboard).\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: dashboard-{uid}\n"
                   f"  namespace: observability\n  labels:\n    grafana_dashboard: \"1\"\n  annotations:\n    grafana_folder: Services\ndata:\n  {uid}.json: |\n{body}\n")
    load(out.read_text())
    return dict(files=[out], title=f"{name}: Grafana dashboard", body=f"A dashboard for `{name}` in `{ns}` (folder Services): replicas, restarts, CPU, memory, gateway traffic and latency, logs"
                + (f", and {len(p['panels'])} extra panel(s)." if p.get("panels") else "."))


# ---- things that expire ------------------------------------------------------------------------------------------------

def expiry(hours=None, days=None):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours or 0, days=days or 0)).strftime("%Y-%m-%dT%H:%M:%SZ")


def act_sandbox(p):
    short, owner = need(p, "name", NAME.pattern), need(p, "owner", r"[A-Za-z0-9:/_.@-]{1,80}")
    days = need(p, "days", kind=int)
    cpu, mem = str(p.get("cpu") or "2"), str(p.get("memory") or "2Gi")
    if not 1 <= days <= 14:
        raise Refused("a sandbox lives 1 to 14 days; ask again to extend it")
    if not re.fullmatch(r"\d{1,2}", cpu) or not re.fullmatch(MEM, mem):
        raise Refused("cpu is whole cores (2) and memory looks like 2Gi")
    ns = f"sbx-{short}"
    if ns in RESERVED or len(ns) > 50:
        raise Refused("that name is not allowed")
    out = ROOT / "gitops/platform/base" / f"sandbox-{short}.yaml"
    if out.exists():
        raise Refused(f"the sandbox {ns} exists: {out.relative_to(ROOT)}")
    base = (ROOT / "gitops/platform/base/sandbox.yaml").read_text()
    base = re.sub(r"\A(#.*\n)+", "", base)
    base = re.sub(r"(name|namespace): sandbox\b", rf"\1: {ns}", base)
    base = base.replace("kubernetes.io/metadata.name: sandbox", f"kubernetes.io/metadata.name: {ns}")
    base = base.replace("    app.kubernetes.io/part-of: platform\n", f"    app.kubernetes.io/part-of: platform\n    sre-platform/owner: \"{owner.replace('group:default/', '').replace('group:', '').replace('/', '_')}\"\n", 1)
    quota = f"""---
apiVersion: v1
kind: ResourceQuota
metadata:
  name: sandbox-quota
  namespace: {ns}
spec:
  hard: {{requests.cpu: "{cpu}", limits.memory: {mem}, pods: "20", services: "10"}}
"""
    out.write_text(f"# sre-platform/expires: {expiry(days=days)}\n# A sandbox namespace requested from the developer portal (scripts/selfservice.py sandbox). It is removed when the time above passes.\n{base.rstrip()}\n{quota}")
    for d in split_docs(out.read_text())[::2]:
        load(d)
    return dict(files=[out], title=f"sandbox {ns} for {days} days",
                body=f"Namespace `{ns}` for `{owner}`: Pod Security restricted, default-deny network policy, DNS and gateway access, quota of {cpu} CPU and {mem} memory.\n\n"
                     f"It is deleted automatically after {days} day(s) (the `selfservice` workflow removes the file, Argo CD prunes the namespace). "
                     f"To deploy into it, add `{ns}` to a service with the New service template.")


def act_access(p):
    user, ns = need(p, "user", r"[a-z][a-z0-9._@-]{1,60}"), need(p, "namespace", r"[a-z][a-z0-9-]{1,50}")
    days = need(p, "days", kind=int)
    if not 1 <= days <= 30:
        raise Refused("access lasts 1 to 30 days")
    known = {"demo", "sandbox", "sre-agent"} | {"sbx-" + f.stem.removeprefix("sandbox-") for f in (ROOT / "gitops/platform/base").glob("sandbox-*.yaml")}
    if ns not in known:
        raise Refused(f"`{ns}` is not a namespace this form can give access to (demo, sandbox, sre-agent or a sandbox)")
    out = ROOT / "gitops/platform/base" / f"access-{ns}-{re.sub('[^a-z0-9-]', '-', user)}.yaml"
    if out.exists():
        raise Refused(f"{user} already has access to {ns}: {out.relative_to(ROOT)}")
    out.write_text(f"""# sre-platform/expires: {expiry(days=days)}
# Read-only access requested from the developer portal (scripts/selfservice.py access): the built-in `view` role in one namespace.
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: view-{re.sub('[^a-z0-9-]', '-', user)}
  namespace: {ns}
subjects:
  - {{kind: User, name: {user}, apiGroup: rbac.authorization.k8s.io}}
roleRef: {{kind: ClusterRole, name: view, apiGroup: rbac.authorization.k8s.io}}
""")
    return dict(files=[out], title=f"read access to {ns} for {user}",
                body=f"Gives the Kubernetes user `{user}` the `view` role in `{ns}` of the platform cluster for {days} day(s) (it cannot read Secrets). "
                     "This is the authorization only: the user still needs a credential for the cluster (a client certificate or an OIDC login).")


def act_drill(p):
    scenario, minutes = need(p, "scenario", "|".join(SCENARIOS)), need(p, "minutes", kind=int)
    if not 10 <= minutes <= 240:
        raise Refused("a drill runs 10 to 240 minutes")
    folder = ROOT / "gitops/fault-drills/active"
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{scenario}.yaml"
    if out.exists():
        raise Refused(f"the drill {scenario} is already running ({out.relative_to(ROOT)})")
    src = (ROOT / "scenarios" / f"{scenario}.yaml").read_text()
    src = re.sub(r"\A(#.*\n)+", "", src)
    out.write_text(f"# sre-platform/expires: {expiry(hours=minutes / 60)}\n# A failure drill requested from the developer portal (scripts/selfservice.py drill). It breaks a workload in the `scenarios` namespace on purpose so the alerts\n# and the SRE agent can be seen working. It is removed when the time above passes.\n{src}")
    for d in split_docs(out.read_text())[::2]:
        load(d)
    return dict(files=[out], title=f"failure drill: {scenario} for {minutes} minutes",
                body=f"Deploys `scenarios/{scenario}.yaml` into the `scenarios` namespace of the platform cluster for {minutes} minutes.\n\n"
                     "Expect a Grafana alert within a few minutes and an RCA report at https://reports.localhost:9443 after that. The drill is removed automatically.")


def act_expire(p):
    now = dt.datetime.now(dt.timezone.utc)
    gone = []
    for pattern in ("gitops/platform/base/sandbox-*.yaml", "gitops/platform/base/access-*.yaml", "gitops/fault-drills/active/*.yaml"):
        for f in sorted(ROOT.glob(pattern)):
            m = EXPIRES.search(f.read_text())
            if m and dt.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc) <= now:
                f.unlink()
                gone.append(f)
    if not gone:
        raise Refused("nothing has expired")
    names = ", ".join(f.name for f in gone)
    return dict(files=gone, title=f"remove expired self-service resources ({len(gone)})", body=f"Time is up for: {names}.", deleted=True)


ACTIONS = {k[4:]: v for k, v in globals().items() if k.startswith("act_")}


def main(argv):
    if len(argv) < 2 or argv[1] not in ACTIONS:
        sys.exit(__doc__)
    try:
        params = json.loads(argv[2]) if len(argv) > 2 and argv[2].strip() else {}
        if not isinstance(params, dict):
            raise Refused("the parameters must be a JSON object")
        result = ACTIONS[argv[1]](params)
    except Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        sys.exit(2)
    result["files"] = [Path(f).relative_to(ROOT).as_posix() for f in result["files"]]
    result["branch"] = f"selfservice/{argv[1]}-{dt.datetime.now(dt.timezone.utc):%Y%m%d-%H%M%S}"
    print(json.dumps(result))


if __name__ == "__main__":
    main(sys.argv)
