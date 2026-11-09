/* RCA report viewer: a list of the reports on the volume and each one rendered from its rca.md.
   Data comes from the same origin: /_list/ (nginx directory listing as JSON), /<id>/report.json and /<id>/rca.md. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };

  const state = {
    reports: [], filter: new Set(), query: "", type: "all",
    current: null, loaded: false, liveTimer: null, live: {},
    dismissed: new Set(), showDismissed: false,
    read: new Set(), notifOpen: false,
  };
  const meta = new Map(); // id -> report.json

  // ---- persistence -------------------------------------------------------------------------------------------
  function loadPrefs() {
    try {
      const t = localStorage.getItem("rca-theme");
      if (t) document.documentElement.dataset.theme = t;
    } catch (_) {}
    try {
      const f = JSON.parse(localStorage.getItem("rca-filter") || "{}");
      if (f.type) state.type = f.type;
      if (f.query) { state.query = f.query; $("q").value = f.query; }
      if (Array.isArray(f.filter)) state.filter = new Set(f.filter);
    } catch (_) {}
    try {
      const d = JSON.parse(localStorage.getItem("rca-dismissed") || "[]");
      state.dismissed = new Set(d);
    } catch (_) {}
    // sync the active tab to the restored type
    document.querySelectorAll(".tab").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.type === state.type)));
    loadRead();
  }
  function saveFilter() {
    try { localStorage.setItem("rca-filter", JSON.stringify({ type: state.type, query: state.query, filter: [...state.filter] })); } catch (_) {}
  }
  function saveDismissed() {
    try { localStorage.setItem("rca-dismissed", JSON.stringify([...state.dismissed])); } catch (_) {}
  }
  function loadRead() {
    try { const r = JSON.parse(localStorage.getItem("rca-read") || "[]"); state.read = new Set(r); } catch (_) {}
  }
  function saveRead() {
    try { localStorage.setItem("rca-read", JSON.stringify([...state.read])); } catch (_) {}
  }
  function markRead(id) {
    if (!state.read.has(id)) { state.read.add(id); saveRead(); updateNotifBadge(); renderList(); }
  }
  function markAllRead() {
    state.reports.forEach((r) => state.read.add(r.id));
    saveRead(); updateNotifBadge(); renderList(); renderNotifList();
  }
  function updateNotifBadge() {
    const unread = state.reports.filter((r) => !state.read.has(r.id) && !state.dismissed.has(r.id) && r.report_type !== "capacity").length;
    const badge = $("notif-badge");
    if (badge) { badge.hidden = unread === 0; badge.textContent = unread > 9 ? "9+" : unread; }
  }

  // ---- theme -------------------------------------------------------------------------------------------------
  $("theme").addEventListener("click", () => {
    const root = document.documentElement;
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("rca-theme", root.dataset.theme); } catch (_) {}
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
  function toast(text, undoFn) {
    const t = el("div", "toast");
    t.textContent = text;
    if (undoFn) {
      const u = el("button", "", " Undo");
      u.style.cssText = "border:0;background:none;color:inherit;cursor:pointer;text-decoration:underline;margin-left:6px;font:inherit;";
      u.addEventListener("click", () => { undoFn(); t.remove(); });
      t.append(u);
    }
    document.body.append(t);
    setTimeout(() => t.remove(), undoFn ? 4000 : 1600);
  }
  const getJson = async (url) => { const r = await fetch(url, { cache: "no-store" }); if (!r.ok) throw new Error(`${url}: ${r.status}`); return r.json(); };
  const getText = async (url) => { const r = await fetch(url, { cache: "no-store" }); if (!r.ok) throw new Error(`${url}: ${r.status}`); return r.text(); };
  const reportType = (r) => {
    if (r.report_type !== "capacity") return "incident";
    if (r.capacity && r.capacity.review_type === "weekly_floor") return "weekly_floor";
    return "capacity";
  };

  // ---- tab counts --------------------------------------------------------------------------------------------
  function updateTabCounts() {
    const active = state.reports.filter((r) => !state.dismissed.has(r.id));
    const counts = { all: active.length, incident: 0, capacity: 0, weekly_floor: 0 };
    for (const r of active) counts[reportType(r)] = (counts[reportType(r)] || 0) + 1;
    for (const [type, n] of Object.entries(counts)) {
      const ct = $("tc-" + type);
      if (ct) ct.textContent = n > 0 ? n : "";
    }
  }

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
    updateTabCounts();
    updateNotifBadge();
    renderList();
    renderNotifList();
  }

  function renderChips() {
    const box = $("chips");
    const present = [...new Set(state.reports.map(level).filter(Boolean))].sort();
    box.replaceChildren(...present.map((p) => {
      const b = el("button", "chip", p);
      b.setAttribute("aria-pressed", String(state.filter.has(p)));
      b.addEventListener("click", () => {
        state.filter.has(p) ? state.filter.delete(p) : state.filter.add(p);
        renderChips(); renderList(); saveFilter();
      });
      return b;
    }));
  }

  const visible = () => {
    const q = state.query.trim().toLowerCase();
    return state.reports.filter((r) => {
      if (!state.showDismissed && state.dismissed.has(r.id)) return false;
      if (state.filter.size && !state.filter.has(level(r))) return false;
      const rt = reportType(r);
      if (state.type === "incident" && rt !== "incident") return false;
      if (state.type === "capacity" && rt !== "capacity") return false;
      if (state.type === "weekly_floor" && rt !== "weekly_floor") return false;
      if (!q) return true;
      return [r.id, r.alert, r.service, r.summary, r.report_type, r.capacity && r.capacity.decision,
        r.root_cause && r.root_cause.category, r.fault_domain && r.fault_domain.layer, level(r)].join(" ").toLowerCase().includes(q);
    });
  };

  function renderList() {
    const shown = visible();
    const total = state.reports.filter((r) => !state.dismissed.has(r.id)).length;
    const dismissedCount = state.dismissed.size;
    $("count").textContent = `${total} report${total === 1 ? "" : "s"}${shown.length !== total ? `, ${shown.length} shown` : ""}`;

    // dismissed toggle
    const dtoggle = $("dismissed-toggle");
    if (dismissedCount > 0) {
      dtoggle.hidden = false;
      dtoggle.textContent = state.showDismissed
        ? `Hide ${dismissedCount} dismissed`
        : `${dismissedCount} dismissed — show`;
    } else {
      dtoggle.hidden = true;
    }

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
      if (level(r)) b.dataset.sev = level(r).toLowerCase();
      const liveState = state.live[r.id];
      if (liveState === "firing") b.dataset.live = "firing";
      else if (liveState === "resolved") b.dataset.live = "resolved";
      if (!state.read.has(r.id) && r.report_type !== "capacity") b.dataset.unread = level(r).toLowerCase() || "p4";
      if (state.current === r.id) b.setAttribute("aria-current", "true");
      if (state.dismissed.has(r.id)) b.classList.add("is-dismissed");
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
      const capacityTitle = capacityLabel(r);
      top.append(el("span", "item-title", isCapacity ? capacityTitle + " · " + (r.service || "workload") : (r.alert || r.id)));
      if (isCapacity) {
        top.append(pill(capacityTitle, "soft"));
        if (verificationStatus) {
          const label2 = verificationStatus === "healthy" ? "Healthy"
            : verificationStatus === "needs_attention" ? "Follow-up"
            : verificationStatus === "not_merged" ? "PR open" : "Pending";
          top.append(pill(label2, verificationStatus === "healthy" ? "ok" : verificationStatus === "needs_attention" ? "p1" : "soft"));
        }
      }
      const done = state.live[r.id] === "resolved";
      if (done) b.classList.add("resolved");
      if (level(r)) top.append(pill(done ? level(r) + " ✓" : level(r), done ? "ok big" : level(r).toLowerCase() + " big"));
      if (r.insights?.change?.likely) {
        const ch = r.insights.change;
        const minBefore = ch.minutes_before_alert != null ? `${ch.minutes_before_alert}m before alert` : (ch.revision || "").slice(0, 8);
        const dp = pill("Deploy · " + minBefore, "deploy");
        const files = (ch.files || []).join(", ");
        dp.title = (ch.note || "") + (files ? "\n\nChanged: " + files : "");
        top.append(dp);
      }
      if (isCapacity) {
        const priority = top.querySelector(".p4"); if (priority) priority.remove();
        if (capacity.review_type === "weekly_floor") top.append(pill("Weekly floor", "ok"));
        const action = (capacity.proposed_change || capacity.reason || r.summary || "No change proposed").replace(/`/g, "");
        b.append(top, el("div", "item-sub item-action", action), el("div", "item-sub", "ID: " + r.id));
      } else b.append(top, el("div", "item-sub", "ID: " + r.id + (r.service ? " - " + r.service : "")));
      const m = el("div", "item-meta");
      m.append(el("span", "", ago(d)));
      if (isCapacity) {
        if (capacity.candidate_floor != null) m.append(el("span", "", (capacity.review_type === "weekly_floor" ? "candidate " : "floor ") + capacity.candidate_floor));
        if (verificationStatus === "healthy") m.append(el("span", "", "post-change checks passed"));
        else if (verificationStatus === "needs_attention") m.append(el("span", "", "check results need review"));
        else if (capacity.review_type === "weekly_floor") m.append(el("span", "", "recommendation only"));
        else if (!verificationStatus && capacity.decision) m.append(el("span", "", words(capacity.decision)));
      }
      if (r.root_cause && r.root_cause.category) m.append(el("span", "", "· " + words(r.root_cause.category)));
      b.append(m);
      b.addEventListener("click", () => { location.hash = r.id; });

      // dismiss button
      const dismiss = el("button", "item-dismiss", "×");
      dismiss.type = "button";
      dismiss.title = state.dismissed.has(r.id) ? "Restore report" : "Dismiss report";
      dismiss.setAttribute("aria-label", state.dismissed.has(r.id) ? "Restore report" : "Dismiss report");
      dismiss.addEventListener("click", (e) => {
        e.stopPropagation();
        const wasDismissed = state.dismissed.has(r.id);
        if (wasDismissed) {
          state.dismissed.delete(r.id);
          toast("Report restored");
        } else {
          state.dismissed.add(r.id);
          if (state.current === r.id) location.hash = "";
          toast("Report dismissed", () => { state.dismissed.delete(r.id); saveDismissed(); updateTabCounts(); renderList(); });
        }
        saveDismissed(); updateTabCounts(); renderList();
      });
      b.append(dismiss);
      li.append(b);
      list.append(li);
    }
  }

  // ---- one report --------------------------------------------------------------------------------------------
  marked.setOptions({ gfm: true, breaks: false });

  function enhance(body) {
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
      if (/^evidence/i.test(h.textContent)) {
        const nodes = []; let n = h.nextElementSibling;
        while (n && n.tagName !== "H2") { nodes.push(n); n = n.nextElementSibling; }
        if (nodes.length) { const d = el("details"); const s = el("summary", "", `Evidence (${nodes.filter((x) => x.tagName === "UL").reduce((a, u) => a + u.children.length, 0) || nodes.length} items)`); d.append(s, ...nodes); h.after(d); }
      }
    }
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

  // ---- live status -------------------------------------------------------------------------------------------
  const RESOLVED_KEY = (id) => "rca-resolved:" + id;
  function savedResolved(id) { try { return localStorage.getItem(RESOLVED_KEY(id)); } catch (_) { return null; } }
  function saveResolved(id, at) { try { localStorage.setItem(RESOLVED_KEY(id), at); } catch (_) {} }

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
    // incident firing banner — sticky at top of main, unmissable for P1/P2
    let banner = $("incident-banner");
    if (!banner) { banner = el("div", "incident-banner"); banner.id = "incident-banner"; banner.hidden = true; $("main").prepend(banner); }
    const rh = $("report")?.querySelector(".report-head");
    if (rh) rh.dataset.live = live.state;
    if (live.state === "firing" || live.state === "pending") {
      const lv = level(r);
      const since = live.since && !isNaN(live.since) ? " since " + live.since.toLocaleTimeString(undefined, { timeStyle: "short" }) : "";
      banner.className = "incident-banner " + (lv.toLowerCase() || "p3");
      banner.hidden = false;
      const closeBtn = el("button", "banner-dismiss", "×");
      closeBtn.addEventListener("click", () => { banner.hidden = true; });
      banner.replaceChildren(el("span", "banner-pulse"), el("strong", "", live.state === "firing" ? "FIRING" : "PENDING"), el("span", "banner-sep", "·"), el("span", "", r.alert || r.id), ...(lv ? [el("span", "banner-pill", lv)] : []), ...(since ? [el("span", "banner-since", since)] : []), closeBtn);
    } else {
      banner.hidden = true;
    }
  }

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
      if (live.state === "resolved") { if (!live.cached) saveResolved(r.id, live.at.toISOString()); return; }
      state.liveTimer = setTimeout(run, every(r, live.state));
    };
    run();
  }

  function renderInsights(r) {
    const card = document.getElementById("insights-card"), body = document.getElementById("insights-body"), i = r.insights;
    body.replaceChildren();
    card.hidden = !i || !i.chain;
    if (card.hidden) return;
    const INS_ICONS = { "What else is affected": "💥", "Was it a change?": "🔀", "How sure is this": "🎯", "Other possible causes": "🔎", "Dependency chain": "🔗", "How long and which way": "⏱", "Seen before": "🔁", "Related alerts in the same window": "🔔", "Confirming the fix": "✅" };
    const grid = el("div", "insights-grid");
    body.append(grid);
    const block = (title, items) => {
      if (!items || !items.length) return;
      const d = el("div", "ins");
      const head = el("div", "ins-head");
      head.append(el("span", "ins-icon", INS_ICONS[title] || "·"), el("span", "ins-title", title));
      d.append(head);
      const ul = el("ul");
      for (const t of items) ul.append(el("li", "", t));
      d.append(ul);
      grid.append(d);
    };
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

  function renderAlertDetails(r) {
    const card = $("alert-card"), body = $("alert-body");
    const d = r.alert_details;
    body.replaceChildren();
    if (r.report_type === "capacity") { card.hidden = true; return; }
    card.hidden = false;
    if (!d || !d.labels) { body.append(el("p", "muted", "The details of the alert were not recorded for this report (it was written before the agent saved them).")); return; }
    const grid = el("dl", "facts-grid");
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
    const { svg, analysis, nameOf } = RcaDiagram.draw(r, live === "resolved" ? "unknown" : live, live === "resolved");
    if (live === "resolved") {
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

  function capacityLabel(r) {
    const c = (r && r.capacity) || {};
    if (c.review_type === "weekly_floor") return "Floor review";
    if (c.decision !== "proposal") return "Capacity review";
    const ch = String(c.proposed_change || "");
    if (/memory limit/i.test(ch)) return "Memory tuning";
    if (/cpu limit/i.test(ch)) return "CPU tuning";
    return "Scale-down plan";
  }

  function capacityActionOutcome(c) {
    if (c.review_type === "weekly_floor") return "Recommendation only; no scaling change was applied";
    const verification = c.post_merge || {};
    if (c.merged === true) {
      if (verification.status === "healthy") return "Applied and verified healthy";
      if (verification.status === "needs_attention") return "Applied; follow-up required";
      if (verification.status === "inconclusive") return "Applied; verification inconclusive";
      return "Applied; verification pending";
    }
    if (c.decision === "proposal") return "Proposed; awaiting merge";
    if (c.decision === "error") return "Review failed";
    return "No change proposed";
  }

  function renderCapacityPlan(container, r) {
    const c = r.capacity || {};
    const remediation = r.remediation || {};
    const verification = c.post_merge || remediation.post_merge_verification || null;
    const weeklyFloor = c.review_type === "weekly_floor";
    const checks = verification && Array.isArray(verification.checks) ? verification.checks : [];
    const status = verification && verification.status;
    const statusText = weeklyFloor ? "Weekly recommendation; no action applied"
      : status === "healthy" ? "Healthy after change"
      : status === "needs_attention" ? "Follow-up needed"
      : status === "inconclusive" ? "Verification inconclusive"
      : status === "not_merged" ? "Waiting for merge"
      : remediation.merged ? "Verification pending" : "Plan recorded";
    const statusClass = status === "healthy" || (weeklyFloor && c.decision === "recommendation") ? "healthy" : status === "needs_attention" ? "attention" : "pending";
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
    const actionTitle = weeklyFloor ? "Weekly replica-floor recommendation"
      : /memory limit/i.test(changeText) ? "Memory limit reduction"
      : /cpu limit/i.test(changeText) ? "CPU limit reduction"
      : "Replica scale-down";
    const overview = section(actionTitle, "capacity-action-panel");
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
    const result = section(weeklyFloor ? "Weekly review outcome" : "Post-change result", "capacity-result-panel " + statusClass);
    result.append(el("span", "capacity-result-mark", status === "healthy" ? "✓" : status === "needs_attention" ? "!" : "·"));
    result.append(el("strong", "capacity-result-title", statusText));
    result.append(el("p", "capacity-result-copy", weeklyFloor
      ? "This is a read-only recommendation based on the weekly telemetry window. It did not change replicas, edit Git, or create a pull request."
      : status === "healthy"
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
    fact(basis, "Action outcome", capacityActionOutcome(c));
    fact(basis, "Review decision", c.decision);
    fact(basis, "Review type", weeklyFloor ? "Weekly replica-floor review"
      : /memory limit/i.test(changeText) ? "Daily resource review (memory)"
      : /cpu limit/i.test(changeText) ? "Daily resource review (CPU)"
      : "Daily scale-down review");
    if (weeklyFloor) fact(basis, "Configured minimum", c.configured_floor);
    fact(basis, weeklyFloor ? "Candidate replica count" : "Replica floor", c.candidate_floor);
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
      pills.append(pill(capacityLabel(r), "soft"));
      const priorityPill = pills.querySelector(".pill");
      if (priorityPill) priorityPill.remove();
    }
    const decision = r.capacity && r.capacity.decision;
    const postMergeStatus = isCapacity && r.capacity.post_merge && r.capacity.post_merge.status;
    const capacityBadge = isCapacity && r.capacity.review_type === "weekly_floor" ? "Weekly floor review · recommendation only"
      : postMergeStatus === "healthy" ? "Merged · healthy"
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
      stamp.textContent = `Action outcome: ${capacityActionOutcome(r.capacity)}${floor != null ? " | candidate floor " + floor : ""}`;
      badge.title = "Stored result of the capacity review";
    }
    if (r.severity && !isCapacity) pills.append(pill("alert severity " + r.severity, "soft"));
    if (r.mode && !isCapacity) pills.append(pill(words(r.mode), "soft"));
    if (r.insights?.change?.likely && !isCapacity) {
      const ch = r.insights.change;
      const minBefore = ch.minutes_before_alert != null ? `${ch.minutes_before_alert} min before alert` : (ch.revision || "").slice(0, 8);
      const files = (ch.files || []).join(", ");
      const dp = pill("Deploy · " + minBefore, "deploy");
      dp.title = (ch.note || "") + (files ? "\n\nChanged: " + files : "");
      pills.append(dp);
    }
    $("title").textContent = isCapacity ? (r.capacity.review_type === "weekly_floor" ? "Weekly replica-floor review for " : capacityLabel(r) + " for ") + (r.service || "workload") : `${r.alert || id}${r.service ? " on " + r.service : ""}`;
    $("summary").textContent = isCapacity ? (r.capacity.review_type === "weekly_floor" ? "Weekly service floor recommendation with telemetry coverage and availability evidence. No scaling action is applied." : "Capacity review, change record, and post-change health evidence.") : (r.summary || "");
    const facts = $("facts"); facts.replaceChildren();
    if (isCapacity) {
      fact(facts, "Cluster", r.capacity.cluster || "not recorded");
      fact(facts, "Namespace", r.capacity.namespace || "not recorded");
      fact(facts, "Workload", r.service || r.capacity.workload || "not recorded");
      fact(facts, "Action outcome", capacityActionOutcome(r.capacity));
      fact(facts, "Review type", r.capacity.review_type === "weekly_floor" ? "Weekly replica-floor review" : "Daily scale-down review");
      fact(facts, "Review decision", decision);
      fact(facts, r.capacity.review_type === "weekly_floor" ? "Configured minimum" : "Replica floor",
        r.capacity.review_type === "weekly_floor" ? r.capacity.configured_floor : r.capacity.candidate_floor);
      fact(facts, "Reviewed", fmtTime(when(r.started_at)));
    } else {
      fact(facts, "Where", r.fault_domain && r.fault_domain.layer ? `${r.fault_domain.layer} level` : "");
      fact(facts, "Component", r.fault_domain && r.fault_domain.component);
      fact(facts, "Root cause", r.root_cause && r.root_cause.category ? `${words(r.root_cause.category)}${r.root_cause.confidence ? " (" + r.root_cause.confidence + " confidence)" : ""}` : "");
      fact(facts, "Started", fmtTime(when(r.started_at)));
      fact(facts, "Report written", fmtTime(when(r.generated_at)));
      fact(facts, "Investigation ID", id);
      // root cause callout — prominent above-fold anchor for incident responders
      const oldCallout = $("report").querySelector(".root-cause-callout");
      if (oldCallout) oldCallout.remove();
      if (!isCapacity && r.root_cause && r.root_cause.category) {
        const callout = el("div", "root-cause-callout");
        const icon = el("span", "rc-icon", "🔍");
        const content = el("div");
        content.append(el("span", "rc-label", "Root cause"), el("p", "rc-text", words(r.root_cause.category) + (r.root_cause.confidence ? " · " + r.root_cause.confidence + " confidence" : "")));
        if (r.root_cause.description) content.append(el("p", "rc-desc", r.root_cause.description));
        callout.append(icon, content);
        facts.after(callout);
      }
    }

    const actions = $("actions"); actions.replaceChildren();
    const link = (text, href) => { const a = el("a", "btn", text); a.href = href; a.target = "_blank"; a.rel = "noopener"; return a; };
    const copy = el("button", "btn btn-primary", "Copy link"); copy.type = "button";
    copy.addEventListener("click", () => navigator.clipboard.writeText(location.href.split("#")[0] + "#" + id).then(() => toast("Link copied")));
    const print = el("button", "btn", "Print / PDF"); print.type = "button"; print.addEventListener("click", () => window.print());
    // dismiss from report view
    const isDismissed = state.dismissed.has(id);
    const dimBtn = el("button", "btn" + (isDismissed ? "" : " btn-danger"), isDismissed ? "Restore" : "Dismiss");
    dimBtn.type = "button";
    dimBtn.addEventListener("click", () => {
      if (state.dismissed.has(id)) {
        state.dismissed.delete(id);
        toast("Report restored");
      } else {
        state.dismissed.add(id);
        toast("Report dismissed", () => { state.dismissed.delete(id); saveDismissed(); updateTabCounts(); renderList(); });
      }
      saveDismissed(); updateTabCounts(); renderList();
      // re-render action bar to reflect new state
      show(id);
    });
    actions.append(copy, link(isCapacity ? "Capacity plan" : "Markdown", `/${id}/rca.md`), link(isCapacity ? "Report data" : "JSON", `/${id}/report.json`), print, dimBtn);

    const body = $("body");
    const toc = $("toc");
    if (isCapacity) {
      toc.replaceChildren(); toc.hidden = true;
      body.parentElement.classList.add("capacity-grid");
      renderCapacityPlan(body, r);
    } else {
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
    const rHead = $("report").querySelector(".report-head");
    if (rHead) { rHead.dataset.sev = isCapacity ? "capacity" : (level(r).toLowerCase() || "none"); delete rHead.dataset.live; }
    markRead(id);
    renderNotifList();
    // remove stale firing banner when navigating to a new report
    const staleBanner = $("incident-banner");
    if (staleBanner) staleBanner.hidden = true;
    $("main").scrollTo({ top: 0 });
    if (isCapacity) document.title = capacityLabel(r) + " | " + (r.service || "workload");
  }

  function route() {
    const id = decodeURIComponent(location.hash.slice(1));
    if (id && /^rca-[\w.-]+$/.test(id)) show(id);
    else { clearTimeout(state.liveTimer); state.current = null; $("app").classList.remove("reading"); $("report").hidden = true; $("empty").hidden = false; document.title = "SRE Reports"; renderList(); }
  }

  // ---- notification panel ------------------------------------------------------------------------------------
  function renderNotifList() {
    const list = $("notif-list");
    if (!list) return;
    const sorted = [...state.reports]
      .filter((r) => r.report_type !== "capacity" && !state.dismissed.has(r.id))
      .sort((a, b) => String(b.started_at || b.id).localeCompare(String(a.started_at || a.id)));
    if (!sorted.length) { list.replaceChildren(el("li", "notif-empty", "No reports yet.")); return; }
    list.replaceChildren(...sorted.map((r) => {
      const li = el("li", "notif-item" + (state.read.has(r.id) ? "" : " unread"));
      const dot = el("span", "notif-dot");
      const body = el("div", "notif-body");
      body.append(el("span", "notif-name", r.alert || r.id));
      const metaRow = el("div", "notif-meta");
      const d = when(r.started_at || r.generated_at);
      metaRow.append(el("span", "", ago(d)));
      if (level(r)) metaRow.append(pill(level(r), level(r).toLowerCase()));
      if (r.root_cause && r.root_cause.category) metaRow.append(el("span", "", words(r.root_cause.category)));
      body.append(metaRow);
      li.append(dot, body);
      li.addEventListener("click", () => { closeNotif(); location.hash = r.id; });
      return li;
    }));
  }

  function openNotif() {
    let panel = $("notif-panel");
    if (!panel) {
      panel = el("div", "notif-panel"); panel.id = "notif-panel";
      const head = el("div", "notif-head");
      const title = el("span", "notif-head-title");
      title.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg> Notifications`;
      const markAll = el("button", "notif-mark-all", "Mark all read");
      markAll.addEventListener("click", markAllRead);
      head.append(title, markAll);
      const list = el("ul", "notif-list"); list.id = "notif-list";
      panel.append(head, list);
      document.body.append(panel);
      renderNotifList();
    }
    panel.hidden = false;
    state.notifOpen = true;
    $("notif-btn").setAttribute("aria-expanded", "true");
  }

  function closeNotif() {
    const panel = $("notif-panel");
    if (panel) panel.hidden = true;
    state.notifOpen = false;
    $("notif-btn").setAttribute("aria-expanded", "false");
  }

  $("notif-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    state.notifOpen ? closeNotif() : openNotif();
  });
  document.addEventListener("click", (e) => {
    if (state.notifOpen && !e.target.closest("#notif-panel") && !e.target.closest("#notif-btn")) closeNotif();
  });

  // ---- wiring ------------------------------------------------------------------------------------------------
  $("q").addEventListener("input", (e) => { state.query = e.target.value; renderList(); saveFilter(); });
  $("back").addEventListener("click", () => { location.hash = ""; });
  $("dismissed-toggle").addEventListener("click", () => { state.showDismissed = !state.showDismissed; renderList(); });

  // type tabs
  $("type-tabs").addEventListener("click", (e) => {
    const tab = e.target.closest(".tab");
    if (!tab) return;
    state.type = tab.dataset.type;
    document.querySelectorAll(".tab").forEach((b) => b.setAttribute("aria-selected", String(b === tab)));
    renderList(); saveFilter();
  });

  addEventListener("hashchange", route);
  addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT") { if (e.key === "Escape") e.target.blur(); return; }
    if (e.key === "/") { e.preventDefault(); $("q").focus(); return; }
    if (e.key !== "j" && e.key !== "k") return;
    const shown = visible(); const i = shown.findIndex((r) => r.id === state.current);
    const next = shown[Math.min(shown.length - 1, Math.max(0, i + (e.key === "j" ? 1 : -1)))];
    if (next) location.hash = next.id;
  });

  loadPrefs();
  loadList().then(route).catch((e) => { $("count").textContent = "Could not load the reports"; $("list").replaceChildren(el("li", "none", String(e.message))); });
  setInterval(() => loadList().catch(() => {}), 30000);
})();
