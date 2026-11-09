"""Self-learning jobs that improve report quality over time.

Four weekly passes over the reports volume:
  1. unknown_rediagnosis    — re-query Mimir at incident time for undiagnosed reports
  2. gap_tracker            — open GitHub issues for repeated observability gaps
  3. confidence_calibration — measure per-family diagnosis accuracy, write weights
  4. threshold_tuning       — derive per-service scale-down thresholds from incident history

scale_down.py reads .thresholds.json; generic.py reads .calibration.json.
"""
import json
import logging
import math
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

from agent import config
from agent.remediate import _headers

log = logging.getLogger("sre-agent")

REPORTS_DIR = Path(config.REPORTS_DIR)
THRESHOLDS_FILE = REPORTS_DIR / ".thresholds.json"
CALIBRATION_FILE = REPORTS_DIR / ".calibration.json"
GAP_ISSUES_FILE = REPORTS_DIR / ".gap-issues.json"

ISSUES_REPO = os.getenv("LEARNING_ISSUES_REPO", getattr(config, "GITOPS_REPO", ""))
MIN_GAP_COUNT = int(os.getenv("GAP_ISSUE_MIN_COUNT", "3"))
REDIAGNOSIS_MIN_AGE_HOURS = int(os.getenv("REDIAGNOSIS_MIN_AGE_HOURS", "2"))

_DEFAULT_CPU_MAX = float(os.getenv("CPU_P95_MAX_DEFAULT", "0.50"))
_DEFAULT_MEM_MAX = float(os.getenv("MEMORY_P99_MAX_DEFAULT", "0.60"))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _load_all_reports() -> list[dict]:
    reports = []
    if not REPORTS_DIR.exists():
        return reports
    for d in sorted(REPORTS_DIR.iterdir()):
        if not d.is_dir() or not d.name.startswith("rca-"):
            continue
        rp = d / "report.json"
        if rp.exists():
            try:
                reports.append(json.loads(rp.read_text(encoding="utf-8")))
            except Exception:
                pass
    return reports


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def _mimir_at(query: str, t: float) -> float | None:
    """Instant Mimir query anchored to Unix timestamp t."""
    try:
        r = httpx.get(
            f"{config.MIMIR_URL}/api/v1/query",
            params={"query": query, "time": str(int(t))},
            timeout=12,
        )
        r.raise_for_status()
        vals = r.json().get("data", {}).get("result", [])
        if vals:
            v = float(vals[0]["value"][1])
            return v if math.isfinite(v) else None
    except Exception as exc:
        log.debug("learning: mimir_at failed: %s", exc)
    return None


def _gh_get(path: str, **kw):
    r = httpx.get(f"https://api.github.com/{path}", headers=_headers(), timeout=15, **kw)
    r.raise_for_status()
    return r.json()


def _gh_post(path: str, body: dict):
    r = httpx.post(f"https://api.github.com/{path}", headers=_headers(), json=body, timeout=15)
    r.raise_for_status()
    return r.json()


def _parse_ts(iso: str) -> float | None:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def _mimir_range(query: str, start: float, end: float, step: int = 86400) -> list[tuple[int, float]]:
    """Return (unix_day_ts, value) pairs from a Mimir range query at daily resolution."""
    try:
        r = httpx.get(
            f"{config.MIMIR_URL}/api/v1/query_range",
            params={"query": query, "start": str(int(start)), "end": str(int(end)), "step": str(step)},
            timeout=25,
        )
        r.raise_for_status()
        results = r.json().get("data", {}).get("result", [])
        if not results:
            return []
        return [(int(ts), round(float(v), 4)) for ts, v in results[0].get("values", []) if v not in ("NaN", "+Inf", "-Inf")]
    except Exception as exc:
        log.debug("learning: mimir_range failed: %s", exc)
    return []


# ---------------------------------------------------------------------------
# 1. Unknown rediagnosis
# ---------------------------------------------------------------------------

def _rediagnose_signals(labels: dict, t: float) -> str | None:
    """Return a root_cause category from historical Mimir signals at time t, or None."""
    c = labels.get("cluster", "platform")
    ns = labels.get("namespace", "")
    pod = labels.get("pod", "")
    dep = labels.get("deployment", re.sub(r"-[a-z0-9]+-[a-z0-9]+$", "", pod) if pod else "")
    if not ns or not dep:
        return None
    sel = f'cluster="{c}",namespace="{ns}",pod=~"{dep}-[a-z0-9]+-[a-z0-9]+"'

    oom = _mimir_at(
        f'max(kube_pod_container_status_last_terminated_reason{{{sel},reason="OOMKilled"}})', t)
    if oom and oom > 0:
        return "resource_exhaustion"

    restarts = _mimir_at(
        f'max(increase(kube_pod_container_status_restarts_total{{{sel}}}[10m]))', t)
    if restarts and restarts > 2:
        return "pod_crash"

    throttle_num = _mimir_at(
        f'max(rate(container_cpu_cfs_throttled_periods_total{{{sel}}}[5m]))', t)
    throttle_den = _mimir_at(
        f'max(rate(container_cpu_cfs_periods_total{{{sel}}}[5m]))', t)
    if (throttle_num and throttle_den and throttle_den > 0
            and throttle_num / throttle_den > 0.5):
        return "resource_exhaustion"

    argo = _mimir_at(
        f'max(argocd_app_info{{dest_namespace="{ns}",health_status!="Healthy"}})', t)
    if argo and argo > 0:
        return "gitops_sync_failure"

    err = _mimir_at(
        f'sum(rate(traefik_service_requests_total{{service=~"{dep}.*",code=~"5.."}}[5m]))', t)
    if err and err > 0.1:
        return "dependency_errors"

    return None


