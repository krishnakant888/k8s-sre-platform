import { Content, Link } from '@backstage/core-components';
import { usePolledProxy } from './proxy';

const REPO = 'krishnakant888/k8s-sre-platform';
const ARGOCD_UI = 'https://argocd.localhost:7443';
const REPORTS_UI = 'https://reports.localhost:9443';

type App = { metadata: { name: string }; status?: { sync?: { status?: string }; health?: { status?: string } } };
type Rule = { name: string; state?: string; alerts?: Array<{ state: string; activeAt?: string; labels?: Record<string, string> }> };
type Run = { id: number; run_number: number; status: string; conclusion: string | null; run_started_at?: string; created_at: string; html_url: string; display_title: string };
type RcaDir = { name: string; type: string; mtime: string };

const ago = (iso?: string) => {
  if (!iso) return '';
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
};

const Pill = ({ kind, children }: { kind: 'ok' | 'warn' | 'bad' | 'run' | 'idle'; children: React.ReactNode }) => <span className={`pc-pill ${kind}`}>{children}</span>;

function appPill(a: App) {
  const sync = a.status?.sync?.status ?? 'Unknown';
  const health = a.status?.health?.status ?? 'Unknown';
  if (health === 'Degraded' || health === 'Missing') return <Pill kind="bad">{health}</Pill>;
  if (health === 'Progressing') return <Pill kind="run">Progressing</Pill>;
  if (sync === 'OutOfSync') return <Pill kind="warn">OutOfSync</Pill>;
  if (sync === 'Synced' && health === 'Healthy') return <Pill kind="ok">Healthy</Pill>;
  return <Pill kind="idle">{sync} / {health}</Pill>;
}

function runPill(r: Run) {
  if (r.status === 'completed') return r.conclusion === 'success' ? <Pill kind="ok">Succeeded</Pill> : r.conclusion === 'cancelled' ? <Pill kind="idle">Cancelled</Pill> : <Pill kind="bad">Failed</Pill>;
  return <Pill kind="run">{r.status === 'in_progress' ? 'In progress' : 'Queued'}</Pill>;
}

