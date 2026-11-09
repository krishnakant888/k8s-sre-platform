"""Insights added to every report, by rules, from what the investigation already collected plus the earlier reports on the volume:

  blast radius, the change that may have caused it, how sure the analysis is, competing causes, related alerts, recurrence,
  the dependency chain, how long and which way it is going, and how to confirm the fix.

They are stored in report.insights (the viewer shows them) and as a section of rca.md. Nothing here calls the model or changes the diagnosis."""
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from agent import config

# what stops working with a component (extend for your own services)
DEPENDENTS = {
    "traefik": ["every URL behind the gateway of that cluster (the apps, Grafana, Argo CD, the report viewer)"],
    "inventory": ["orders-api (its inventory lookups)", "checkout in the frontend"],
    "orders-api": ["the frontend (orders and checkout)", "the orders URL through the gateway"],
    "frontend": ["users of the web shop"],
    "grafana": ["dashboards and alert rules evaluation", "the portal's Grafana pages"],
    "loki": ["log search and log based evidence in reports"],
    "mimir": ["metrics, alert rules and every dashboard"],
    "tempo": ["trace search and trace based evidence in reports"],
    "argocd-server": ["the Argo CD UI and API", "the portal's deployment views"],
    "argocd-application-controller": ["every GitOps sync and health check"],
    "coredns": ["name resolution for every workload of the cluster"],
}

# competing causes per root cause category: (cause, regex over the events and top log lines that supports it, how to tell it apart)
ALTERNATIVES = {
    "image_pull_failure": [("wrong tag or image name", r"not found|manifest unknown|invalid reference", "the event says the tag or manifest does not exist"),
                           ("registry unreachable or rate limited", r"no such host|timeout|i/o|429|too many requests|connection refused", "the event shows a network error or a rate limit, not a missing tag"),
                           ("missing pull credentials", r"unauthorized|denied|authentication required|403", "the event says unauthorized or denied")],
    "pod_crash": [("application error at start", r"error|exception|traceback|fatal|panic", "the log has an exception or fatal line before the exit"),
                  ("out of memory", r"oomkilled|out of memory|exit code 137|137", "the last exit is OOMKilled or 137"),
                  ("missing setting or dependency", r"not set|missing|refused|unable to connect|no such file", "the log names a missing variable, file or refused connection")],
    "dependency_unreachable": [("dependency down", r"refused|no route|unavailable|503", "the connection is refused or the service has no ready pod"),
                               ("network policy blocking", r"timed? ?out|i/o timeout|deadline", "calls time out instead of being refused"),
                               ("name resolution", r"no such host|dns|lookup|resolve", "the host name does not resolve")],
    "dependency_errors": [("dependency returns errors", r"5\d\d|error|failed", "the dependency answers, with 5xx or an error body"),
                          ("dependency too slow", r"timeout|deadline|slow", "calls end in timeouts rather than errors"),
                          ("bad deploy of the dependency", r"deploy|rollout|version", "a rollout of the dependency started just before")],
    "scheduling_failure": [("not enough CPU or memory on any node", r"insufficient|too many pods", "the event says Insufficient cpu or memory"),
                           ("node selector, taint or affinity", r"node selector|taint|affinity|didn't match", "the event says no node matched a selector or taint"),
                           ("volume cannot be bound", r"persistentvolumeclaim|unbound|storageclass|volume", "the event mentions a claim or storage class")],
    "resource_exhaustion": [("limit too low for the load", r"limit|oomkilled|throttl", "usage is at the limit while the load is normal"),
                            ("memory leak or runaway loop", r"leak|growing|loop", "usage grows steadily without a load increase"),
                            ("node under pressure", r"pressure|evict", "pressure or eviction events on the node")],
    "node_failure": [("node or kubelet down", r"notready|kubelet|node.*not ready", "the node reports NotReady"),
                     ("network partition", r"unreachable|partition|timeout", "the node runs but cannot reach other clusters"),
                     ("resource pressure on the node", r"pressure|evict", "pressure conditions before the node went down")],
    "probe_failure": [("probe too strict", r"timeout|deadline|slow", "the probe times out while the app answers eventually"),
                      ("app not healthy", r"500|503|refused|error", "the probe gets errors from the app"),
                      ("wrong probe path or port", r"404|not found|connection refused", "the probe path or port does not exist")],
    "config_error": [("missing secret", r"secret", "the event names a missing secret"),
                     ("missing config map", r"configmap", "the event names a missing config map"),
                     ("read only or permission", r"read-only|permission denied|eacces", "the log shows a write or permission error")],
    "gitops_sync_failure": [("path or repository problem", r"path|not found|repository|rendering|kustomize", "the sync error names a path or the render step"),
                            ("policy refused the manifest", r"forbidden|violat|admission|policy|privileged|kyverno", "an admission webhook or policy rejected it"),
                            ("cluster or permission problem", r"unauthorized|cannot|connection|timeout", "the controller could not reach the cluster")],
}


