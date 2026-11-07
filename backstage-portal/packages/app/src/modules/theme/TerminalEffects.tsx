import { useEffect } from 'react';

/**
 * The template run page (/create/tasks/<id>) as a pipeline in a terminal: the stepper flows and glows, the log is a dark terminal whose lines type in,
 * coloured by what they say (✓ done, ▶ running, … waiting, ✗ failed), with a blinking cursor on the last line. The log is plain text from the scaffolder,
 * so the lines are recognised by their marker and styled; nothing about the data changes.
 */

const MARK = /([✓✗▶…–])\s+(.*)$/;

type Kind = 'ok' | 'fail' | 'run' | 'wait' | 'skip' | 'stage' | 'err' | 'info';

function kindOf(text: string): { kind: Kind; name: string } | undefined {
  if (text.includes('━━')) return { kind: 'stage', name: text };
  const m = MARK.exec(text);
  if (m) {
    const kind: Kind = m[1] === '✓' ? 'ok' : m[1] === '✗' ? 'fail' : m[1] === '▶' ? 'run' : m[1] === '–' ? 'skip' : 'wait';
    return { kind, name: m[2].replace(/\s*\((success|failure|skipped|cancelled)\)\s*$/, '').trim() };
  }
  if (/\b(error|Error:)/.test(text) && /\d{4}-\d\d-\d\dT/.test(text)) return { kind: 'err', name: text };
  if (/\d{4}-\d\d-\d\dT[\d:.]+Z/.test(text)) return { kind: 'info', name: text };
  return undefined;
}

const scrollParent = (el: HTMLElement | null): HTMLElement | undefined => {
  for (let n = el; n && n !== document.body; n = n.parentElement) {
    const o = getComputedStyle(n).overflowY;
    if ((o === 'auto' || o === 'scroll') && n.clientHeight > 80) return n;
  }
  return undefined;
};