/** The landing page: the state of the platform at a glance, from Argo CD, Grafana, the selfservice workflow and the RCA reports. */
export function OverviewPage() {
  const apps = usePolledProxy<{ items: App[] }>('/argocd/api/v1/applications', 'json', 30000);
  const rules = usePolledProxy<{ data?: { groups?: Array<{ rules?: Rule[] }> } }>('/grafana/api/prometheus/grafana/api/v1/rules', 'json', 30000);
  const runs = usePolledProxy<{ workflow_runs: Run[] }>(`/github/repos/${REPO}/actions/workflows/selfservice.yaml/runs?per_page=6`, 'json', 15000);
  const rca = usePolledProxy<RcaDir[]>('/rca/_list/', 'json', 60000);

  const appList = apps.value?.items ?? [];
  const okApps = appList.filter(a => a.status?.sync?.status === 'Synced' && a.status?.health?.status === 'Healthy');
  const attention = appList.filter(a => !okApps.includes(a));
  const firing = (rules.value?.data?.groups ?? [])
    .flatMap(g => g.rules ?? [])
    .flatMap(r => (r.alerts ?? []).filter(a => /^(alerting|firing)$/i.test(a.state)).map(a => ({ name: r.name, ...a })));
  const runList = runs.value?.workflow_runs ?? [];
  const running = runList.filter(r => r.status !== 'completed');
  const reports = (rca.value ?? []).filter(d => d.type === 'directory' && d.name.startsWith('rca-')).sort((a, b) => b.mtime.localeCompare(a.mtime) || b.name.localeCompare(a.name)).slice(0, 6);
  const updated = apps.updated ?? rules.updated ?? runs.updated;

  return (
    <Content>
      <div className="pc-page">
        <div className="pc-head">
          <div>
            <h1>Platform overview</h1>
            <p>The state of the clusters, alerts and requests{updated ? ` · updated ${updated.toLocaleTimeString()}` : ''}</p>
          </div>
          <Link to="/create" className="pc-cta">+ New request</Link>
        </div>

        <div className="pc-kpis">
          <div className={`pc-kpi ${apps.value ? (attention.length === 0 ? 'ok' : 'warn') : ''}`}>
            <div className="l">Applications healthy</div>
            <div className="v">{apps.value ? okApps.length : '–'}<small>/ {apps.value ? appList.length : '–'}</small></div>
            <div className="s">Synced and Healthy in Argo CD</div>
          </div>
          <div className={`pc-kpi ${apps.value ? (attention.length === 0 ? 'ok' : 'warn') : ''}`}>
            <div className="l">Need attention</div>
            <div className="v">{apps.value ? attention.length : '–'}</div>
            <div className="s">{attention.length === 0 ? 'Nothing out of sync or degraded' : 'OutOfSync, Progressing or Degraded'}</div>
          </div>
          <div className={`pc-kpi ${rules.value ? (firing.length === 0 ? 'ok' : 'bad') : ''}`}>
            <div className="l">Firing alerts</div>
            <div className="v">{rules.value ? firing.length : '–'}</div>
            <div className="s">{firing.length === 0 ? 'No alert is firing in Grafana' : 'The SRE agent is investigating them'}</div>
          </div>
          <div className="pc-kpi">
            <div className="l">Requests running</div>
            <div className="v">{runs.value ? running.length : '–'}</div>
            <div className="s">{runs.value ? `${runList.length} recent self-service requests` : ' '}</div>
          </div>
        </div>

        <div className="pc-grid">
          <section className="pc-card">
            <header><h3>Applications needing attention</h3><a href={ARGOCD_UI}>Open Argo CD</a></header>
            {!apps.value ? <div className="empty">{apps.error ? `Argo CD could not be read: ${apps.error.message}` : 'Loading'}</div> : attention.length === 0 ? (
              <div className="pc-row"><Pill kind="ok">All healthy</Pill><span className="grow">All {appList.length} applications are Synced and Healthy.</span></div>
            ) : attention.slice(0, 7).map(a => (
              <div className="pc-row" key={a.metadata.name}>
                <span className="grow"><a href={`${ARGOCD_UI}/applications/argocd/${a.metadata.name}`}>{a.metadata.name}</a></span>
                {appPill(a)}
              </div>
            ))}
          </section>

          <section className="pc-card">
            <header><h3>Latest requests</h3><Link to="/self-service">All requests</Link></header>
            {!runs.value ? <div className="empty">{runs.error ? `GitHub could not be read: ${runs.error.message}` : 'Loading'}</div> : runList.length === 0 ? <div className="empty">No request yet.</div> : runList.map(r => (
              <div className="pc-row" key={r.id}>
                <span className="grow"><a href={r.html_url} target="_blank" rel="noreferrer">#{r.run_number} {r.display_title.replace(/^selfservice: ?/, '')}</a></span>
                {runPill(r)}
                <span className="muted">{ago(r.run_started_at ?? r.created_at)}</span>
              </div>
            ))}
          </section>

          <section className="pc-card">
            <header><h3>Firing alerts</h3><a href="https://grafana.localhost:9443/alerting/list">Open Grafana</a></header>
            {!rules.value ? <div className="empty">{rules.error ? `Grafana could not be read: ${rules.error.message}` : 'Loading'}</div> : firing.length === 0 ? (
              <div className="pc-row"><Pill kind="ok">Clear</Pill><span className="grow">No alert is firing.</span></div>
            ) : firing.slice(0, 7).map((a, i) => (
              <div className="pc-row" key={i}>
                <span className="grow">{a.name} <span className="muted">{a.labels?.service ?? a.labels?.deployment ?? a.labels?.pod ?? a.labels?.ingress ?? ''}</span></span>
                <Pill kind="bad">Firing</Pill>
                <span className="muted">{ago(a.activeAt)}</span>
              </div>
            ))}
          </section>

          <section className="pc-card">
            <header><h3>Recent RCA reports</h3><a href={REPORTS_UI}>Open the viewer</a></header>
            {!rca.value ? <div className="empty">{rca.error ? `The reports could not be read: ${rca.error.message}` : 'Loading'}</div> : reports.length === 0 ? <div className="empty">No report yet.</div> : reports.map(d => {
              const m = /^rca-(.+)-(\d{8})-(\d{6})$/.exec(d.name);
              return (
                <div className="pc-row" key={d.name}>
                  <span className="grow"><a href={`${REPORTS_UI}/#${d.name}`} target="_blank" rel="noreferrer">{m ? m[1] : d.name}</a></span>
                  <span className="muted">{ago(new Date(d.mtime).toISOString())}</span>
                </div>
              );
            })}
          </section>
        </div>
      </div>
    </Content>
  );
}