def _ts(text: str) -> float | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def blast_radius(report, sig: dict) -> list[str]:
    names = {sig.get("workload", ""), report.service, sig.get("backend", "") or ""}
    out = []
    for name in sorted(n for n in names if n):
        for key, deps in DEPENDENTS.items():
            if name.startswith(key):
                out += [f"{d} (because {name} is affected)" for d in deps]
    if report.root_cause.category == "scaled_to_zero" and "traefik" in (sig.get("workload", "") + sig.get("namespace", "")):
        out += [f"{d} (the gateway has no pod)" for d in DEPENDENTS["traefik"]]
    if sig.get("user_pods_on_node"):
        out.append("pods of users on the affected node: " + ", ".join(sig["user_pods_on_node"][:5]))
    elif sig.get("pods_on_node"):
        out.append(f"{len(sig['pods_on_node'])} pods on the affected node")
    if report.service in config.USER_FACING_SERVICES:
        out.append(f"users of {report.service} directly")
    return list(dict.fromkeys(out))


def change(report, sig: dict) -> dict:
    rd = sig.get("recent_deploy")
    if not rd or not rd.get("deployed_at"):
        return {"likely": False, "note": "no deploy found in the 2 hours before the alert"}
    started, at = _ts(report.started_at), _ts(rd["deployed_at"])
    gap = round((started - at) / 60) if started and at else None
    # likely=True covers 0–120 min (matches 2 h lookback); high_confidence=True for ≤60 min
    likely = gap is not None and 0 <= gap <= 120
    high_confidence = gap is not None and 0 <= gap <= 60
    note = (f"deploy {rd.get('revision', '?')} landed {gap} min before the alert: strong candidate — roll back or inspect first" if high_confidence
            else f"deploy {rd.get('revision', '?')} landed {gap} min before the alert: possible contributing cause, worth checking" if likely
            else f"deploy {rd.get('revision', '?')} was {abs(gap) if gap is not None else '?'} min {'after' if gap is not None and gap < 0 else 'before'} the alert: probably not the cause")
    return {"likely": likely, "high_confidence": high_confidence, "revision": rd.get("revision", ""), "deployed_at": rd["deployed_at"], "minutes_before_alert": gap, "files": sig.get("source_files", [])[:5], "note": note}


def confidence(report, ctx: dict) -> dict:
    bad = ("lookup failed", "could be read", "no error lines", "not found")
    good = sorted({e["source"] for e in ctx.get("evidence", []) if e.get("finding") and not any(b in e["finding"] for b in bad)})
    errors = (ctx.get("signals") or {}).get("tool_errors", [])
    level = "high" if len(good) >= 3 and not errors else "medium" if len(good) >= 2 else "low"
    asks = {"loki": "logs of the affected pods", "mimir": "the metrics of the workload", "kubernetes": "pod status and events", "tempo": "a trace of a failing request", "argocd": "the Argo CD history of the application"}
    return {"level": level, "agreeing_sources": good, "problems": errors[:3], "check_next": [f"no usable evidence from {s}: look at {w}" for s, w in asks.items() if s not in good][:3]}


def alternatives(report, ctx: dict) -> list[dict]:
    sig = ctx.get("signals") or {}
    text = " ".join([e.get("message", "") + " " + e.get("reason", "") for e in sig.get("events", [])] + [m for m, _ in sig.get("top_log_messages", [])] + [e.get("finding", "") for e in ctx.get("evidence", [])]).lower()
    rows = []
    for cause, rx, how in ALTERNATIVES.get(report.root_cause.category, []):
        seen = bool(re.search(rx, text))
        rows.append({"cause": cause, "supported": seen, "how_to_tell": how, "evidence": "seen in the events, logs or evidence" if seen else "not seen in this investigation"})
    rows.sort(key=lambda r: not r["supported"])
    return rows