def run_unknown_rediagnosis() -> list[dict]:
    """Re-diagnose reports with unknown root cause using historical Mimir data.
    Returns list of {id, service, old_cause, new_cause} for each updated report."""
    reports = _load_all_reports()
    now = datetime.now(timezone.utc).timestamp()
    min_age = REDIAGNOSIS_MIN_AGE_HOURS * 3600
    updated: list[dict] = []

    for r in reports:
        if r.get("report_type", "incident") != "incident":
            continue
        rc = r.get("root_cause") or {}
        if rc.get("category") not in ("unknown", None, ""):
            continue
        if r.get("learning_rediagnosed_at"):
            continue

        t = _parse_ts(r.get("started_at") or r.get("generated_at", ""))
        if t is None or now - t < min_age:
            continue

        labels = (r.get("alert_details") or {}).get("labels", {})
        new_cat = _rediagnose_signals(labels, t)

        rp = REPORTS_DIR / r["id"] / "report.json"
        if not rp.exists():
            continue

        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        r["learning_rediagnosed_at"] = stamp
        if new_cat:
            old_cat = rc.get("category", "unknown")
            r["root_cause"] = {
                **rc,
                "category": new_cat,
                "confidence": "low",
                "description": (
                    f"Re-diagnosed from historical Mimir signals at incident time "
                    f"(original: {old_cat})."
                ),
            }
            updated.append({
                "id": r["id"],
                "service": r.get("service", ""),
                "namespace": r.get("namespace", ""),
                "old_cause": old_cat,
                "new_cause": new_cat,
                "generated_at": r.get("generated_at", ""),
            })
            log.info("learning: rediagnosed %s → %s", r["id"], new_cat)
        else:
            log.debug("learning: rediagnosis inconclusive for %s", r["id"])

        _atomic_write(rp, r)

    log.info("learning: rediagnosis complete; updated %d of eligible reports", len(updated))
    return updated


# ---------------------------------------------------------------------------
# 2. Observability gap tracker
# ---------------------------------------------------------------------------

_GAP_PATTERNS = [
    (r"no `?trace_id`?", "missing_trace_id",
     "Add trace_id to structured log context so a failing request links to its trace."),
    (r"no Argo.?CD application", "missing_argocd_app",
     "Label the deployment so ArgoCD app name is discoverable: "
     "app.kubernetes.io/part-of or argocd-app annotation."),
    (r"alert was not explained|recording rule or structured log field", "unexplained_alert",
     "Add a recording rule or structured log field that captures the signal "
     "that crossed the alert threshold (the value, pod name, error message)."),
    (r"log lookup failed|loki.*timeout|ConnectTimeout.*loki|loki.*connect", "loki_timeout",
     "Loki query times out for this service. Check Loki retention policy, "
     "label cardinality, and per-tenant query limits."),
    (r"no metrics|metrics.*unavailable|metric.*not found", "missing_metrics",
     "Service has no Prometheus/Mimir metrics. Add instrumentation or verify scrape config."),
]


def _classify_gap(gap: str) -> tuple[str, str] | None:
    """Return (gap_type, remediation_hint) or None if unclassified."""
    for pattern, label, hint in _GAP_PATTERNS:
        if re.search(pattern, gap, re.I):
            return label, hint
    return None


