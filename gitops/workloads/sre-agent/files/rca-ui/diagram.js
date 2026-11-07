/* Architecture diagram of a report: the platform's three clusters, with the component that has the problem in red, what it affects in amber
   and everything the report found no problem with in green. It is drawn from the fields of report.json (alert, service, root cause, layer),
   so it needs nothing the agent does not already write. `analyze` is pure (tested in node); `draw` builds the SVG. */
(function (root) {
  "use strict";

  // id -> [label, sub-label, x, y, w, h, cluster]
  const NODES = {
    users: ["Users", "browsers, API clients", 40, 14, 190, 46, null],
    gw: ["Gateway (Traefik)", "routes and TLS", 50, 120, 190, 56, "platform"],
    cert: ["cert-manager", "certificates", 270, 120, 170, 56, "platform"],
    kyverno: ["Kyverno", "admission policies", 470, 120, 190, 56, "platform"],
    svc: ["Workload", "the service of the alert", 50, 250, 250, 64, "platform"],
    other: ["Other workloads", "demo, sandbox", 330, 250, 190, 64, "platform"],
    alloy: ["Alloy", "collector", 550, 250, 110, 64, "platform"],
    nodes: ["Kubernetes nodes, control plane and DNS", "", 50, 400, 610, 60, "platform"],
    argocd: ["Argo CD", "deploys from Git", 730, 120, 150, 56, "gitops"],
    portal: ["Developer portal", "Backstage", 900, 120, 160, 56, "gitops"],
    mimir: ["Mimir", "metrics", 730, 284, 140, 46, "observability"],
    loki: ["Loki", "logs", 730, 344, 140, 46, "observability"],
    tempo: ["Tempo", "traces", 730, 404, 140, 46, "observability"],
    grafana: ["Grafana", "dashboards and alerts", 910, 330, 150, 56, "observability"],
    agent: ["SRE agent", "investigates alerts", 910, 420, 150, 50, "observability"],
    workflows: ["Argo Workflows", "runs investigations", 730, 500, 150, 46, "observability"],
    reports: ["RCA reports", "this viewer", 910, 500, 150, 46, "observability"],
  };
  const CLUSTERS = {
    platform: ["Platform cluster", 20, 84, 660, 420],
    gitops: ["GitOps cluster", 710, 84, 370, 130],
    observability: ["Observability cluster", 710, 244, 370, 330],
  };
  // [from, to, label, path]  (path: a list of points; a plain pair is joined with a curve)
  const EDGES = [
    ["users", "gw", "HTTPS", [[125, 60], [125, 120]]],
    ["cert", "gw", "", [[270, 148], [240, 148]]],
    ["gw", "svc", "routes", [[100, 176], [100, 250]]],
    ["gw", "other", "routes", [[220, 176], [380, 250]]],
    ["kyverno", "svc", "admission", [[565, 176], [565, 200], [235, 200], [235, 250]]],
    ["argocd", "svc", "deploys", [[805, 176], [805, 226], [170, 226], [170, 250]]],
    ["nodes", "svc", "runs", [[175, 400], [175, 314]]],
    ["nodes", "other", "runs", [[425, 400], [425, 314]]],
    ["nodes", "alloy", "runs", [[605, 400], [605, 314]]],
    ["alloy", "mimir", "", [[660, 270], [730, 307]]],
    ["alloy", "loki", "", [[660, 282], [730, 367]]],
    ["alloy", "tempo", "", [[660, 296], [730, 427]]],
    ["mimir", "grafana", "", [[870, 307], [910, 350]]],
    ["loki", "grafana", "", [[870, 367], [910, 358]]],
    ["tempo", "grafana", "", [[870, 427], [910, 366]]],
    ["grafana", "agent", "alert webhook", [[985, 386], [985, 420]]],
    ["agent", "reports", "writes", [[985, 470], [985, 500]]],
    ["agent", "workflows", "", [[910, 450], [880, 523]]],
  ];
  // what a failure of a component takes with it
  const IMPACT = {
    svc: ["users"],
    gw: ["users", "svc", "other"],
    cert: ["gw", "users"],
    kyverno: ["svc", "other"],
    argocd: ["svc", "other"],
    nodes: ["gw", "svc", "other", "alloy", "kyverno", "cert"],
    alloy: ["mimir", "loki", "tempo", "grafana", "agent"],
    mimir: ["grafana", "agent"],
    loki: ["grafana", "agent"],
    tempo: ["grafana"],
    grafana: ["agent"],
    agent: ["reports"],
    workflows: ["agent", "reports"],
    reports: [],
    portal: ["users"],
    other: [],
    users: [],
  };
  const BY_SERVICE = [
    [/kyverno/, "kyverno"], [/cert-?manager/, "cert"], [/^traefik|gateway/, "gw"], [/^alloy/, "alloy"], [/^mimir/, "mimir"], [/^loki/, "loki"],
    [/^tempo/, "tempo"], [/^grafana/, "grafana"], [/sre-agent|alert-webhook|incident/, "agent"], [/argo-?workflows|workflow-controller/, "workflows"],
    [/rca-viewer/, "reports"], [/^argocd|argo-cd/, "argocd"], [/^portal$|^postgres$/, "portal"],
  ];

  function byService(name) {
    const s = String(name || "").toLowerCase().replace(/^(platform|observability|gitops)-/, "");
    for (const [rx, id] of BY_SERVICE) if (rx.test(s)) return id;
    return null;
  }

  function analyze(r) {
    const alert = String((r && r.alert) || "");
    const category = String((r.root_cause && r.root_cause.category) || "");
    const service = String((r && r.service) || "");
    const component = String((r.fault_domain && r.fault_domain.component) || "");
    let id = null;
    let label = null;
    let impacted = null;
    const backend = component.match(/route\s+(?:[\w.-]+\/)?([\w.-]+)/);
    if (/^Gateway/.test(alert) && category === "ingress_backend_failure" && backend) {
      id = "svc";                       // the gateway is fine: one backend behind it is what fails
      label = backend[1];
    } else if (/^GatewayCertificate/.test(alert) || /^certificate/.test(category)) id = "cert";
    else if (/^Gateway/.test(alert)) id = "gw";
    else if (/^Node|^Kubelet|^ApiServer|^CoreDns/.test(alert)) id = "nodes";
    else if (/^(ClusterTelemetryMissing|TelemetryNotDelivered|LogsDropped)$/.test(alert)) id = "alloy";
    else if (/^InvestigationWorkflowFailed$/.test(alert)) id = "agent";
    else if (/^Datasource/.test(alert)) id = "grafana";
    else id = byService(service);
    if (/^Ingress/.test(alert)) impacted = new Set(["users"]);   // slow or failing answers reach the users of that service only
    if (!id) id = "svc";
    if (!impacted) impacted = new Set(IMPACT[id] || []);
    impacted.delete(id);
    if (id === "svc" && !label) label = service || "Workload";
    return { root: id, impacted, label, service };
  }

  const NS = "http://www.w3.org/2000/svg";
  function s(tag, attrs, text) {
    const n = document.createElementNS(NS, tag);
    for (const k in attrs || {}) n.setAttribute(k, attrs[k]);
    if (text !== undefined) n.textContent = text;
    return n;
  }
  const clip = (t, n) => (t.length > n ? t.slice(0, n - 1) + "…" : t);

  // edges on the shortest path (two edges at most) between the problem and each affected component: red next to the problem, amber further away
  function pathEdges(a) {
    const marks = new Map();
    const touching = (i, id) => EDGES[i][0] === id || EDGES[i][1] === id;
    const other = (i, id) => (EDGES[i][0] === id ? EDGES[i][1] : EDGES[i][0]);
    for (const target of a.impacted) {
      const direct = EDGES.findIndex((e) => (e[0] === a.root && e[1] === target) || (e[1] === a.root && e[0] === target));
      if (direct >= 0) { marks.set(direct, "bad"); continue; }
      let found = false;
      for (let i = 0; i < EDGES.length && !found; i++) {
        if (!touching(i, a.root)) continue;
        const mid = other(i, a.root);
        for (let j = 0; j < EDGES.length; j++) {
          if (j !== i && touching(j, mid) && other(j, mid) === target) { marks.set(i, "bad"); if (!marks.has(j)) marks.set(j, "warn"); found = true; break; }
        }
      }
    }
    return marks;
  }

  // live: "firing", "pending", "resolved" or "unknown" (from Grafana); a resolved alert turns everything green, the rest draws what the report found
  function draw(r, live) {
    const a = analyze(r);
    const recovered = live === "resolved";
    const state = (id) => (recovered ? "ok" : id === a.root ? "bad" : a.impacted.has(id) ? "warn" : "ok");
    const nameOf = (id) => (id === "svc" ? a.label || NODES.svc[0] : NODES[id][0]);
    const svg = s("svg", { viewBox: "0 0 1100 600", class: "arch", role: "img" });
    const impactedNames = [...a.impacted].map(nameOf);
    svg.setAttribute("aria-label", recovered ? `Architecture: the problem in ${nameOf(a.root)} has recovered.` : `Architecture: the problem is in ${nameOf(a.root)}. ${impactedNames.length ? "Affected: " + impactedNames.join(", ") + "." : "Nothing else is affected."}`);
    const defs = s("defs");
    for (const k of ["bad", "warn", "idle"]) {
      const m = s("marker", { id: "ah-" + k, viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" });
      m.append(s("path", { d: "M0,0 L10,5 L0,10 z", class: "ah " + k }));
      defs.append(m);
    }
    svg.append(defs);

    for (const [id, [title, x, y, w, h]] of Object.entries(CLUSTERS)) {
      svg.append(s("rect", { x, y, width: w, height: h, rx: 16, class: "cluster" }));
      svg.append(s("text", { x: x + 16, y: y + 24, class: "cluster-t" }, title));
    }

    const marks = recovered ? new Map() : pathEdges(a);
    for (const [i, [from, to, label, pts]] of EDGES.entries()) {
      const sf = state(from), st = state(to);
      const kind = marks.get(i) || (sf !== "ok" && st !== "ok" ? "warn" : "idle");
      let d;
      if (pts.length === 2) {
        const [[x1, y1], [x2, y2]] = pts;
        const horizontal = Math.abs(x2 - x1) > Math.abs(y2 - y1);
        d = horizontal ? `M${x1},${y1} C${(x1 + x2) / 2},${y1} ${(x1 + x2) / 2},${y2} ${x2},${y2}` : `M${x1},${y1} C${x1},${(y1 + y2) / 2} ${x2},${(y1 + y2) / 2} ${x2},${y2}`;
      } else d = "M" + pts.map((p) => p.join(",")).join(" L");
      svg.append(s("path", { d, class: "edge " + kind, "marker-end": `url(#ah-${kind})` }));
      if (label) {
        const [mx, my] = pts.length === 2 ? [(pts[0][0] + pts[1][0]) / 2, (pts[0][1] + pts[1][1]) / 2] : [(pts[1][0] + pts[2][0]) / 2, pts[1][1] === pts[2][1] ? pts[1][1] : (pts[1][1] + pts[2][1]) / 2];
        svg.append(s("text", { x: mx + (pts.length === 2 && pts[0][0] === pts[1][0] ? 6 : 0), y: my - 4, class: "edge-t", "text-anchor": pts.length === 2 && pts[0][0] === pts[1][0] ? "start" : "middle" }, label));
      }
    }

    for (const [id, [title, sub, x, y, w, h]] of Object.entries(NODES)) {
      const st = state(id);
      const g = s("g", { class: "node " + st });
      g.append(s("title", {}, `${nameOf(id)}: ${st === "bad" ? "the problem is here" : st === "warn" ? "affected" : recovered ? "recovered, the alert is no longer firing" : "no problem found in this report"}`));
      g.append(s("rect", { x, y, width: w, height: h, rx: 12, class: "box" }));
      g.append(s("circle", { cx: x + 16, cy: y + h / 2, r: 6, class: "dot" }));
      const label = id === "svc" ? clip(nameOf(id), 28) : title;
      g.append(s("text", { x: x + 32, y: y + (sub ? h / 2 - 3 : h / 2 + 5), class: "label" }, label));
      if (sub) g.append(s("text", { x: x + 32, y: y + h / 2 + 15, class: "sub" }, st === "bad" ? "problem here" : st === "warn" ? "affected" : recovered && id === a.root ? "recovered" : sub));
      svg.append(g);
    }
    return { svg, analysis: a, nameOf };
  }

  const api = { analyze, draw, NODES, EDGES, IMPACT };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.RcaDiagram = api;
})(typeof window !== "undefined" ? window : globalThis);