def _earlier(reports_dir: str | None, now: float, days: float):
    folder = Path(reports_dir or config.REPORTS_DIR)
    if not folder.is_dir():
        return
    for f in sorted(folder.glob("*/report.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:300]:
        try:
            r = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        t = _ts(r.get("started_at", ""))
        if t and now - t <= days * 86400:
            yield r, t


def related_and_recurrence(report, cluster: str, now: float, reports_dir: str | None = None) -> tuple[list[dict], dict]:
    me = _ts(report.started_at) or now
    related, same = [], []
    for r, t in _earlier(reports_dir, now, 7):
        if r.get("id") == report.id:
            continue
        if r.get("alert") == report.alert and r.get("service") == report.service and t < me:
            same.append(r)
        if abs(t - me) <= 900 and (r.get("alert_details", {}).get("cluster") or cluster) == cluster and r.get("alert") != report.alert:
            related.append({"id": r["id"], "alert": r["alert"], "service": r.get("service", ""), "priority": r.get("priority", {}).get("level", ""), "minutes_apart": round((t - me) / 60)})
    rec = {"times_before": len(same), "window_days": 7, "ids": [r["id"] for r in same[:3]],
           "note": (f"the {len(same) + 1}th time in 7 days: a permanent fix is overdue, not another restart" if len(same) >= 2 else "second time in 7 days" if same else "first time in 7 days")}
    return related[:5], rec


def chain(report, sig: dict) -> list[dict]:
    cat = report.root_cause.category
    hops = []
    if sig.get("family") in ("ingress", "gateway") or cat == "scaled_to_zero":
        hops.append(("gateway of " + sig.get("cluster", "the cluster"), cat in ("scaled_to_zero",) and "traefik" in sig.get("workload", "")))
    if sig.get("backend"):
        hops.append((f"service {sig['backend']}", cat == "ingress_backend_failure"))
    wl = sig.get("workload") or report.service
    pods = sig.get("pods", [])
    ready = sum(1 for p in pods if p.get("ready"))
    flagged = any(p for _, p in hops)
    hops.append((f"{wl}" + (f" ({ready} of {len(pods)} pods ready)" if pods else ""), not flagged and cat not in ("node_failure", "telemetry_gap", "control_plane_degraded", "dns_failure")))
    if sig.get("nodes"):
        hops.append(("node " + ", ".join(sig["nodes"][:2]), cat == "node_failure"))
    hops.append(("cluster " + sig.get("cluster", ""), cat in ("control_plane_degraded", "telemetry_gap", "dns_failure")))
    out = [{"hop": h, "problem": p} for h, p in hops]
    return out


def trend(report, sig: dict, now: float) -> dict:
    started = _ts(report.started_at) or now
    lines = []
    peak, last = sig.get("error_ratio_peak"), sig.get("error_ratio_last")
    if peak and last is not None:
        lines.append("errors are easing" if last < peak * 0.5 else "errors are still at their peak" if last >= peak * 0.9 else "errors are falling slowly")
    if (sig.get("mem_growth") or 0) >= 1.2:
        lines.append(f"memory grew {sig['mem_growth']:.1f}x over the window; at that pace the limit is reached again")
    g, cap, used = sig.get("growth_per_hour"), sig.get("cap_bytes"), sig.get("used_bytes")
    if g and g > 0 and cap and used is not None and cap > used:
        lines.append(f"the volume fills in about {(cap - used) / g:.1f} hours at the current growth")
    if sig.get("days_left") is not None and sig["days_left"] < 14:
        lines.append(f"the certificate expires in {sig['days_left']:.1f} days")
    if sig.get("throttle", 0) >= 0.25:
        lines.append(f"CPU is throttled {sig['throttle']:.0%} of the time: capacity, not a bug, limits it")
    return {"unhealthy_minutes": max(0, round((now - started) / 60)), "still_ongoing": report.priority.ongoing, "lines": lines}


def build(report, ctx: dict, now: float | None = None, reports_dir: str | None = None) -> dict:
    now = now or time.time()
    sig = ctx.get("signals") or {}
    cluster = sig.get("cluster") or (ctx.get("labels") or {}).get("cluster", "")
    related, recurrence = related_and_recurrence(report, cluster, now, reports_dir)
    return {
        "blast_radius": blast_radius(report, sig),
        "change": change(report, sig),
        "confidence": confidence(report, ctx),
        "alternatives": alternatives(report, ctx),
        "related": related,
        "recurrence": recurrence,
        "chain": chain(report, sig),
        "trend": trend(report, sig, now),
        "verification": {"resolves_when": f"the alert {report.alert} stops firing (the report viewer shows it live)", "commands": report.how_to_fix.verify},
    }


def section(d: dict) -> str:
    if not d:
        return ""
    L = ["", "", "## Insights"]
    if d["blast_radius"]:
        L += ["### What else is affected"] + [f"- {x}" for x in d["blast_radius"]]
    L += ["### Was it a change?", f"- {d['change']['note']}"]
    c = d["confidence"]
    L += ["### How sure is this", f"- **{c['level']}**, evidence from: {', '.join(c['agreeing_sources']) or 'nothing usable'}"] + [f"- {x}" for x in c["check_next"]]
    if d["alternatives"]:
        L += ["### Other possible causes"] + [f"- {'**seen**' if a['supported'] else 'not seen'}: {a['cause']} ({a['how_to_tell']})" for a in d["alternatives"]]
    L += ["### Dependency chain", "- " + " → ".join(h["hop"] + (" ← problem here" if h["problem"] else "") for h in d["chain"])]
    t = d["trend"]
    L += ["### How long and which way", f"- unhealthy for about {t['unhealthy_minutes']} min" + (", still ongoing" if t["still_ongoing"] else ", no longer ongoing")] + [f"- {x}" for x in t["lines"]]
    L += ["### Seen before", f"- {d['recurrence']['note']}"]
    if d["related"]:
        L += ["### Related alerts in the same window"] + [f"- {r['alert']} on {r['service']} ({r['priority']}), {abs(r['minutes_apart'])} min {'after' if r['minutes_apart'] > 0 else 'before'}: possibly the same incident, `{r['id']}`" for r in d["related"]]
    L += ["### Confirming the fix", f"- {d['verification']['resolves_when']}"]
    return "\n".join(L) + "\n"