def run_gap_tracker() -> list[dict]:
    """Open GitHub issues for observability gaps that repeat ≥ MIN_GAP_COUNT times.
    Returns list of all tracked gaps: {service, gap_type, count, issue_url, example_reports, is_new}."""
    if not ISSUES_REPO or not getattr(config, "GITHUB_TOKEN", ""):
        log.info("learning: gap_tracker skipped (no ISSUES_REPO or GITHUB_TOKEN)")
        return []

    filed: dict[str, str] = {}  # "{service}:{gap_type}" -> issue_url
    if GAP_ISSUES_FILE.exists():
        try:
            filed = json.loads(GAP_ISSUES_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    reports = _load_all_reports()
    by_svc_gap: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"count": 0, "hint": "", "ids": []})
    for r in reports:
        svc = r.get("service", "")
        for gap in r.get("observability_gaps", []):
            classified = _classify_gap(gap)
            if not classified:
                continue
            gap_type, hint = classified
            key = (svc, gap_type)
            by_svc_gap[key]["count"] += 1
            by_svc_gap[key]["hint"] = hint
            by_svc_gap[key]["ids"].append(r["id"])

    result: list[dict] = []
    opened = 0
    for (svc, gap_type), data in by_svc_gap.items():
        if data["count"] < MIN_GAP_COUNT:
            continue
        issue_key = f"{svc}:{gap_type}"
        is_new = issue_key not in filed

        if is_new:
            title = f"[sre-gap] {svc}: {gap_type.replace('_', ' ')} ({data['count']} reports)"
            body = (
                f"## Observability gap: {gap_type.replace('_', ' ')}\n\n"
                f"**Service:** `{svc}`  \n"
                f"**Occurrences:** {data['count']} SRE reports in the reports volume  \n\n"
                f"### What to fix\n{data['hint']}\n\n"
                f"### Example reports\n"
                + "\n".join(f"- `{rid}`" for rid in data["ids"][:5])
                + "\n\n"
                "_Opened automatically by the SRE agent self-learning pass. "
                "Close once the gap is resolved and the agent stops reporting it._"
            )
            try:
                issue = _gh_post(f"repos/{ISSUES_REPO}/issues", {
                    "title": title,
                    "body": body,
                    "labels": ["sre-gap", "observability"],
                })
                filed[issue_key] = issue["html_url"]
                log.info("learning: opened gap issue %s/%s → %s",
                         svc, gap_type, issue["html_url"])
                opened += 1
            except Exception as exc:
                log.warning("learning: could not open gap issue for %s/%s: %s",
                            svc, gap_type, exc)

        result.append({
            "service": svc,
            "gap_type": gap_type,
            "count": data["count"],
            "hint": data["hint"],
            "issue_url": filed.get(issue_key, ""),
            "example_reports": data["ids"][:5],
            "is_new": is_new and issue_key in filed,
        })

    _atomic_write(GAP_ISSUES_FILE, filed)
    log.info("learning: gap_tracker complete; opened %d new issues", opened)
    return result


# ---------------------------------------------------------------------------
# 3. Confidence calibration
# ---------------------------------------------------------------------------

def _alert_cleared_at_t(alert: str, t: float) -> bool | None:
    """Return True if the alert had cleared by time t, False if still firing, None if unknown.

    The ALERTS synthetic metric carries the alert rule's own labels (alertname, namespace, pod…)
    — not necessarily a 'service' label — so we filter by alertname only.  A result of None means
    Mimir had no data at that time; the caller treats it as "unknown" (no penalty applied).
    """
    if not alert:
        return None
    still_firing = _mimir_at(f'ALERTS{{alertname="{alert}"}}', t)
    if still_firing is None:
        return None
    return still_firing == 0