// the milestones of a request, in order, with the time each normally takes in seconds (measured on real runs): the progress bar and the estimate
// are the share of this plan that the log shows as done. The Argo CD pick-up is the long one: it checks Git every few minutes.
const PLAN: Array<{ key: string; re: RegExp; secs: number }> = [
  { key: 'ask', re: /Asking .* to run/, secs: 1 },
  { key: 'setup', re: /✓\s+Set up job/, secs: 3 },
  { key: 'checkout', re: /✓\s+Run actions\/checkout@v4 \(/, secs: 2 },
  { key: 'pip', re: /✓\s+Run pip install/, secs: 8 },
  { key: 'make', re: /✓\s+Make the change/, secs: 2 },
  { key: 'check', re: /✓\s+Check the files/, secs: 1 },
  { key: 'pr', re: /✓\s+Open the pull request \(/, secs: 4 },
  { key: 'lint', re: /✓\s+Run the checks on the pull request/, secs: 22 },
  { key: 'merge', re: /✓\s+Merge the pull request \(/, secs: 3 },
  { key: 'job', re: /Job "change": success/, secs: 2 },
  { key: 'merged', re: /✓ Merged as/, secs: 8 },
  { key: 'seen', re: /Argo CD has the commit/, secs: 95 },
  { key: 'synced', re: /✓ Synced/, secs: 8 },
  { key: 'deployed', re: /✓ Deployed/, secs: 7 },
];
const PLAN_TOTAL = PLAN.reduce((a, m) => a + m.secs, 0);

const SPIN = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];
const taskIdOf = () => /\/create\/tasks\/([^/?#]+)/.exec(location.pathname)?.[1];

export function TerminalEffects() {
  useEffect(() => {
    const seen = new WeakSet<Element>();
    const opened = new Set<string>();
    let raf = 0;
    let bannerSince = 0;
    let frame = 0;
    let currentTask = '';
    let done = new Set<string>();   // milestones the log has shown for this run
    let lastMilestoneAt = Date.now();
    let stick = true;               // follow the last line until the person scrolls up
    let stuckPanel: HTMLElement | undefined;

    // while the pipeline runs: a big blinking WAIT panel above the stepper (spinner, elapsed time, the stage it is in); gone when it ends
    const banner = () => {
      const id = taskIdOf();
      const old = document.getElementById('tl-wait');
      const stepper = document.querySelector('.MuiStepper-root');
      const running = Boolean(id && stepper && stepper.querySelector('.MuiCircularProgress-root, [class*="MuiStepIcon-active"]'));
      if (!running) {
        old?.remove();
        bannerSince = 0;
        return;
      }
      let el = old;
      if (!el) {
        const card = stepper!.closest('.MuiPaper-rounded') ?? stepper!.parentElement;
        if (!card?.parentElement) return;
        el = document.createElement('div');
        el.id = 'tl-wait';
        el.innerHTML = '<span class="tl-spin"></span><span class="tl-big">WAIT</span><span class="tl-sub"></span><div class="tl-barrow"><div class="tl-track"><div class="tl-fill"></div></div><span class="tl-pct"></span></div>';
        card.parentElement.insertBefore(el, card);
        bannerSince = Date.now();
      }
      const stages = [...document.querySelectorAll('[data-tl="stage"]')];
      const stage = stages.length ? (stages[stages.length - 1].textContent ?? '').replace(/━/g, '').trim() : 'starting the request';
      const s = Math.round((Date.now() - bannerSince) / 1000);
      const spin = el.querySelector('.tl-spin');
      const sub = el.querySelector('.tl-sub');
      if (spin) spin.textContent = SPIN[frame % SPIN.length];
      if (sub) sub.textContent = `PIPELINE RUNNING · ${stage.toUpperCase()} · ELAPSED ${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
      // progress: the planned time of the milestones already done, plus part of the one in progress (never more than 90% of it)
      const doneSecs = PLAN.filter(m => done.has(m.key)).reduce((a, m) => a + m.secs, 0);
      const next = PLAN.find(m => !done.has(m.key));
      const part = next ? Math.min(next.secs * 0.9, (Date.now() - lastMilestoneAt) / 1000) : 0;
      const progress = Math.min(0.99, (doneSecs + part) / PLAN_TOTAL);
      const eta = Math.max(0, Math.round(PLAN_TOTAL - doneSecs - part));
      const fill = el.querySelector('.tl-fill') as HTMLElement | null;
      const pct = el.querySelector('.tl-pct');
      if (fill) fill.style.width = `${(progress * 100).toFixed(1)}%`;
      if (pct) pct.textContent = `${Math.round(progress * 100)}%  ·  EST. ${String(Math.floor(eta / 60)).padStart(2, '0')}:${String(eta % 60).padStart(2, '0')} LEFT`;
    };
    const tick = window.setInterval(() => {
      frame += 1;
      const route = location.pathname.replace(/\/+$/, '');
      if (document.body.dataset.pcRoute !== route) document.body.dataset.pcRoute = route;
      if (location.pathname.includes('/create/tasks/')) banner();
    }, 110);

    // open the log by itself, once per run
    const openLog = () => {
      const id = taskIdOf();
      if (!id || opened.has(id)) return;
      const b = [...document.querySelectorAll('button')].find(x => /show logs/i.test(x.textContent ?? ''));
      if (b) {
        opened.add(id);
        (b as HTMLButtonElement).click();
      }
    };

    const scan = () => {
      if (!location.pathname.includes('/create/tasks/')) return;
      openLog();
      const root = document.getElementById('root');
      if (!root) return;
      const rows: Array<{ el: HTMLElement; kind: Kind; name: string }> = [];
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      for (let n = walker.nextNode(); n; n = walker.nextNode()) {
        const text = n.textContent ?? '';
        if (text.length < 8 || !/\d{4}-\d\d-\d\dT|━━/.test((n.parentElement?.parentElement?.textContent ?? text).slice(0, 60))) continue;
        const row = (n.parentElement?.closest('div') ?? n.parentElement) as HTMLElement | null;
        if (!row) continue;
        const full = row.textContent ?? text;
        const k = kindOf(full);
        if (k && !rows.some(r => r.el === row)) rows.push({ el: row, ...k });
      }
      if (rows.length === 0) return;
      const taskId = taskIdOf() ?? '';
      if (taskId !== currentTask) {
        currentTask = taskId;
        done = new Set();
        lastMilestoneAt = Date.now();
        stick = true;
      }
      for (const r of rows) {
        const full = r.el.textContent ?? '';
        for (const m of PLAN) {
          if (!done.has(m.key) && m.re.test(full)) {
            done.add(m.key);
            lastMilestoneAt = Date.now();
          }
        }
      }
      rows.sort((a, b) => (a.el.compareDocumentPosition(b.el) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1));
      rows.forEach((r, i) => {
        // a running line is history once a later line says how that step ended
        const ended = r.kind === 'run' && rows.slice(i + 1).some(o => (o.kind === 'ok' || o.kind === 'fail' || o.kind === 'skip') && o.name === r.name);
        r.el.dataset.tl = ended ? 'done' : r.kind;
        if (!seen.has(r.el)) {
          seen.add(r.el);
          r.el.dataset.tlNew = '1';
        }
        delete r.el.dataset.tlLast;
      });
      const last = rows[rows.length - 1];
      last.el.dataset.tlLast = '1';
      const panel = scrollParent(rows[0].el);
      if (panel) {
        panel.dataset.tlpanel = '1';
        // a normal terminal size: the log area and the boxes around it get the height (react-window measures its parent)
        const want = Math.max(420, Math.round(window.innerHeight * 0.58));
        let n: HTMLElement | null = panel;
        for (let i = 0; i < 4 && n; i++, n = n.parentElement) {
          if (n.classList.contains('MuiPaper-root') && i > 0) break;
          if (n.clientHeight < want) {
            n.style.setProperty('height', `${want}px`, 'important');
            n.style.setProperty('min-height', `${want}px`, 'important');
          }
        }
        if (panel !== stuckPanel) {
          stuckPanel = panel;
          panel.addEventListener('scroll', () => {
            stick = panel.scrollHeight - panel.scrollTop - panel.clientHeight < 48;
          });
        }
        if (stick) panel.scrollTop = panel.scrollHeight;   // the last line is the one to see
      }
    };

    const schedule = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(scan);
    };
    const mo = new MutationObserver(schedule);
    mo.observe(document.body, { childList: true, subtree: true, characterData: true });
    schedule();
    return () => {
      mo.disconnect();
      window.clearInterval(tick);
      document.getElementById('tl-wait')?.remove();
      cancelAnimationFrame(raf);
    };
  }, []);

  return <style>{CSS}</style>;
}

const MONO = "ui-monospace, 'Cascadia Mono', 'SF Mono', Consolas, Menlo, monospace";

const CSS = `
@keyframes tl-in { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
@keyframes tl-blink { 0%, 49% { opacity: 1; } 50%, 100% { opacity: 0; } }
@keyframes tl-soft { 0%, 100% { opacity: 1; } 50% { opacity: 0.45; } }
@keyframes tl-flow { from { background-position: 0% 0; } to { background-position: 200% 0; } }
@keyframes tl-stripes { from { background-position: 0 0; } to { background-position: 40px 0; } }
@keyframes tl-sweep { from { transform: translateX(-100%); } to { transform: translateX(100%); } }

/* the log as a console */
[data-tlpanel] { background-color: #0b0f17 !important; background-image: none !important; border: 1px solid #1f2937 !important; border-radius: 12px !important; padding: 10px 12px !important; box-shadow: var(--pc-shadow); }
[data-tl] { font-family: ${MONO} !important; font-size: 13px !important; line-height: 1.7 !important; white-space: pre-wrap; color: #94a3b8; overflow: hidden; text-overflow: clip; }
[data-tl][data-tl-new] { animation: tl-in 0.25s ease-out both; }
[data-tl] * { font-family: ${MONO} !important; color: inherit !important; }
[data-tl='info'] { color: #94a3b8; }
[data-tl='ok'] { color: #34d399; }
[data-tl='done'], [data-tl='skip'] { color: #64748b; }
[data-tl='wait'] { color: #8b95a8; }
[data-tl='run'] { color: #a5b4fc; animation: tl-soft 1.2s ease-in-out infinite; }
[data-tl='fail'], [data-tl='err'] { color: #f87171; }
[data-tl='stage'] { color: #e2e8f0; font-weight: 700; letter-spacing: 0.04em; box-shadow: inset 0 1px 0 #1f2937; }
[data-tl][data-tl-last]::after { content: '▍'; color: #a5b4fc; margin-left: 6px; animation: tl-blink 1s steps(1) infinite; }

/* the stepper */
.MuiStepConnector-alternativeLabel .MuiStepConnector-line { border-color: var(--pc-border-2); border-top-width: 2px; border-radius: 2px; }
.MuiStepConnector-active .MuiStepConnector-line, .MuiStepConnector-completed .MuiStepConnector-line { border: 0 !important; height: 2px; border-radius: 2px; background: linear-gradient(90deg, var(--pc-accent-solid), #a78bfa, var(--pc-accent-solid)); background-size: 200% 100%; animation: tl-flow 2s linear infinite; }
.MuiStepIcon-root.MuiStepIcon-active { color: var(--pc-accent-solid) !important; }
.MuiStepIcon-root.MuiStepIcon-completed { color: var(--pc-ok) !important; }
.MuiStepIcon-root.Mui-error { color: var(--pc-bad) !important; }
.MuiStepLabel-label.MuiStepLabel-completed { color: var(--pc-text) !important; }
.MuiStepLabel-label.Mui-error { color: var(--pc-bad) !important; }
.MuiLinearProgress-root { height: 4px !important; border-radius: 4px; background-color: var(--pc-accent-soft) !important; overflow: hidden; }
.MuiLinearProgress-bar { background-color: var(--pc-accent-solid) !important; }

/* the WAIT panel while the pipeline runs: a status block with a progress bar and the estimated time left */
#tl-wait { position: relative; display: flex; align-items: center; gap: 22px; flex-wrap: wrap; margin: 0 0 14px; padding: 18px 24px; overflow: hidden; border-radius: 12px; background: #0b0f17; border: 1px solid #1f2937; font-family: ${MONO}; color: #e2e8f0; box-shadow: var(--pc-shadow); }
#tl-wait::after { content: ''; position: absolute; left: 0; right: 0; bottom: 0; height: 2px; background: linear-gradient(90deg, transparent, #818cf8, transparent); animation: tl-sweep 1.8s linear infinite; }
#tl-wait .tl-spin { font-size: 40px; line-height: 1; color: #a5b4fc; min-width: 1.1em; text-align: center; }
#tl-wait .tl-big { font-size: 44px; font-weight: 800; letter-spacing: 0.14em; line-height: 1; color: #f1f5f9; animation: tl-soft 1.4s ease-in-out infinite; }
#tl-wait .tl-big::after { content: '_'; color: #a5b4fc; }
#tl-wait .tl-sub { font-size: 13px; letter-spacing: 0.1em; color: #94a3b8; flex: 1; min-width: 260px; }
#tl-wait .tl-barrow { flex-basis: 100%; display: flex; align-items: center; gap: 16px; margin-top: 2px; }
#tl-wait .tl-track { flex: 1; height: 10px; border-radius: 6px; background: #1b2231; overflow: hidden; }
#tl-wait .tl-fill { height: 100%; width: 0; border-radius: 6px; transition: width 0.6s ease-out; background: repeating-linear-gradient(115deg, #6366f1 0 12px, #818cf8 12px 24px); animation: tl-stripes 0.9s linear infinite; }
#tl-wait .tl-pct { font-size: 13px; letter-spacing: 0.08em; color: #cbd5e1; white-space: nowrap; min-width: 220px; text-align: right; }
`;
