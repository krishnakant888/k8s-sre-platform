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


def run_unknown_rediagnosis() -> int:
    """Re-diagnose reports with unknown root cause using historical Mimir data.
    Returns the number of reports successfully updated."""
    reports = _load_all_reports()
    now = datetime.now(timezone.utc).timestamp()
    min_age = REDIAGNOSIS_MIN_AGE_HOURS * 3600
    updated = 0

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
            r["root_cause"] = {
                **rc,
                "category": new_cat,
                "confidence": "low",
                "description": (
                    f"Re-diagnosed from historical Mimir signals at incident time "
                    f"(original: {rc.get('category', 'unknown')})."
                ),
            }
            updated += 1
            log.info("learning: rediagnosed %s → %s", r["id"], new_cat)
        else:
            log.debug("learning: rediagnosis inconclusive for %s", r["id"])

        _atomic_write(rp, r)

    log.info("learning: rediagnosis complete; updated %d of eligible reports", updated)
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


def run_gap_tracker() -> int:
    """Open GitHub issues for observability gaps that repeat ≥ MIN_GAP_COUNT times.
    Returns number of new issues opened."""
    if not ISSUES_REPO or not getattr(config, "GITHUB_TOKEN", ""):
        log.info("learning: gap_tracker skipped (no ISSUES_REPO or GITHUB_TOKEN)")
        return 0

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

    opened = 0
    for (svc, gap_type), data in by_svc_gap.items():
        if data["count"] < MIN_GAP_COUNT:
            continue
        issue_key = f"{svc}:{gap_type}"
        if issue_key in filed:
            continue

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

    _atomic_write(GAP_ISSUES_FILE, filed)
    log.info("learning: gap_tracker complete; opened %d new issues", opened)
    return opened


# ---------------------------------------------------------------------------
# 3. Confidence calibration
# ---------------------------------------------------------------------------

def _alert_cleared_at_t(alert: str, svc: str, t: float) -> bool | None:
    """Return True if the alert had cleared by time t, False if still firing, None if unknown."""
    # Query Mimir ALERTS metric (synthetic series present while rule is firing)
    sel = f'alertname="{alert}"'
    if svc:
        sel += f',service=~".*{re.escape(svc)}.*"'
    still_firing = _mimir_at(f"ALERTS{{{sel}}}", t)
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
            cleared = _alert_cleared_at_t(r.get("alert", ""), r.get("service", ""),
                                          gen_t + 3600)
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
        # accuracy: fraction that cleared OR recurred with same cause (correctly ID'd)
        correct = cleared + (total - cleared - wrong)
        accuracy = round(correct / total, 3) if total else 1.0
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
    for r in reports:
        svc = r.get("service", "")
        if not svc:
            continue
        rt = r.get("report_type", "incident")
        if rt == "incident" and (r.get("root_cause") or {}).get("category") == "resource_exhaustion":
            oom_counts[svc] += 1
        if rt == "capacity":
            cap = r.get("capacity", {}) or {}
            if (cap.get("decision") in ("skipped", "recommendation")
                    and cap.get("review_type") != "weekly_floor"):
                clean_caps[svc] += 1

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

        thresholds[svc] = {
            "cpu_p95_max": cpu,
            "memory_p99_max": mem,
            "reason": reason,
            "oom_incidents": ooms,
            "clean_capacity_reviews": cleans,
        }

    thresholds["_generated_at"] = (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
    _atomic_write(THRESHOLDS_FILE, thresholds)
    log.info("learning: threshold_tuning written; %d services", len(thresholds) - 1)
    return thresholds


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def run_self_learning() -> None:
    log.info("learning: starting self-learning pass")
    errors = []
    for name, fn in [
        ("unknown_rediagnosis", run_unknown_rediagnosis),
        ("gap_tracker", run_gap_tracker),
        ("confidence_calibration", run_confidence_calibration),
        ("threshold_tuning", run_threshold_tuning),
    ]:
        try:
            fn()
        except Exception as exc:
            log.exception("learning: %s failed: %s", name, exc)
            errors.append(name)
    if errors:
        raise RuntimeError(f"self-learning modules failed: {', '.join(errors)}")
    log.info("learning: self-learning pass complete")
