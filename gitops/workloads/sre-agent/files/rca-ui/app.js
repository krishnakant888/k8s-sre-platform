/* RCA report viewer: a list of the reports on the volume and each one rendered from its rca.md.
   Data comes from the same origin: /_list/ (nginx directory listing as JSON), /<id>/report.json and /<id>/rca.md. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };

  const state = { reports: [], filter: new Set(), query: "", type: "all", current: null, loaded: false, liveTimer: null, live: {} };
  const meta = new Map(); // id -> report.json

  // ---- theme -------------------------------------------------------------------------------------------------
  const root = document.documentElement;
  try { const t = localStorage.getItem("rca-theme"); if (t) root.dataset.theme = t; } catch (_) { /* storage can be blocked */ }
  $("theme").addEventListener("click", () => {
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("rca-theme", root.dataset.theme); } catch (_) { /* ignore */ }
  });

  // ---- helpers -----------------------------------------------------------------------------------------------
  const when = (iso) => (iso ? new Date(iso) : null);
  const fmtTime = (d) => (d ? d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "");
  function ago(d) {
    if (!d) return "";
    const s = Math.max(0, (Date.now() - d.getTime()) / 1000);
    if (s < 90) return "just now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} d ago`;
  }
  const level = (r) => (r.priority && r.priority.level) || "";
  const pill = (text, cls) => { const p = el("span", `pill ${cls || ""}`); p.append(el("span", "dot"), document.createTextNode(text)); return p; };
  const words = (s) => String(s || "").replace(/_/g, " ");
  function toast(text) { const t = el("div", "toast", text); document.body.append(t); setTimeout(() => t.remove(), 1600); }
  const getJson = async (url) => { const r = await fetch(url, { cache: "no-store" }); if (!r.ok) throw new Error(`${url}: ${r.status}`); return r.json(); };
  const getText = async (url) => { const r = await fetch(url, { cache: "no-store" }); if (!r.ok) throw new Error(`${url}: ${r.status}`); return r.text(); };

  // ---- the list ----------------------------------------------------------------------------------------------
  async function loadList() {
    const dirs = (await getJson("/_list/")).filter((e) => e.type === "directory" && e.name.startsWith("rca-")).map((e) => e.name);
    const fresh = dirs.filter((d) => !meta.has(d));
    await Promise.all(fresh.map(async (d) => {
      try { meta.set(d, await getJson(`/${d}/report.json`)); } catch (_) { meta.set(d, { id: d, alert: d, service: "", priority: {}, root_cause: {}, fault_domain: {}, generated_at: "" }); }
    }));
    state.reports = dirs.map((d) => meta.get(d)).sort((a, b) => String(b.started_at || b.id).localeCompare(String(a.started_at || a.id)) || b.id.localeCompare(a.id));
    state.loaded = true;
    try {
      const data = await getJson("/_status/rules");
      for (const r of state.reports) {
      if (r.report_type === "capacity") { state.live[r.id] = "capacity"; continue; }
      if (savedResolved(r.id)) { state.live[r.id] = "resolved"; continue; }
        const s = statusFrom(data, r);
        state.live[r.id] = s.state;
        if (s.state === "resolved") saveResolved(r.id, (s.at || new Date()).toISOString());
      }
    } catch (_) { for (const r of state.reports) if (savedResolved(r.id)) state.live[r.id] = "resolved"; }
    renderChips();
    renderList();
  }

  function renderChips() {
    const box = $("chips");
    const present = [...new Set(state.reports.map(level).filter(Boolean))].sort();
    box.replaceChildren(...present.map((p) => {
      const b = el("button", "chip", p);
      b.setAttribute("aria-pressed", String(state.filter.has(p)));
      b.addEventListener("click", () => { state.filter.has(p) ? state.filter.delete(p) : state.filter.add(p); renderChips(); renderList(); });
      return b;
    }));
  }

  const visible = () => {
    const q = state.query.trim().toLowerCase();
    return state.reports.filter((r) => {
      if (state.filter.size && !state.filter.has(level(r))) return false;
      const type = r.report_type === "capacity" ? "capacity" : "incident";
      if (state.type !== "all" && state.type !== type) return false;
      if (!q) return true;
      return [r.id, r.alert, r.service, r.summary, r.report_type, r.capacity && r.capacity.decision,
        r.root_cause && r.root_cause.category, r.fault_domain && r.fault_domain.layer, level(r)].join(" ").toLowerCase().includes(q);
    });
  };

  function renderList() {
    const shown = visible();
    $("count").textContent = `${state.reports.length} report${state.reports.length === 1 ? "" : "s"}${shown.length !== state.reports.length ? `, ${shown.length} shown` : ""}`;
    const list = $("list");
    list.replaceChildren();
    if (!shown.length) { list.append(el("li", "none", state.loaded ? "No report matches." : "Loading…")); return; }
    let day = "";
    for (const r of shown) {
      const d = when(r.started_at || r.generated_at);
      const label = d ? d.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" }) : "";
      if (label !== day) { day = label; list.append(el("li", "day", label)); }
      const li = el("li");
      const b = el("button", "item");
      b.dataset.id = r.id;
      if (state.current === r.id) b.setAttribute("aria-current", "true");
      const top = el("div", "item-top");
      const isCapacity = r.report_type === "capacity";
      const capacity = r.capacity || {};
      if (isCapacity && r.root_cause && r.root_cause.category === "capacity_review") r.root_cause.category = "";
      const verification = capacity.post_merge || (r.remediation && r.remediation.post_merge_verification);
      const verificationStatus = verification && verification.status;
      if (isCapacity) {
        b.classList.add("capacity-record");
        if (verificationStatus === "healthy") b.classList.add("capacity-healthy");
        else if (verificationStatus === "needs_attention") b.classList.add("capacity-attention");
      }
      const capacityTitle = capacity.decision === "proposal" ? "Capacity plan" : "Capacity review";
      top.append(el("span", "item-title", isCapacity ? capacityTitle + " · " + (r.service || "workload") : (r.alert || r.id)));
      if (isCapacity) {
        top.append(pill("Capacity", "soft"));
        if (verificationStatus) {
          const label = verificationStatus === "healthy" ? "Healthy"
            : verificationStatus === "needs_attention" ? "Follow-up"
            : verificationStatus === "not_merged" ? "PR open" : "Pending";
          top.append(pill(label, verificationStatus === "healthy" ? "ok" : verificationStatus === "needs_attention" ? "p1" : "soft"));
        }
      }
      const done = state.live[r.id] === "resolved";
      if (done) b.classList.add("resolved");
      if (level(r)) top.append(pill(done ? level(r) + " ✓" : level(r), done ? "ok big" : level(r).toLowerCase() + " big"));
      if (isCapacity) {
        const priority = top.querySelector(".p4"); if (priority) priority.remove();
        const action = (capacity.proposed_change || capacity.reason || r.summary || "No change proposed").replace(/`/g, "");
        b.append(top, el("div", "item-sub item-action", action), el("div", "item-sub", "ID: " + r.id));
      } else b.append(top, el("div", "item-sub", "ID: " + r.id + (r.service ? " - " + r.service : "")));
      const m = el("div", "item-meta");
      m.append(el("span", "", ago(d)));
      if (isCapacity) {
        if (capacity.candidate_floor != null) m.append(el("span", "", "floor " + capacity.candidate_floor));
        if (verificationStatus === "healthy") m.append(el("span", "", "post-change checks passed"));
        else if (verificationStatus === "needs_attention") m.append(el("span", "", "check results need review"));
        else if (!verificationStatus && capacity.decision) m.append(el("span", "", words(capacity.decision)));
      }
      if (r.root_cause && r.root_cause.category) m.append(el("span", "", "· " + words(r.root_cause.category)));
      b.append(m);
      b.addEventListener("click", () => { location.hash = r.id; });
      li.append(b);
      list.append(li);
    }
  }

  // ---- one report --------------------------------------------------------------------------------------------
  marked.setOptions({ gfm: true, breaks: false });

  function enhance(body) {
    // timeline: "- 2026-...Z: text" lines become a vertical timeline
    for (const h of body.querySelectorAll("h2")) {
      if (/^timeline/i.test(h.textContent)) {
        const ul = h.nextElementSibling;
        if (ul && ul.tagName === "UL") {
          ul.classList.add("tl");
          for (const li of ul.children) {
            const m = li.textContent.match(/^(\d{4}-\d\d-\d\dT[\d:]+Z):\s*([\s\S]*)$/);
            if (m) { const t = el("time", "", fmtTime(new Date(m[1]))); t.dateTime = m[1]; li.textContent = ""; li.append(t, document.createTextNode(m[2])); }
          }
        }
      }
      // long evidence goes under a fold
      if (/^evidence/i.test(h.textContent)) {
        const nodes = []; let n = h.nextElementSibling;
        while (n && n.tagName !== "H2") { nodes.push(n); n = n.nextElementSibling; }
        if (nodes.length) { const d = el("details"); const s = el("summary", "", `Evidence (${nodes.filter((x) => x.tagName === "UL").reduce((a, u) => a + u.children.length, 0) || nodes.length} items)`); d.append(s, ...nodes); h.after(d); }
      }
    }
    // copy buttons on code blocks, and a copy button for the commands in the fix steps
    for (const pre of body.querySelectorAll("pre")) {
      const b = el("button", "copy", "Copy"); b.type = "button";
      b.addEventListener("click", () => navigator.clipboard.writeText(pre.innerText.replace(/\nCopy$/, "")).then(() => toast("Copied")));
      pre.append(b);
    }
    for (const li of body.querySelectorAll("li")) {
      const code = li.querySelector(":scope > code");
      if (code && /^(kubectl|helm|argocd|curl|git|docker)\b/.test(code.textContent)) {
        code.style.cursor = "copy"; code.title = "Click to copy";
        code.addEventListener("click", () => navigator.clipboard.writeText(code.textContent).then(() => toast("Copied")));
      }
    }
    for (const li of body.querySelectorAll("li")) {
      const first = li.firstChild && li.firstChild.type === "checkbox" ? li.childNodes[1] : li.firstChild;
      if (first && first.nodeType === 3) {
        const m = first.textContent.match(/^\s*((?:kubectl|helm|argocd|curl|git|docker)\b[^#]*?)(\s+#.*)?$/);
        if (m) {
          const code = el("code", "", m[1].trim()); code.style.cursor = "copy"; code.title = "Click to copy";
          code.addEventListener("click", () => navigator.clipboard.writeText(code.textContent).then(() => toast("Copied")));
          first.replaceWith(...(m[2] ? [code, el("span", "muted", m[2])] : [code]));
        }
      }
    }
    const lead = body.querySelector(":scope > p");
    if (lead && /^Report:/.test(lead.textContent)) lead.classList.add("meta-line");
    for (const a of body.querySelectorAll("a[href]")) { a.target = "_blank"; a.rel = "noopener noreferrer"; }
    for (const i of body.querySelectorAll('input[type="checkbox"]')) i.disabled = false, i.addEventListener("click", (e) => e.preventDefault());
  }

  function buildToc(body) {
    const toc = $("toc");
    const heads = [...body.querySelectorAll("h2")];
    toc.replaceChildren();
    if (heads.length < 3) return;
    toc.append(el("h3", "", "On this page"));
    heads.forEach((h, i) => {
      h.id = "s-" + i + "-" + h.textContent.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
      const a = el("a", "", h.textContent); a.href = "#" + h.id;
      a.addEventListener("click", (e) => { e.preventDefault(); h.scrollIntoView({ behavior: "smooth", block: "start" }); });
      toc.append(a);
    });
    const links = [...toc.querySelectorAll("a")];
    const obs = new IntersectionObserver((entries) => {
      for (const e of entries) if (e.isIntersecting) { links.forEach((l) => l.classList.toggle("on", l.getAttribute("href") === "#" + e.target.id)); }
    }, { root: $("main"), rootMargin: "0px 0px -75% 0px" });
    heads.forEach((h) => obs.observe(h));
  }

  // ---- live status: is the alert of this report still firing? --------------------------------------------------
  // One request to /_status/rules (Grafana's rule states, read-only) matches the report to an alert instance by the alert name and the service.
  // A resolved report is remembered in this browser and not asked about again: if the problem comes back, the agent writes a new report.
  const RESOLVED_KEY = (id) => "rca-resolved:" + id;
  function savedResolved(id) { try { return localStorage.getItem(RESOLVED_KEY(id)); } catch (_) { return null; } }
  function saveResolved(id, at) { try { localStorage.setItem(RESOLVED_KEY(id), at); } catch (_) { /* ignore */ } }

  async function liveStatus(r) {
    const saved = savedResolved(r.id);
    if (saved) return { state: "resolved", at: new Date(saved), cached: true };
    let data;
    try { data = await getJson("/_status/rules"); } catch (_) { return { state: "unknown", why: "Grafana could not be asked" }; }
    return statusFrom(data, r);
  }

  function statusFrom(data, r) {
    const rules = ((data && data.data && data.data.groups) || []).flatMap((g) => g.rules || []).filter((x) => x.name === r.alert);
    if (!rules.length) return { state: "unknown", why: "no alert rule named " + r.alert };
    const evaluated = new Date(rules.map((x) => x.lastEvaluation).filter(Boolean).sort().pop() || NaN);
    if (rules.some((x) => x.health === "error")) return { state: "unknown", why: "Grafana cannot evaluate the rule", evaluated };
    const svc = r.service || "";
    const mine = (a) => !svc || Object.values(a.labels || {}).some((v) => v === svc || String(v).startsWith(svc + "-"));
    const inst = rules.flatMap((x) => x.alerts || []).filter(mine);
    const firing = inst.filter((a) => /^(alerting|firing)$/i.test(a.state));
    if (firing.length) return { state: "firing", since: new Date(firing.map((a) => a.activeAt).sort()[0]), evaluated };
    if (inst.some((a) => /^pending$/i.test(a.state))) return { state: "pending", evaluated };
    if (inst.some((a) => /^(nodata|error)$/i.test(a.state))) return { state: "unknown", why: "Grafana reports no data for the rule", evaluated };
    return { state: "resolved", at: isNaN(evaluated) ? new Date() : evaluated, evaluated };
  }

  const clock = (d) => d.toLocaleTimeString(undefined, { timeStyle: "medium" });
  function paintLive(r, live, badge, stamp) {
    const cls = { firing: "p1 live", pending: "p3", resolved: "ok", unknown: "soft" }[live.state];
    const text = {
      firing: `Firing now${live.since && !isNaN(live.since) ? " · since " + live.since.toLocaleTimeString(undefined, { timeStyle: "short" }) : ""}`,
      pending: "Pending: the condition is true, waiting out the alert's time",
      resolved: `Resolved · ${live.cached ? "seen clear at " : "clear at "}${live.at ? live.at.toLocaleTimeString(undefined, { timeStyle: "short" }) : ""}`,
      unknown: "Status unknown" + (live.why ? " · " + live.why : ""),
    }[live.state];
    badge.className = "pill " + cls;
    badge.replaceChildren(el("span", "dot"), document.createTextNode(text));
    badge.title = "Asked Grafana at " + clock(new Date()) + ". The report itself is a snapshot from when it was written.";
    const asked = live.cached ? "Resolved earlier, not checked again" : "Last updated " + clock(new Date());
    const evald = live.evaluated && !isNaN(live.evaluated) ? " · rule last evaluated " + clock(live.evaluated) : "";
    const next = live.state === "resolved" ? "" : ` · checks again every ${every(r, live.state) / 1000} s (${level(r) || "no priority"})`;
    stamp.textContent = asked + evald + next;
  }

  // how often an open report is checked while it fires, is pending or its state is unknown: the higher the priority, the sooner a change shows.
  // Unknown (Grafana cannot be asked) backs off to twice the interval so a broken proxy is not hammered. Resolved reports are not checked at all.
  const EVERY = { P1: 10, P2: 30, P3: 60, P4: 120 };
  const every = (r, st) => (EVERY[level(r)] || 60) * (st === "unknown" ? 2 : 1) * 1000;

  function startLive(r, badge, stamp) {
    clearTimeout(state.liveTimer);
    const run = async () => {
      if (state.current !== r.id) return;
      const live = await liveStatus(r);
      if (state.current !== r.id) return;
      paintLive(r, live, badge, stamp);
      drawArchitecture(r, live.state);
      if (live.state === "resolved") { if (!live.cached) saveResolved(r.id, live.at.toISOString()); return; }   // resolved: no more checks
      state.liveTimer = setTimeout(run, every(r, live.state));
    };
    run();
  }

  // what the rules worked out beyond the cause: blast radius, change, confidence, competing causes, chain, trend, recurrence, related alerts
  function renderInsights(r) {
    const card = document.getElementById("insights-card"), body = document.getElementById("insights-body"), i = r.insights;
    body.replaceChildren();
    card.hidden = !i || !i.chain;
    if (card.hidden) return;
    const block = (title, items) => { if (!items || !items.length) return; const d = el("div", "ins"); d.append(el("b", "", title)); const ul = el("ul"); for (const t of items) ul.append(el("li", "", t)); d.append(ul); body.append(d); };
    block("What else is affected", i.blast_radius);
    block("Was it a change?", [i.change.note]);
    block("How sure is this", [words(i.confidence.level) + " confidence, evidence from " + (i.confidence.agreeing_sources.join(", ") || "nothing usable"), ...i.confidence.check_next]);
    block("Other possible causes", (i.alternatives || []).map((a) => (a.supported ? "Seen: " : "Not seen: ") + a.cause + " (" + a.how_to_tell + ")"));
    block("Dependency chain", [i.chain.map((h) => h.hop + (h.problem ? "  <- problem here" : "")).join("  >  ")]);
    block("How long and which way", ["Unhealthy for about " + i.trend.unhealthy_minutes + " min" + (i.trend.still_ongoing ? ", still ongoing" : ", no longer ongoing"), ...i.trend.lines]);
    block("Seen before", [i.recurrence.note]);
    block("Related alerts in the same window", (i.related || []).map((x) => x.alert + " on " + x.service + " (" + x.priority + "), " + Math.abs(x.minutes_apart) + " min " + (x.minutes_apart > 0 ? "after" : "before")));
    block("Confirming the fix", [i.verification.resolves_when]);
  }

  // the alert that started the investigation, as the agent recorded it (reports written before this have none)
  function renderAlertDetails(r) {
    const card = $("alert-card"), body = $("alert-body");
    const d = r.alert_details;
    body.replaceChildren();
    if (r.report_type === "capacity") { card.hidden = true; return; }
    card.hidden = false;
    if (!d || !d.labels) { body.append(el("p", "muted", "The details of the alert were not recorded for this report (it was written before the agent saved them).")); return; }
    const grid = el("dl", "facts");
    const add = (k, v) => { if (!v) return; const w = el("div"); w.append(el("dt", "", k), el("dd", "", v)); grid.append(w); };
    add("Alert", d.alertname); add("State when it fired", d.status); add("Severity", d.severity); add("Started", fmtTime(when(d.starts_at)));
    add("Cluster", d.cluster); add("Namespace", d.namespace);
    const inc = r.incident || {};
    add("Incident", inc.id); add("Investigation took", inc.took_seconds != null ? inc.took_seconds + " s" : ""); add("Analysis", words(inc.analysis_mode));
    body.append(grid);
    const a = d.annotations || {};
    for (const k of ["summary", "description"]) if (a[k]) { const p = el("p", "alert-ann"); p.append(el("b", "", k[0].toUpperCase() + k.slice(1) + ": "), document.createTextNode(a[k])); body.append(p); }
    const chips = el("div", "alert-labels");
    for (const [k, v] of Object.entries(d.labels).sort()) if (k !== "alertname") chips.append(el("span", "chip", k + "=" + v));
    const det = el("details"); det.append(el("summary", "", "All labels (" + Object.keys(d.labels).length + ")"), chips);
    body.append(det);
  }

  function drawArchitecture(r, live) {
    // resolved: two pictures, the problem as it was and the recovered state now; otherwise the one picture
    const { svg, analysis, nameOf } = RcaDiagram.draw(r, live === "resolved" ? "unknown" : live, live === "resolved");
    if (live === "resolved") {
      // the problem picture stays (animated, as it was); the recovered state is one green line under it, no second diagram
      const fig = el("figure", "arch-fig"); fig.append(el("figcaption", "", "When it happened: the problem and what it affected"), svg);
      $("arch").replaceChildren(fig, el("div", "arch-now", "✓ Now: the alert is no longer firing, everything is healthy."));
    } else {
      $("arch").replaceChildren(svg);
    }
    const note = $("arch-note");
    const where = (r.fault_domain && r.fault_domain.component) || (r.root_cause && r.root_cause.description) || nameOf(analysis.root);
    const hit = [...analysis.impacted].map(nameOf);
    const tail = live === "resolved" ? "The alert is no longer firing, so the picture shows the problem as it was. "
      : live === "firing" || live === "pending" ? "Live: the alert is " + (live === "firing" ? "still firing" : "pending") + ", so the problem is drawn as the report found it. "
      : "Grafana could not say whether the alert is still firing, so this is the state when the report was written. ";
    note.replaceChildren(el("b", "", live === "resolved" ? "The problem was: " : "Problem: "), document.createTextNode(`${nameOf(analysis.root)} (${where}). `), el("b", "", live === "resolved" ? "Was affected: " : "Affected: "),
      document.createTextNode(hit.length ? hit.join(", ") + ". " : "nothing else. "),
      document.createTextNode(tail + "The diagram is drawn from the alert, service and root cause of the report; green on a firing alert means the report found no problem there."));
  }

  function fact(dl, label, value) {
    if (!value) return;
    const d = el("div"); d.append(el("dt", "", label), el("dd", "", value)); dl.append(d);
  }

  function renderCapacityPlan(container, r) {
    const c = r.capacity || {};
    const remediation = r.remediation || {};
    const verification = c.post_merge || remediation.post_merge_verification || null;
    const checks = verification && Array.isArray(verification.checks) ? verification.checks : [];
    const status = verification && verification.status;
    const statusText = status === "healthy" ? "Healthy after change"
      : status === "needs_attention" ? "Follow-up needed"
      : status === "inconclusive" ? "Verification inconclusive"
      : status === "not_merged" ? "Waiting for merge"
      : remediation.merged ? "Verification pending" : "Plan recorded";
    const statusClass = status === "healthy" ? "healthy" : status === "needs_attention" ? "attention" : "pending";
    const section = (title, cls) => { const s = el("section", "capacity-panel " + (cls || "")); s.append(el("h3", "capacity-section-title", title)); return s; };
    const metric = (label, value, detail) => {
      const item = el("div", "capacity-metric");
      item.append(el("span", "capacity-metric-label", label), el("strong", "capacity-metric-value", value));
      if (detail) item.append(el("span", "capacity-metric-detail", detail));
      return item;
    };
    const vmetrics = verification && verification.metrics || {};
    const fmt = (value, digits = 2) => typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "—";
    const memMiB = typeof vmetrics.memory_p99_bytes === "number" ? vmetrics.memory_p99_bytes / 1048576 : null;
    const cpuMilli = typeof vmetrics.cpu_p95_cores === "number" ? vmetrics.cpu_p95_cores * 1000 : null;
    const expected = verification && verification.expected_replicas != null ? verification.expected_replicas : c.current_replicas;
    const available = vmetrics.available_replicas;
    const beforeMetrics = c.metrics || {};
    const changeText = String(c.proposed_change || "");
    const memoryQuantities = [...changeText.matchAll(/(\d+(?:\.\d+)?)(Mi|Gi)/g)].map((m) => Number(m[1]) * (m[2] === "Gi" ? 1024 : 1));
    const oldMemoryLimit = memoryQuantities.length > 1 ? memoryQuantities[0] : null;
    const newMemoryLimit = (verification && verification.expected_memory_limit_mib) || (memoryQuantities.length > 1 ? memoryQuantities[1] : null);
    const beforeMemMiB = typeof beforeMetrics.memory_p99_bytes === "number" ? beforeMetrics.memory_p99_bytes / 1048576 : null;
    const beforeMemPct = typeof c.memory_pct === "number" ? c.memory_pct : (beforeMemMiB != null && oldMemoryLimit ? beforeMemMiB / oldMemoryLimit * 100 : null);
    const afterMemPct = memMiB != null && newMemoryLimit ? memMiB / newMemoryLimit * 100 : null;

    const top = el("div", "capacity-plan-top");
    const overview = section("Capacity action", "capacity-action-panel");
    overview.append(el("p", "capacity-eyebrow", (c.namespace ? c.namespace + " / " : "") + (c.workload || r.service || "workload")));
    overview.append(el("p", "capacity-change", String(c.proposed_change || "No configuration change proposed").replace(/`/g, "")));
    overview.append(el("p", "capacity-reason", c.reason || r.summary || "Capacity signals reviewed."));
    const actionMeta = el("div", "capacity-action-meta");
    if (remediation.pr_url || c.pr_url) {
      const pr = el("a", "capacity-pr-link", "Pull request #" + (remediation.pr_number || c.pr_number || ""));
      pr.href = remediation.pr_url || c.pr_url; pr.target = "_blank"; pr.rel = "noopener";
      actionMeta.append(pr);
    }
    actionMeta.append(el("span", "capacity-status " + statusClass, statusText));
    overview.append(actionMeta);
    const result = section("Post-change result", "capacity-result-panel " + statusClass);
    result.append(el("span", "capacity-result-mark", status === "healthy" ? "✓" : status === "needs_attention" ? "!" : "·"));
    result.append(el("strong", "capacity-result-title", statusText));
    result.append(el("p", "capacity-result-copy", status === "healthy"
      ? "The proposed settings are active and the workload passed its " + (verification.window || "post-merge") + " health checks."
      : status === "needs_attention" ? "The change was applied, but one or more health checks need follow-up."
      : status === "not_merged" ? "The pull request has not merged, so post-change health has not been checked."
      : "Post-change health evidence is not yet sufficient to confirm the result."));
    if (verification && verification.checked_at) result.append(el("span", "capacity-checked-at", "Checked " + fmtTime(when(verification.checked_at))));
    top.append(overview, result);
    container.replaceChildren(top);

    if (verification && verification.metrics) {
      const health = section("Verification evidence", "capacity-health-panel");
      const grid = el("div", "capacity-metrics");
      grid.append(
        metric("CPU P95", cpuMilli == null ? "—" : fmt(cpuMilli, 1) + "m", "cores"),
        metric("CPU throttling", typeof vmetrics.cpu_throttle === "number" ? fmt(vmetrics.cpu_throttle * 100, 3) + "%" : "—", "throttled periods"),
        metric("Memory P99", memMiB == null ? "—" : fmt(memMiB, 1) + " MiB", "working set"),
        metric("Available replicas", (available == null ? "—" : fmt(available, 0)) + " / " + (expected == null ? "—" : expected), "available / expected"),
        metric("Restarts", vmetrics.restarts_in_window == null ? "—" : fmt(vmetrics.restarts_in_window, 0), "in " + (verification.window || "observation window")),
        metric("Metric coverage", fmt(vmetrics.cpu_coverage_seconds, 0) + "s / " + fmt(vmetrics.memory_coverage_seconds, 0) + "s", "CPU / memory")
      );
      health.append(grid);
      if (checks.length) {
        const list = el("ul", "capacity-check-list");
        checks.forEach((check) => {
          const row = el("li", check.passed ? "capacity-check passed" : "capacity-check failed");
          row.append(el("span", "capacity-check-icon", check.passed ? "✓" : "!"));
          const text = el("span", "capacity-check-text");
          text.append(el("strong", "", check.name || "Health check"), el("span", "", check.evidence || ""));
          row.append(text); list.append(row);
        });
        health.append(list);
      }
      container.append(health);
    }

    const delta = section("Before and after", "capacity-delta-panel");
    const deltaGrid = el("div", "capacity-delta-grid");
    const changeIsMemory = /memory limit/i.test(changeText) && oldMemoryLimit != null && newMemoryLimit != null;
    if (changeIsMemory) {
      const reduction = (1 - newMemoryLimit / oldMemoryLimit) * 100;
      deltaGrid.append(metric("Memory limit", oldMemoryLimit + " → " + newMemoryLimit + " MiB", reduction.toFixed(0) + "% lower per replica"));
      if (beforeMemMiB != null && memMiB != null) {
        const usedDelta = (memMiB - beforeMemMiB) / beforeMemMiB * 100;
        deltaGrid.append(metric("Memory P99", beforeMemMiB.toFixed(1) + " → " + memMiB.toFixed(1) + " MiB", (usedDelta <= 0 ? Math.abs(usedDelta).toFixed(1) + "% lower" : usedDelta.toFixed(1) + "% higher") + " observed"));
      }
    } else if (/CPU limit/i.test(changeText)) {
      const cpuQuantities = [...changeText.matchAll(/(\d+(?:\.\d+)?)m/g)].map((m) => Number(m[1]));
      if (cpuQuantities.length > 1) deltaGrid.append(metric("CPU limit", cpuQuantities[0] + " → " + cpuQuantities[1] + "m", (1 - cpuQuantities[1] / cpuQuantities[0] > 0 ? ((1 - cpuQuantities[1] / cpuQuantities[0]) * 100).toFixed(0) + "% lower per replica" : "limit change")));
    } else if (/replicas/i.test(changeText)) {
      const replicas = [...changeText.matchAll(/replicas\s+`?(\d+)`?\s*(?:→|->)\s*`?(\d+)/gi)].map((m) => [Number(m[1]), Number(m[2])]);
      if (replicas.length) deltaGrid.append(metric("Replica plan", replicas[0][0] + " → " + replicas[0][1], "configured floor retained"));
    }
    if (typeof beforeMetrics.cpu_p95_cores === "number" && typeof vmetrics.cpu_p95_cores === "number") {
      deltaGrid.append(metric("CPU P95", beforeMetrics.cpu_p95_cores.toFixed(4) + " → " + vmetrics.cpu_p95_cores.toFixed(4) + " cores", "before → after"));
    }
    if (typeof beforeMetrics.cpu_throttle === "number" && typeof vmetrics.cpu_throttle === "number") {
      deltaGrid.append(metric("CPU throttling", (beforeMetrics.cpu_throttle * 100).toFixed(3) + "% → " + (vmetrics.cpu_throttle * 100).toFixed(3) + "%", "before → after"));
    }
    if (available != null && expected != null) deltaGrid.append(metric("Availability", fmt(available, 0) + " / " + expected, "all expected replicas available"));
    if (deltaGrid.childElementCount) delta.append(deltaGrid);
    if (changeIsMemory && beforeMemPct != null && afterMemPct != null) {
      const bars = el("div", "capacity-compare-bars");
      [["Before", beforeMemPct, beforeMemMiB, oldMemoryLimit], ["After", afterMemPct, memMiB, newMemoryLimit]].forEach(([label, pct, used, limit]) => {
        const row = el("div", "capacity-compare-row");
        const topLine = el("div", "capacity-compare-label");
        topLine.append(el("strong", "", label), el("span", "", used.toFixed(1) + " / " + limit + " MiB · " + pct.toFixed(1) + "% used"));
        const track = el("div", "capacity-meter");
        const fill = el("span", "capacity-meter-fill"); fill.style.width = Math.max(0, Math.min(100, pct)) + "%";
        track.append(fill); row.append(topLine, track); bars.append(row);
      });
      const perPodDrop = oldMemoryLimit - newMemoryLimit;
      const totalCeilingDrop = expected == null ? null : perPodDrop * Number(expected);
      const note = "Memory limit reduced by " + perPodDrop + " MiB per pod" + (totalCeilingDrop == null ? "." : " (" + totalCeilingDrop + " MiB across " + expected + " replicas).") + " The 256Mi request and replica floor stayed unchanged. Scheduling still uses the request; this trims runtime memory headroom without claiming additional schedulable node capacity.";
      delta.append(bars, el("p", "capacity-delta-note", note));
    }
    if (delta.childElementCount) container.append(delta);

    const plan = section("Review basis", "capacity-basis-panel");
    const basis = el("dl", "capacity-basis");
    fact(basis, "Review decision", c.decision);
    fact(basis, "Replica floor", c.candidate_floor);
    fact(basis, "Review window", c.window);
    fact(basis, "Review metrics", c.metrics ? "Captured" : "Unavailable");
    plan.append(basis);
    container.append(plan);
  }

  async function show(id) {
    clearTimeout(state.liveTimer);
    state.current = id;
    document.querySelectorAll(".item").forEach((b) => (b.dataset.id === id ? b.setAttribute("aria-current", "true") : b.removeAttribute("aria-current")));
    $("app").classList.add("reading");
    $("empty").hidden = true; $("report").hidden = false;
    $("body").replaceChildren(el("p", "muted", "Loading…"));
    let r = meta.get(id);
    if (!r) { try { r = await getJson(`/${id}/report.json`); meta.set(id, r); } catch (_) { r = { id, alert: id }; } }
    let md = "";
    try { md = await getText(`/${id}/rca.md`); } catch (_) { md = r.rca_markdown || "This report has no rca.md."; }
    if (state.current !== id) return;

    const pills = $("pills"); pills.replaceChildren();
    const isCapacity = r.report_type === "capacity";
    const lv = level(r);
    if (lv) pills.append(pill(`${lv}${r.priority.respond_within ? " · respond " + r.priority.respond_within : ""}`, lv.toLowerCase()));
    if (isCapacity) {
      pills.append(pill("Capacity plan", "soft"));
      const priorityPill = pills.querySelector(".pill");
      if (priorityPill) priorityPill.remove();
    }
    const decision = r.capacity && r.capacity.decision;
    const postMergeStatus = isCapacity && r.capacity.post_merge && r.capacity.post_merge.status;
    const capacityBadge = postMergeStatus === "healthy" ? "Merged · healthy"
      : postMergeStatus === "needs_attention" ? "Merged · needs attention"
      : postMergeStatus === "inconclusive" ? "Merged · verification inconclusive"
      : postMergeStatus === "not_merged" ? "PR open · not verified"
      : decision === "proposal" ? "Proposal recorded" : "Review complete";
    const badge = pill(isCapacity ? capacityBadge : "Checking Grafana…", "soft");
    badge.id = "live-badge";
    if (isCapacity && postMergeStatus === "healthy") badge.classList.add("ok");
    if (isCapacity && postMergeStatus === "needs_attention") badge.classList.add("p1");
    const stamp = el("span", "stamp muted", "");
    pills.append(badge, stamp);
    if (isCapacity) {
      const floor = r.capacity.candidate_floor;
      stamp.textContent = `Decision: ${decision || "recorded"}${floor != null ? " · candidate floor " + floor : ""}`;
      badge.title = "Stored result of the capacity review";
    }
    if (r.severity && !isCapacity) pills.append(pill("alert severity " + r.severity, "soft"));
    if (r.mode && !isCapacity) pills.append(pill(words(r.mode), "soft"));
    $("title").textContent = isCapacity ? "Capacity plan for " + (r.service || "workload") : `${r.alert || id}${r.service ? " on " + r.service : ""}`;
    $("summary").textContent = isCapacity ? "Capacity review, change record, and post-change health evidence." : (r.summary || "");
    const facts = $("facts"); facts.replaceChildren();
    if (isCapacity) {
      fact(facts, "Cluster", r.capacity.cluster || "not recorded");
      fact(facts, "Namespace", r.capacity.namespace || "not recorded");
      fact(facts, "Workload", r.service || r.capacity.workload || "not recorded");
      fact(facts, "Decision", decision);
      fact(facts, "Replica floor", r.capacity.candidate_floor);
      fact(facts, "Reviewed", fmtTime(when(r.started_at)));
    } else {
      fact(facts, "Where", r.fault_domain && r.fault_domain.layer ? `${r.fault_domain.layer} level` : "");
      fact(facts, "Component", r.fault_domain && r.fault_domain.component);
      fact(facts, "Root cause", r.root_cause && r.root_cause.category ? `${words(r.root_cause.category)}${r.root_cause.confidence ? " (" + r.root_cause.confidence + " confidence)" : ""}` : "");
      fact(facts, "Started", fmtTime(when(r.started_at)));
      fact(facts, "Report written", fmtTime(when(r.generated_at)));
      fact(facts, "Investigation ID", id);
    }

    const actions = $("actions"); actions.replaceChildren();
    const link = (text, href) => { const a = el("a", "btn", text); a.href = href; a.target = "_blank"; a.rel = "noopener"; return a; };
    const copy = el("button", "btn", "Copy link"); copy.type = "button";
    copy.addEventListener("click", () => navigator.clipboard.writeText(location.href.split("#")[0] + "#" + id).then(() => toast("Link copied")));
    const print = el("button", "btn", "Print / PDF"); print.type = "button"; print.addEventListener("click", () => window.print());
    actions.append(copy, link(isCapacity ? "Capacity plan" : "Markdown", `/${id}/rca.md`), link(isCapacity ? "Report data" : "JSON", `/${id}/report.json`), print);

    const body = $("body");
    const toc = $("toc");
    if (isCapacity) {
      toc.replaceChildren(); toc.hidden = true;
      body.parentElement.classList.add("capacity-grid");
      renderCapacityPlan(body, r);
    }
    else {
      toc.hidden = false;
      body.parentElement.classList.remove("capacity-grid");
      body.innerHTML = DOMPurify.sanitize(marked.parse(md), { ADD_ATTR: ["target"] });
      enhance(body);
      buildToc(body);
    }
    renderInsights(r);
    renderAlertDetails(r);
    $("arch").closest(".arch-card").hidden = isCapacity;
    if (!isCapacity) {
      drawArchitecture(r, "unknown");
      startLive(r, badge, stamp);
    }
    document.title = `${r.alert || id} · reports`;
    $("main").scrollTo({ top: 0 });
    if (isCapacity) document.title = "Capacity plan | " + (r.service || "workload");
  }

  function route() {
    const id = decodeURIComponent(location.hash.slice(1));
    if (id && /^rca-[\w.-]+$/.test(id)) show(id);
    else { clearTimeout(state.liveTimer); state.current = null; $("app").classList.remove("reading"); $("report").hidden = true; $("empty").hidden = false; document.title = "SRE Reports"; renderList(); }
  }

  // ---- wiring ------------------------------------------------------------------------------------------------
  $("q").addEventListener("input", (e) => { state.query = e.target.value; renderList(); });
  $("report-type").addEventListener("change", (e) => { state.type = e.target.value; renderList(); });
  $("back").addEventListener("click", () => { location.hash = ""; });
  addEventListener("hashchange", route);
  addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT") { if (e.key === "Escape") e.target.blur(); return; }
    if (e.key === "/") { e.preventDefault(); $("q").focus(); return; }
    if (e.key !== "j" && e.key !== "k") return;
    const shown = visible(); const i = shown.findIndex((r) => r.id === state.current);
    const next = shown[Math.min(shown.length - 1, Math.max(0, i + (e.key === "j" ? 1 : -1)))];
    if (next) location.hash = next.id;
  });

  loadList().then(route).catch((e) => { $("count").textContent = "Could not load the reports"; $("list").replaceChildren(el("li", "none", String(e.message))); });
  setInterval(() => loadList().catch(() => {}), 30000);
})();
