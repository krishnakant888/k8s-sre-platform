/* RCA report viewer: a list of the reports on the volume and each one rendered from its rca.md.
   Data comes from the same origin: /_list/ (nginx directory listing as JSON), /<id>/report.json and /<id>/rca.md. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };

  const state = { reports: [], filter: new Set(), query: "", current: null, loaded: false, liveTimer: null, live: {} };
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
      if (!q) return true;
      return [r.id, r.alert, r.service, r.summary, r.root_cause && r.root_cause.category, r.fault_domain && r.fault_domain.layer, level(r)].join(" ").toLowerCase().includes(q);
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
      top.append(el("span", "item-title", r.alert || r.id));
      const done = state.live[r.id] === "resolved";
      if (done) b.classList.add("resolved");
      if (level(r)) top.append(pill(done ? level(r) + " ✓" : level(r), done ? "ok big" : level(r).toLowerCase() + " big"));
      b.append(top, el("div", "item-sub", r.service || r.id));
      const m = el("div", "item-meta");
      m.append(el("span", "", ago(d)));
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
    const lv = level(r);
    if (lv) pills.append(pill(`${lv}${r.priority.respond_within ? " · respond " + r.priority.respond_within : ""}`, lv.toLowerCase()));
    const badge = pill("Checking Grafana…", "soft");
    badge.id = "live-badge";
    const stamp = el("span", "stamp muted", "");
    pills.append(badge, stamp);
    if (r.severity) pills.append(pill("alert severity " + r.severity, "soft"));
    if (r.mode) pills.append(pill(words(r.mode), "soft"));
    $("title").textContent = `${r.alert || id}${r.service ? " on " + r.service : ""}`;
    $("summary").textContent = r.summary || "";
    const facts = $("facts"); facts.replaceChildren();
    fact(facts, "Where", r.fault_domain && r.fault_domain.layer ? `${r.fault_domain.layer} level` : "");
    fact(facts, "Component", r.fault_domain && r.fault_domain.component);
    fact(facts, "Root cause", r.root_cause && r.root_cause.category ? `${words(r.root_cause.category)}${r.root_cause.confidence ? " (" + r.root_cause.confidence + " confidence)" : ""}` : "");
    fact(facts, "Started", fmtTime(when(r.started_at)));
    fact(facts, "Report written", fmtTime(when(r.generated_at)));
    fact(facts, "Report id", id);

    const actions = $("actions"); actions.replaceChildren();
    const link = (text, href) => { const a = el("a", "btn", text); a.href = href; a.target = "_blank"; a.rel = "noopener"; return a; };
    const copy = el("button", "btn", "Copy link"); copy.type = "button";
    copy.addEventListener("click", () => navigator.clipboard.writeText(location.href.split("#")[0] + "#" + id).then(() => toast("Link copied")));
    const print = el("button", "btn", "Print / PDF"); print.type = "button"; print.addEventListener("click", () => window.print());
    actions.append(copy, link("Markdown", `/${id}/rca.md`), link("JSON", `/${id}/report.json`), print);

    const body = $("body");
    body.innerHTML = DOMPurify.sanitize(marked.parse(md), { ADD_ATTR: ["target"] });
    enhance(body);
    buildToc(body);
    drawArchitecture(r, "unknown");
    startLive(r, badge, stamp);
    document.title = `${r.alert || id} · RCA reports`;
    $("main").scrollTo({ top: 0 });
  }

  function route() {
    const id = decodeURIComponent(location.hash.slice(1));
    if (id && /^rca-[\w.-]+$/.test(id)) show(id);
    else { clearTimeout(state.liveTimer); state.current = null; $("app").classList.remove("reading"); $("report").hidden = true; $("empty").hidden = false; document.title = "RCA reports"; renderList(); }
  }

  // ---- wiring ------------------------------------------------------------------------------------------------
  $("q").addEventListener("input", (e) => { state.query = e.target.value; renderList(); });
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