def run_confidence_calibration() -> dict:
    """Measure per-family diagnosis accuracy and write calibration weights.

    For each resolved incident the agent measures two things:
    1. Alert cleared within 1h of report generation (Mimir ALERTS metric at T+1h).
       Cleared = the fix worked = diagnosis was correct.
    2. Re-fire within 24h:
       - Same root_cause → correctly identified a recurring issue (not a miss).
       - Different root_cause → first diagnosis was wrong.

    confidence_weight in [0.3, 1.0]; generic.py reads it and downgrades:
      weight < 0.5 → "high" becomes "medium"
      weight < 0.4 → "medium" becomes "low"
    """
    reports = _load_all_reports()
    incidents = sorted(
        [r for r in reports if r.get("report_type", "incident") == "incident"],
        key=lambda r: r.get("started_at") or r.get("generated_at", ""),
    )

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in incidents:
        groups[(r.get("alert", ""), r.get("service", ""))].append(r)

    family_stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "cleared": 0, "wrong_rediagnosis": 0})

    for rs in groups.values():
        for i, r in enumerate(rs):
            cat = (r.get("root_cause") or {}).get("category", "unknown")
            if cat in ("unknown", "investigation_pipeline_failed", "", None):
                continue
            gen_t = _parse_ts(r.get("generated_at") or r.get("started_at", ""))
            if gen_t is None:
                continue

            family_stats[cat]["total"] += 1

            # 1. Did the alert clear within 1h?
            cleared = _alert_cleared_at_t(r.get("alert", ""), gen_t + 3600)
            if cleared is True:
                family_stats[cat]["cleared"] += 1
            elif cleared is False and i + 1 < len(rs):
                # 2. Re-fired; check if same or different root cause
                next_cat = (rs[i + 1].get("root_cause") or {}).get("category", "unknown")
                t1 = _parse_ts(rs[i + 1].get("started_at", ""))
                if t1 and t1 - gen_t < 86400 and next_cat not in ("unknown", "", None):
                    if next_cat != cat:
                        # different cause identified on re-fire → first was wrong
                        family_stats[cat]["wrong_rediagnosis"] += 1
                    # same cause → recurring issue, correctly identified, not penalised

    calibration: dict = {}
    for cat, stats in family_stats.items():
        total = stats["total"]
        cleared = stats["cleared"]
        wrong = stats["wrong_rediagnosis"]
        # accuracy: fraction of incidents NOT confirmed wrong
        # (only wrong_rediagnosis is penalised; unknowns and cleared both get benefit of the doubt)
        accuracy = round(1.0 - (wrong / total), 3) if total else 1.0
        calibration[cat] = {
            "total": total,
            "cleared_within_1h": cleared,
            "wrong_rediagnosis": wrong,
            "accuracy": accuracy,
            "confidence_weight": round(min(1.0, max(0.3, accuracy)), 2),
        }

    calibration["_generated_at"] = (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
    _atomic_write(CALIBRATION_FILE, calibration)
    total_incidents = sum(
        v["total"] for v in calibration.values() if isinstance(v, dict) and "total" in v)
    log.info("learning: calibration written; %d families, %d incidents evaluated",
             len(calibration) - 1, total_incidents)
    return calibration


# ---------------------------------------------------------------------------
# 4. Threshold tuning
# ---------------------------------------------------------------------------

def run_threshold_tuning() -> dict:
    """Derive per-service scale-down thresholds from incident + capacity history.

    Rules:
      ≥2 resource_exhaustion incidents → tighten (harder to trigger scale-down)
      1 resource_exhaustion incident    → slightly tighten
      0 incidents + ≥3 clean reviews   → slightly loosen
    Writes .thresholds.json; scale_down.py reads it at runtime.
    """
    reports = _load_all_reports()

    oom_counts: Counter = Counter()
    clean_caps: Counter = Counter()
    service_locations: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    for r in reports:
        svc = r.get("service", "")
        if not svc:
            continue
        rt = r.get("report_type", "incident")
        alert_details = r.get("alert_details") or {}
        labels = alert_details.get("labels") or {}
        capacity = r.get("capacity") or {}
        cluster = capacity.get("cluster") or alert_details.get("cluster") or labels.get("cluster") or ""
        namespace = capacity.get("namespace") or alert_details.get("namespace") or labels.get("namespace") or ""
        pod = labels.get("pod", "")
        pod_workload = re.sub(r"-[a-f0-9]{8,10}-[a-z0-9]+$|-\d+$", "", pod) if pod else svc
        workload = labels.get("deployment") or labels.get("statefulset") or pod_workload
        service_locations[svc].add((str(cluster), str(namespace), str(workload)))
        if rt == "incident" and (r.get("root_cause") or {}).get("category") == "resource_exhaustion":
            oom_counts[svc] += 1
        if rt == "capacity":
            cap = r.get("capacity", {}) or {}
            if (cap.get("decision") in ("skipped", "recommendation")
                    and cap.get("review_type") != "weekly_floor"):
                clean_caps[svc] += 1

    # collect per-service incident and capacity-review dates for telemetry annotation
    incident_dates: dict[str, list[str]] = defaultdict(list)
    cap_review_dates: dict[str, list[dict]] = defaultdict(list)
    for r in reports:
        svc = r.get("service", "")
        if not svc:
            continue
        rt = r.get("report_type", "incident")
        date_str = (r.get("started_at") or r.get("generated_at", ""))[:10]
        if rt == "incident" and (r.get("root_cause") or {}).get("category") == "resource_exhaustion":
            incident_dates[svc].append(date_str)
        if rt == "capacity" and r.get("capacity", {}).get("review_type") != "weekly_floor":
            cap = r.get("capacity", {}) or {}
            cap_review_dates[svc].append({"date": date_str, "decision": cap.get("decision", "")})

    now_ts = datetime.now(timezone.utc).timestamp()
    thirty_ago = now_ts - 30 * 86400

    thresholds: dict = {}
    for svc in set(oom_counts) | set(clean_caps):
        ooms = oom_counts[svc]
        cleans = clean_caps[svc]
        cpu = _DEFAULT_CPU_MAX
        mem = _DEFAULT_MEM_MAX
        reason = "default thresholds (insufficient history)"

        if ooms >= 2:
            step = min(ooms, 3) * 0.10
            cpu = round(max(0.25, _DEFAULT_CPU_MAX - step), 2)
            mem = round(max(0.35, _DEFAULT_MEM_MAX - step), 2)
            reason = f"conservative: {ooms} resource_exhaustion incidents"
        elif ooms == 1:
            cpu = round(max(0.30, _DEFAULT_CPU_MAX - 0.10), 2)
            mem = round(max(0.40, _DEFAULT_MEM_MAX - 0.10), 2)
            reason = "slightly conservative: 1 resource_exhaustion incident"
        elif cleans >= 3:
            cpu = round(min(0.65, _DEFAULT_CPU_MAX + 0.05), 2)
            mem = round(min(0.75, _DEFAULT_MEM_MAX + 0.05), 2)
            reason = f"slightly relaxed: {cleans} clean capacity reviews, no resource incidents"

        # Query each observed workload location. Reports carry the real namespace and
        # deployment; assuming `default` and container==service silently drops samples.
        legacy_parts = svc.split(":", 1)
        svc_name = legacy_parts[0]
        locations = service_locations.get(svc) or {("", legacy_parts[1] if len(legacy_parts) > 1 else "", svc_name)}
        cpu_by_time: dict[int, float] = {}
        mem_by_time: dict[int, float] = {}
        for cluster, ns, workload in locations:
            # Prometheus label values are quoted; workload names are escaped as RE2 regexes.
            matcher_parts = []
            if cluster:
                matcher_parts.append(f"cluster={json.dumps(cluster)}")
            if ns:
                matcher_parts.append(f"namespace={json.dumps(ns)}")
            workload_pattern = re.escape(workload or svc_name)
            matcher_parts.extend((f'pod=~"^{workload_pattern}-.*"', 'container!=""', 'container!="POD"'))
            sel = ",".join(matcher_parts)
            cpu_samples = _mimir_range(
                f"max(quantile_over_time(0.95, rate(container_cpu_usage_seconds_total{{{sel}}}[30m])[1d:30m]))",
                thirty_ago, now_ts)
            mem_samples = _mimir_range(
                f"max(quantile_over_time(0.99, container_memory_working_set_bytes{{{sel}}}[1d]))",
                thirty_ago, now_ts)
            for ts, value in cpu_samples:
                cpu_by_time[ts] = max(cpu_by_time.get(ts, value), value)
            for ts, value in mem_samples:
                mem_by_time[ts] = max(mem_by_time.get(ts, value), value)
        cpu_series = sorted(cpu_by_time.items())
        mem_series = sorted(mem_by_time.items())

        thresholds[svc] = {
            "cpu_p95_max": cpu,
            "memory_p99_max": mem,
            "reason": reason,
            "oom_incidents": ooms,
            "clean_capacity_reviews": cleans,
            "telemetry": {
                "cpu_p95_cores": [{"t": t, "v": v} for t, v in cpu_series],
                "mem_p99_gib": [{"t": t, "v": round(v / 1073741824, 4)} for t, v in mem_series],
                "incident_dates": sorted(set(incident_dates.get(svc, []))),
                "cap_review_dates": cap_review_dates.get(svc, [])[-30:],
            },
        }

    thresholds["_generated_at"] = (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
    _atomic_write(THRESHOLDS_FILE, thresholds)
    log.info("learning: threshold_tuning written; %d services", len(thresholds) - 1)
    return thresholds


# ---------------------------------------------------------------------------
# 5. LLM review of past rule_based reports
# ---------------------------------------------------------------------------

_LLM_REVIEW_SYSTEM = """You are an SRE reviewing a previously written rule-based incident report.
You have the alert details, the evidence collected at incident time, and the current report fields.
Treat the evidence as ground truth — do not invent facts not in it.

Improve the report by:
1. what_happened — 3-5 plain sentences: what broke, when, how bad, exact numbers, cause chain.
2. root_cause_description — specific reasoning from evidence; not generic.
3. root_cause_confidence — low / medium / high; downgrade if evidence is thin.
4. how_to_fix_immediate — exact kubectl/helm commands, file paths, expected values.
5. how_to_fix_permanent — the specific config or code change that prevents recurrence.
6. how_to_fix_verify — a PromQL / LogQL query or kubectl command and the value to expect when fixed.
7. observability_gaps — precise gaps; exact metric name, log field, or alert expression to add.
8. rca_markdown — full blameless postmortem: summary, impact, timeline, root cause, action items.

If the rule-based hypothesis looks wrong, say so and explain from the evidence.
Distinguish symptoms (errors, latency) from causes (killed pod, slow dep, deploy, limit).
Respond ONLY with a JSON object with keys:
  what_happened, root_cause_description, root_cause_confidence,
  how_to_fix_immediate (list), how_to_fix_permanent (list), how_to_fix_verify (list),
  observability_gaps (list), rca_markdown."""

_LLM_REVIEW_BATCH = int(os.getenv("LLM_REVIEW_BATCH", "10"))


def run_llm_report_review() -> list[dict]:
    """Use Claude to improve rule_based incident reports using their stored evidence.

    Does NOT re-run live queries — evidence was stored at incident time and may predate
    Mimir/Loki retention. Instead, Claude reasons over the stored evidence to produce
    better narrative, more specific fix commands and tighter gap descriptions.

    Reports with mode=llm or llm_reviewed_at set are skipped.
    Processes at most LLM_REVIEW_BATCH (default 10) reports per run to bound API cost.
    Returns list of {id, service, alert, was_confidence, now_confidence}.
    """
    if not config.ANTHROPIC_API_KEY:
        log.info("learning: llm_review skipped (no ANTHROPIC_API_KEY)")
        return []

    try:
        import anthropic as _anthropic
    except ImportError:
        log.warning("learning: llm_review skipped (anthropic package not installed)")
        return []

    reports = _load_all_reports()
    eligible = [
        r for r in reports
        if r.get("report_type", "incident") == "incident"
        and r.get("mode") == "rule_based"
        and not r.get("llm_reviewed_at")
        and r.get("evidence")
    ]

    if not eligible:
        log.info("learning: llm_review: no eligible rule_based reports")
        return []

    log.info("learning: llm_review: %d eligible, processing up to %d", len(eligible), _LLM_REVIEW_BATCH)
    client = _anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    improved: list[dict] = []

    for r in eligible[:_LLM_REVIEW_BATCH]:
        rid = r.get("id", "?")
        rp = REPORTS_DIR / rid / "report.json"
        if not rp.exists():
            continue
        try:
            evidence_text = json.dumps(r.get("evidence", []), indent=2)[:6000]
            current = (
                f"Alert: {r.get('alert', '')} | Service: {r.get('service', '')} | "
                f"Severity: {r.get('severity', '')} | Started: {r.get('started_at', '')}\n\n"
                f"Current root_cause: {json.dumps(r.get('root_cause', {}))}\n"
                f"Current what_happened: {r.get('what_happened', '')}\n"
                f"Current how_to_fix: {json.dumps(r.get('how_to_fix', {}))}\n"
                f"Current observability_gaps: {json.dumps(r.get('observability_gaps', []))}\n\n"
                f"Evidence:\n{evidence_text}"
            )
            resp = client.messages.create(
                model=config.ANTHROPIC_MODEL,
                max_tokens=2048,
                system=_LLM_REVIEW_SYSTEM,
                messages=[{"role": "user", "content": current + "\n\nRespond with JSON only."}],
            )
            raw = resp.content[0].text.strip()
            if raw.startswith("```"):
                raw = "\n".join(raw.split("\n")[1:])
                if raw.endswith("```"):
                    raw = raw[: raw.rfind("```")]
            data = json.loads(raw)

            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            old_conf = (r.get("root_cause") or {}).get("confidence", "")
            r["llm_reviewed_at"] = stamp
            r["mode"] = "llm_reviewed"
            if data.get("what_happened"):
                r["what_happened"] = data["what_happened"]
            rc = r.setdefault("root_cause", {})
            if data.get("root_cause_description"):
                rc["description"] = data["root_cause_description"]
            if data.get("root_cause_confidence"):
                rc["confidence"] = data["root_cause_confidence"]
            htf = r.setdefault("how_to_fix", {})
            if data.get("how_to_fix_immediate"):
                htf["immediate"] = data["how_to_fix_immediate"]
            if data.get("how_to_fix_permanent"):
                htf["permanent"] = data["how_to_fix_permanent"]
            if data.get("how_to_fix_verify"):
                htf["verify"] = data["how_to_fix_verify"]
            if data.get("observability_gaps"):
                r["observability_gaps"] = data["observability_gaps"]
            if data.get("rca_markdown"):
                (REPORTS_DIR / rid / "rca.md").write_text(data["rca_markdown"], encoding="utf-8")

            _atomic_write(rp, r)
            new_conf = rc.get("confidence", "")
            improved.append({
                "id": rid,
                "service": r.get("service", ""),
                "alert": r.get("alert", ""),
                "was_confidence": old_conf,
                "now_confidence": new_conf,
            })
            log.info("learning: llm_review improved %s (%s → %s)", rid, old_conf, new_conf)
        except Exception as exc:
            log.warning("learning: llm_review failed for %s: %s", rid, exc)

    log.info("learning: llm_review complete; improved %d / %d eligible reports",
             len(improved), len(eligible))
    return improved


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def _grafana_explore_url(service: str, namespace: str) -> str:
    """Return a Grafana Explore deep-link for CPU + memory P95/P99 for the service, or ''."""
    base = os.getenv("GRAFANA_PUBLIC_URL", "").rstrip("/")
    if not base:
        return ""
    import urllib.parse
    ns = namespace or "default"
    cpu_expr = f'quantile(0.95, rate(container_cpu_usage_seconds_total{{namespace="{ns}",container=~"{service}"}}[5m]))'
    mem_expr = f'quantile(0.99, container_memory_working_set_bytes{{namespace="{ns}",container=~"{service}"}})'
    q = urllib.parse.quote(json.dumps({
        "datasource": "mimir",
        "queries": [
            {"expr": cpu_expr, "refId": "CPU"},
            {"expr": mem_expr, "refId": "MEM"},
        ],
        "range": {"from": "now-30d", "to": "now"},
    }))
    return f"{base}/explore?left={q}"


def _write_self_learning_report(
    rediagnosed_reports: list[dict],
    gap_results: list[dict],
    calibration: dict,
    thresholds: dict,
    errors: list,
    llm_reviewed: list[dict] | None = None,
) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    report_id = "rca-self-learning-" + stamp[:19].replace(":", "").replace("-", "").replace("T", "T")
    llm_reviewed = llm_reviewed or []

    n_rediag = len(rediagnosed_reports)
    new_issues = [g for g in gap_results if g.get("is_new")]
    n_new_issues = len(new_issues)
    n_families = len([k for k in calibration if not k.startswith("_") and isinstance(calibration[k], dict)])
    n_services = len([k for k in thresholds if not k.startswith("_") and isinstance(thresholds[k], dict)])
    n_llm = len(llm_reviewed)

    parts = []
    if n_rediag:
        parts.append(f"{n_rediag} unknown{'s' if n_rediag != 1 else ''} rediagnosed")
    if n_new_issues:
        parts.append(f"{n_new_issues} gap issue{'s' if n_new_issues != 1 else ''} opened")
    if n_families:
        parts.append(f"{n_families} families calibrated")
    if n_services:
        parts.append(f"{n_services} services tuned")
    if n_llm:
        parts.append(f"{n_llm} report{'s' if n_llm != 1 else ''} LLM-reviewed")
    if errors:
        parts.append(f"{len(errors)} error(s)")
    summary = "Self-learning pass: " + (", ".join(parts) if parts else "nothing to update")

    # --- rediagnosed reports section ---
    if rediagnosed_reports:
        rediag_rows = "\n".join(
            f"| [{r['id']}](/{r['id']}/rca.md) | {r['service']} | {r['old_cause']} | → {r['new_cause']} |"
            for r in rediagnosed_reports
        )
        rediag_section = f"""## Rediagnosed reports

These previously-unknown incidents were re-diagnosed from historical Mimir signals at incident time.

| Report | Service | Was | Now |
|--------|---------|-----|-----|
{rediag_rows}

Root cause confidence is set to **low**; it will be promoted on the next calibration pass once the alert shows cleared-within-1h.
"""
    else:
        rediag_section = "## Rediagnosed reports\n\nNo unknown-root-cause reports eligible this week.\n"

    # --- gap issues section ---
    if gap_results:
        gap_rows = "\n".join(
            (
                f"| {g['service']} | {g['gap_type'].replace('_', ' ')} | {g['count']} | "
                + (f"[#{g['issue_url'].split('/')[-1]}]({g['issue_url']})" if g["issue_url"] else "—")
                + (" 🆕" if g.get("is_new") else "")
                + f" | {', '.join(('[' + rid + '](/' + rid + '/rca.md)') for rid in g['example_reports'][:3])} |"
            )
            for g in sorted(gap_results, key=lambda x: -x["count"])
        )
        gap_section = f"""## Observability gap issues

Gaps that appear in ≥ {MIN_GAP_COUNT} reports trigger a GitHub issue. 🆕 = opened this run.

| Service | Gap type | Reports | Issue | Example reports |
|---------|----------|---------|-------|-----------------|
{gap_rows}
"""
    else:
        gap_section = f"## Observability gap issues\n\nNo gaps reached the {MIN_GAP_COUNT}-report threshold, or gap tracker was skipped.\n"

    # --- calibration section ---
    def _weight_arrow(w: float) -> str:
        if w >= 0.9:
            return "✅ high"
        if w >= 0.5:
            return "⚠️ medium"
        return "🔻 low"

    if n_families:
        cal_rows = "\n".join(
            f"| {cat} | {v.get('total', 0)} | {v.get('cleared_within_1h', 0)} | "
            f"{v.get('wrong_rediagnosis', 0)} | {v.get('accuracy', 0):.0%} | "
            f"{v.get('confidence_weight', 1.0):.2f} | {_weight_arrow(v.get('confidence_weight', 1.0))} |"
            for cat, v in sorted(calibration.items(), key=lambda x: -(x[1].get("total", 0) if isinstance(x[1], dict) else 0))
            if not cat.startswith("_") and isinstance(v, dict) and "total" in v
        )
        cal_section = f"""## Confidence calibration

Accuracy = 1 − (wrong re-diagnoses / total). Weight < 0.5 → confidence downgraded to medium; < 0.4 → low.

| Family | Incidents | Cleared ≤1h | Wrong re-diag | Accuracy | Weight | Level |
|--------|-----------|-------------|---------------|----------|--------|-------|
{cal_rows}
"""
    else:
        cal_section = "## Confidence calibration\n\nNo incident families with enough history to calibrate.\n"

    # --- threshold section ---
    def _thr_direction(v: dict) -> str:
        cpu = v.get("cpu_p95_max", _DEFAULT_CPU_MAX)
        ooms = v.get("oom_incidents", 0)
        cleans = v.get("clean_capacity_reviews", 0)
        if ooms >= 2:
            return "🔽 tightened"
        if ooms == 1:
            return "↘ slightly tightened"
        if cleans >= 3:
            return "↗ slightly relaxed"
        return "→ default"

    if n_services:
        thr_rows = "\n".join(
            f"| {svc} | {v.get('cpu_p95_max', '—')} | {v.get('memory_p99_max', '—')} | "
            f"{v.get('oom_incidents', 0)} | {v.get('clean_capacity_reviews', 0)} | "
            f"{_thr_direction(v)} |"
            + (f" [telemetry]({_grafana_explore_url(svc.split(':')[0], svc.split(':')[1] if ':' in svc else '')}) |"
               if _grafana_explore_url(svc.split(':')[0], svc.split(':')[1] if ':' in svc else "") else " — |")
            for svc, v in sorted(thresholds.items(), key=lambda x: x[0])
            if not svc.startswith("_") and isinstance(v, dict) and "cpu_p95_max" in v
        )
        thr_section = f"""## Threshold decisions

Thresholds are written to `.thresholds.json` and read by `scale_down.py` on every daily run.
Defaults: CPU P95 ≤ {_DEFAULT_CPU_MAX}, Mem P99 ≤ {_DEFAULT_MEM_MAX}.

| Service | CPU P95 max | Mem P99 max | OOM incidents | Clean reviews | Decision | Telemetry |
|---------|-------------|-------------|---------------|---------------|----------|-----------|
{thr_rows}
"""
    else:
        thr_section = "## Threshold decisions\n\nNo services with capacity history found.\n"

    # --- llm review section ---
    if llm_reviewed:
        conf_arrow = {"low": "🔺", "medium": "⚠️", "high": "✅"}
        llm_rows = "\n".join(
            f"| [{r['id']}](/{r['id']}/rca.md) | {r['service']} | {r['alert']} | "
            f"{conf_arrow.get(r['was_confidence'], r['was_confidence'])} {r['was_confidence']} | "
            f"{conf_arrow.get(r['now_confidence'], r['now_confidence'])} {r['now_confidence']} |"
            for r in llm_reviewed
        )
        llm_review_section = f"""## LLM report review

Claude re-reviewed these `rule_based` reports using their stored evidence.
Improvements: narrative (`what_happened`), fix commands (`how_to_fix`), gap descriptions, full RCA Markdown.

| Report | Service | Alert | Was | Now |
|--------|---------|-------|-----|-----|
{llm_rows}

Reports are tagged `mode: llm_reviewed` and their `rca.md` is replaced in-place.
"""
    else:
        no_key_note = " (set ANTHROPIC_API_KEY to enable)" if not config.ANTHROPIC_API_KEY else ""
        llm_review_section = f"## LLM report review\n\nNo rule_based reports eligible or pass skipped{no_key_note}.\n"

    # --- errors ---
    err_section = ""
    if errors:
        err_section = "\n## Errors\n\nThe following modules failed this run:\n\n" + "\n".join(f"- `{e}`" for e in errors) + "\n"

    md = f"""# Self-learning report · {stamp[:10]}

**{summary}**

## Summary

| Pass | Result |
|------|--------|
| Unknown rediagnosis | {n_rediag} report(s) updated |
| Gap tracker | {n_new_issues} new issue(s) opened · {len(gap_results)} total tracked gaps |
| Confidence calibration | {n_families} famil{'ies' if n_families != 1 else 'y'} |
| Threshold tuning | {n_services} service{'s' if n_services != 1 else ''} |
| LLM report review | {n_llm} report{'s' if n_llm != 1 else ''} improved |
{"| **Errors** | " + ", ".join(errors) + " |" if errors else ""}

{rediag_section}
{gap_section}
{cal_section}
{thr_section}
{llm_review_section}{err_section}"""

    report = {
        "id": report_id,
        "report_type": "self_learning",
        "generated_at": stamp,
        "service": "platform",
        "namespace": "all",
        "cluster": os.getenv("SCALE_DOWN_CLUSTER", os.getenv("HEALTH_CLUSTER", "platform")),
        "workflow": os.getenv("WORKFLOW_NAME", ""),
        "summary": summary,
        "self_learning": {
            "rediagnosed": n_rediag,
            "rediagnosed_reports": rediagnosed_reports,
            "gap_issues_opened": n_new_issues,
            "gap_results": gap_results,
            "calibration": {k: v for k, v in calibration.items() if not k.startswith("_")},
            "thresholds": {k: v for k, v in thresholds.items() if not k.startswith("_")},
            "llm_reviewed": llm_reviewed,
            "errors": errors,
        },
    }
    report_dir = REPORTS_DIR / report_id
    report_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(report_dir / "report.json", report)
    (report_dir / "rca.md").write_text(md, encoding="utf-8")
    log.info("learning: self-learning report written to %s", report_id)


def run_self_learning() -> None:
    log.info("learning: starting self-learning pass")
    errors: list[str] = []
    rediagnosed_reports: list[dict] = []
    gap_results: list[dict] = []
    calibration: dict = {}
    thresholds: dict = {}
    llm_reviewed: list[dict] = []
    for name, fn in [
        ("unknown_rediagnosis", run_unknown_rediagnosis),
        ("gap_tracker", run_gap_tracker),
        ("confidence_calibration", run_confidence_calibration),
        ("threshold_tuning", run_threshold_tuning),
        ("llm_report_review", run_llm_report_review),
    ]:
        try:
            result = fn()
            if name == "unknown_rediagnosis":
                rediagnosed_reports = result or []
            elif name == "gap_tracker":
                gap_results = result or []
            elif name == "confidence_calibration":
                calibration = result or {}
            elif name == "threshold_tuning":
                thresholds = result or {}
            elif name == "llm_report_review":
                llm_reviewed = result or []
        except Exception as exc:
            log.exception("learning: %s failed: %s", name, exc)
            errors.append(name)
    try:
        _write_self_learning_report(rediagnosed_reports, gap_results, calibration, thresholds, errors, llm_reviewed)
    except Exception as exc:
        log.exception("learning: could not write self-learning report: %s", exc)
    if errors:
        raise RuntimeError(f"self-learning modules failed: {', '.join(errors)}")
    log.info("learning: self-learning pass complete")
