#!/usr/bin/env python3
"""Runs every query of every generated dashboard and alert rule against the live Mimir and Loki and reports the ones that
fail or return nothing. Needs port-forwards:  kubectl --context kind-observability -n observability port-forward svc/mimir 18080:8080 svc/loki 13100:3100
Usage: python3 scripts/check_queries.py [mimir_url] [loki_url]
"""
import glob
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIMIR = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:18080") + "/prometheus"
LOKI = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:13100"

VARS = {"cluster": ".+", "namespace": ".+", "node": ".+", "ingress": ".+", "project": ".+", "dest": ".+", "service": ".+", "level": ".+", "search": "",
        "workload": "orders-api", "__interval": "5m", "__range": "1h", "__rate_interval": "5m"}


SINGLE = {"cluster": "platform", "namespace": "demo", "workload": "orders-api"}  # dashboards whose variables are single select


def sub(expr, where=""):
    vars_ = {**VARS, **(SINGLE if where.startswith("obs-workload") else {})}
    for k, v in vars_.items():
        expr = expr.replace("${" + k + "}", v).replace("$" + k, v)
    return expr


def get(url, params):
    full = url + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(full, timeout=30) as r:
        return json.load(r)


def run_prom(expr):
    return get(MIMIR + "/api/v1/query", {"query": expr})["data"]["result"]


def run_loki(expr):
    now = int(time.time())
    if re.match(r"^\s*(sum|count|topk|rate|avg|max|min|bytes)", expr) or "[" in expr and "(" in expr:
        return get(LOKI + "/loki/api/v1/query", {"query": expr, "time": now})["data"]["result"]
    return get(LOKI + "/loki/api/v1/query_range", {"query": expr, "start": (now - 3600) * 10**9, "end": now * 10**9, "limit": 5})["data"]["result"]


def targets():
    for f in sorted(glob.glob(os.path.join(ROOT, "gitops/observability/dashboards/obs-*.yaml"))):
        d = yaml.safe_load(open(f))
        dash = json.loads(next(iter(d["data"].values())))
        for p in dash["panels"]:
            for t in p.get("targets", []):
                if t.get("expr"):
                    kind = "loki" if t.get("datasource", {}).get("type") == "loki" else "prom"
                    yield os.path.basename(f), p.get("title", ""), kind, t["expr"]
    d = yaml.safe_load(open(os.path.join(ROOT, "gitops/observability/alerting/rules-generic.yaml")))
    inner = yaml.safe_load(next(iter(d["data"].values())))
    for g in inner["groups"]:
        for r in g["rules"]:
            yield "rules-generic", r["title"], "prom", r["data"][0]["model"]["expr"]


bad = empty = ok = 0
for where, title, kind, expr in targets():
    e = sub(expr, where)
    try:
        res = run_prom(e) if kind == "prom" else run_loki(e)
    except Exception as exc:  # noqa: BLE001
        bad += 1
        msg = str(exc)
        if hasattr(exc, "read"):
            msg = exc.read().decode()[:200]
        print(f"ERROR  {where} / {title}: {msg}\n       {e[:160]}")
        continue
    if not res and where == "rules-generic":
        # an alert query is empty while everything is healthy: it is only wrong when one of its metrics does not exist
        missing = [m for m in sorted(set(re.findall(r"\b([a-z][a-z0-9_]*_[a-z0-9_]+)(?=[\{\[\)\s]|$)", e))) if m not in ("by", "on", "without", "group_left", "histogram_quantile", "vector", "count") and not run_prom(f'count({{__name__="{m}"}})')]
        if missing:
            bad += 1
            print(f"MISSING METRIC  {title}: {missing}")
        else:
            ok += 1
    elif not res:
        empty += 1
        print(f"EMPTY  {where} / {title}\n       {e[:160]}")
    else:
        ok += 1
print(f"\n{ok} queries returned data, {empty} empty, {bad} errors")
