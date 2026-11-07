import { Fragment, useCallback, useEffect, useRef, useState } from 'react';
import { discoveryApiRef, fetchApiRef, useApi } from '@backstage/frontend-plugin-api';
import { Content, InfoCard, Link, Progress, ResponseErrorPanel, StatusAborted, StatusError, StatusOK, StatusPending, StatusRunning } from '@backstage/core-components';

const REPO = 'krishnakant888/k8s-sre-platform';
const GITHUB = `https://github.com/${REPO}`;
const ARGOCD_UI = 'https://argocd.localhost:7443';

type Run = {
  id: number;
  run_number: number;
  status: string;
  conclusion: string | null;
  created_at: string;
  run_started_at?: string;
  updated_at: string;
  html_url: string;
  display_title: string;
  actor?: { login: string };
};
type Step = { name: string; number: number; status: string; conclusion: string | null };
type Job = { id: number; name: string; status: string; conclusion: string | null; steps?: Step[] };
type Pr = {
  number: number;
  title: string;
  html_url: string;
  state: string;
  merged_at: string | null;
  merge_commit_sha: string | null;
  created_at: string;
  head: { ref: string };
};
type ArgoApp = {
  metadata: { name: string };
  status?: {
    sync?: { status?: string; revision?: string; revisions?: string[] };
    health?: { status?: string };
    operationState?: { phase?: string };
    resources?: Array<{ kind: string; name: string }>;
  };
};

const running = (r: Run) => r.status === 'queued' || r.status === 'in_progress' || r.status === 'waiting' || r.status === 'pending';

/** GET through the portal backend's proxy (the backend adds the token). `prefix` is /github/repos/<repo> for GitHub and /argocd for Argo CD. */
function useProxyGet() {
  const discovery = useApi(discoveryApiRef);
  const { fetch } = useApi(fetchApiRef);
  return useCallback(
    async <T,>(path: string): Promise<T> => {
      const base = await discovery.getBaseUrl('proxy');
      const res = await fetch(`${base}${path}`);
      if (!res.ok) throw new Error(`${res.status} ${res.statusText} (${path.split('?')[0]})`);
      return (await res.json()) as T;
    },
    [discovery, fetch],
  );
}
const useGithub = () => {
  const get = useProxyGet();
  return useCallback(<T,>(path: string) => get<T>(`/github/repos/${REPO}${path}`), [get]);
};

function seconds(from?: string, to?: string) {
  if (!from) return '';
  const s = Math.max(0, Math.round(((to ? new Date(to).getTime() : Date.now()) - new Date(from).getTime()) / 1000));
  return s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`;
}

function RunStatus({ status, conclusion }: { status: string; conclusion: string | null }) {
  if (status === 'completed') {
    if (conclusion === 'success') return <StatusOK>Succeeded</StatusOK>;
    if (conclusion === 'cancelled' || conclusion === 'skipped') return <StatusAborted>{conclusion === 'cancelled' ? 'Cancelled' : 'Skipped'}</StatusAborted>;
    return <StatusError>Failed</StatusError>;
  }
  if (status === 'in_progress') return <StatusRunning>In progress</StatusRunning>;
  return <StatusPending>Queued</StatusPending>;
}

function StepIcon({ s }: { s: Step }) {
  if (s.status === 'completed') return s.conclusion === 'success' ? <StatusOK /> : s.conclusion === 'skipped' ? <StatusAborted /> : <StatusError />;
  if (s.status === 'in_progress') return <StatusRunning />;
  return <StatusPending />;
}

/** The steps of the run, like the GitHub Actions page, refreshed while the run is going. */
function Steps({ run, refreshMs }: { run: Run; refreshMs: number }) {
  const get = useGithub();
  const [jobs, setJobs] = useState<Job[] | undefined>();
  const [error, setError] = useState<string | undefined>();
  const live = running(run);
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      try {
        const r = await get<{ jobs: Job[] }>(`/actions/runs/${run.id}/jobs`);
        if (!cancelled) setJobs(r.jobs);
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      }
      if (!cancelled && live) timer = setTimeout(load, refreshMs);
    };
    load();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [get, run.id, live, refreshMs]);

  if (error) return <span>{error}</span>;
  if (!jobs) return <Progress />;
  return (
    <div style={{ padding: '4px 0 8px 28px' }}>
      {jobs.map(j => (
        <div key={j.id}>
          {(j.steps ?? []).map(s => (
            <div key={s.number} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '2px 0' }}>
              <StepIcon s={s} />
              <span>{s.name}</span>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

/** The Argo CD application a request ends up in. Mirrors the mapping of the selfservice:run action in the backend. */
function appFor(pr: Pr, apps: ArgoApp[]): string | undefined {
  const action = pr.head.ref.split('/')[1]?.split('-')[0];
  const fixed: Record<string, string> = { sandbox: 'platform-base', access: 'platform-base', expire: 'platform-base', drill: 'platform-drills', alert: 'observability-alerting', dashboard: 'observability-dashboards' };
  if (action && fixed[action]) return fixed[action];
  const service = pr.title.split(':')[0].trim();
  return apps.find(a => a.status?.resources?.some(r => r.kind === 'Deployment' && r.name === service))?.metadata.name;
}

/** Does the revision Argo CD compares against contain the merge commit? One GitHub compare call per pair, remembered. */
function useContains(get: <T>(p: string) => Promise<T>, pairs: Array<{ sha: string; rev: string }>, tick: number) {
  const cache = useRef<Record<string, boolean>>({});
  const [, force] = useState(0);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      let changed = false;
      for (const { sha, rev } of pairs) {
        const key = `${sha}:${rev}`;
        if (key in cache.current) continue;
        if (sha === rev) {
          cache.current[key] = true;
          changed = true;
          continue;
        }
        try {
          const c = await get<{ status: string }>(`/compare/${sha}...${rev}`);
          if (c.status === 'ahead' || c.status === 'identical') cache.current[key] = true;
          else if (c.status === 'behind') cache.current[key] = false;
          else continue;
          changed = true;
        } catch {
          /* try again on the next refresh */
        }
      }
      if (changed && !cancelled) force(n => n + 1);
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(pairs), tick]);
  return (sha: string, rev: string) => cache.current[`${sha}:${rev}`];
}

function Deployment({ pr, app, seen }: { pr?: Pr; app?: ArgoApp; seen?: boolean }) {
  if (!pr) return <>–</>;
  if (!pr.merged_at) return pr.state === 'open' ? <StatusPending>Waiting for the merge</StatusPending> : <StatusAborted>Not merged</StatusAborted>;
  if (!app) return <StatusAborted>No Argo CD app</StatusAborted>;
  const link = (text: string) => <Link to={`${ARGOCD_UI}/applications/argocd/${app.metadata.name}`}>{text}</Link>;
  if (seen === undefined) return <StatusPending>Checking</StatusPending>;
  if (!seen) return <StatusPending>Waiting for Argo CD · {link(app.metadata.name)}</StatusPending>;
  const sync = app.status?.sync?.status;
  const health = app.status?.health?.status;
  const phase = app.status?.operationState?.phase;
  if (phase === 'Running' || sync === 'OutOfSync') return <StatusRunning>Syncing · {link(app.metadata.name)}</StatusRunning>;
  if (health === 'Degraded' || health === 'Missing') return <StatusError>{health} · {link(app.metadata.name)}</StatusError>;
  if (health === 'Progressing') return <StatusRunning>Rolling out · {link(app.metadata.name)}</StatusRunning>;
  if (sync === 'Synced' && health === 'Healthy') return <StatusOK>Deployed · {link(app.metadata.name)}</StatusOK>;
  return <StatusPending>{sync ?? 'Unknown'} / {health ?? 'Unknown'} · {link(app.metadata.name)}</StatusPending>;
}

/** What was asked for from the portal: each run of the selfservice workflow with its progress, the pull request it opened, and the Argo CD deployment of it. */
export function SelfServicePage() {
  const get = useGithub();
  const proxyGet = useProxyGet();
  const [runs, setRuns] = useState<Run[] | undefined>();
  const [prs, setPrs] = useState<Pr[]>([]);
  const [apps, setApps] = useState<ArgoApp[]>([]);
  const [error, setError] = useState<Error | undefined>();
  const [updated, setUpdated] = useState<Date | undefined>();
  const [open, setOpen] = useState<Record<number, boolean>>({});
  const [tick, setTick] = useState(0);

  const prOf = useCallback(
    (r: Run) => {
      const from = new Date(r.run_started_at ?? r.created_at).getTime() - 5000;
      const to = new Date(r.updated_at).getTime() + 120000;
      return prs.find(p => {
        const t = new Date(p.created_at).getTime();
        return t >= from && t <= to;
      });
    },
    [prs],
  );

  const anyRunning = (runs ?? []).some(running);
  const anyDeploying = (runs ?? []).some(r => {
    const p = prOf(r);
    return p && (!p.merged_at ? p.state === 'open' : Date.now() - new Date(p.merged_at).getTime() < 15 * 60000);
  });
  const refreshMs = anyRunning || anyDeploying ? 5000 : 30000;

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      try {
        const [r, p] = await Promise.all([
          get<{ workflow_runs: Run[] }>('/actions/workflows/selfservice.yaml/runs?per_page=20'),
          get<Pr[]>('/pulls?state=all&per_page=40&sort=created&direction=desc'),
        ]);
        if (cancelled) return;
        setRuns(r.workflow_runs);
        setPrs(p.filter(x => x.head.ref.startsWith('selfservice/')));
        setUpdated(new Date());
        setError(undefined);
        try {
          // the whole list (about 600 KB): Argo CD's \ filter drops some of the status fields when several are asked for
          const a = await proxyGet<{ items: ArgoApp[] }>('/argocd/api/v1/applications');
          if (!cancelled) setApps(a.items ?? []);
        } catch {
          /* Argo CD unreachable: the Deployment column says so */
        }
        if (!cancelled) setTick(n => n + 1);
      } catch (e) {
        if (!cancelled) setError(e as Error);
      }
      if (!cancelled) timer = setTimeout(load, refreshMs);
    };
    load();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [get, proxyGet, refreshMs]);

  // the clock of a run in progress ticks every second
  useEffect(() => {
    if (!anyRunning) return undefined;
    const t = setInterval(() => setTick(n => n + 1), 1000);
    return () => clearInterval(t);
  }, [anyRunning]);

  const rows = (runs ?? []).map(r => {
    const pr = prOf(r);
    const appName = pr ? appFor(pr, apps) : undefined;
    const app = apps.find(a => a.metadata.name === appName);
    const rev = app?.status?.sync?.revisions?.find(x => /^[0-9a-f]{40}$/.test(x)) ?? app?.status?.sync?.revision;
    return { r, pr, app, rev };
  });
  const contains = useContains(
    get,
    rows.filter(x => x.pr?.merged_at && x.pr.merge_commit_sha && x.rev).slice(0, 8).map(x => ({ sha: x.pr!.merge_commit_sha!, rev: x.rev! })),
    tick,
  );

  return (
    <Content>
      <InfoCard
        title="Requests"
        subheader={
          updated
            ? `Last updated ${updated.toLocaleTimeString()} · refreshes every ${refreshMs / 1000} s${anyRunning || anyDeploying ? ' while a request is running or deploying' : ''}`
            : 'Loading'
        }
        deepLink={{ title: 'New request', link: '/create' }}
      >
        {error && <ResponseErrorPanel error={error} />}
        {!runs && !error && <Progress />}
        {runs && runs.length === 0 && <span>No request yet. Create one from the templates.</span>}
        {runs && runs.length > 0 && (
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ textAlign: 'left' }}>
                <th style={{ padding: '6px 8px' }}>Request</th>
                <th style={{ padding: '6px 8px' }}>Status</th>
                <th style={{ padding: '6px 8px' }}>Time</th>
                <th style={{ padding: '6px 8px' }}>Pull request</th>
                <th style={{ padding: '6px 8px' }}>Deployment</th>
                <th style={{ padding: '6px 8px' }}>Started</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(({ r, pr, app, rev }) => {
                const expanded = open[r.id] ?? running(r);
                const seen = pr?.merge_commit_sha && rev ? contains(pr.merge_commit_sha, rev) : undefined;
                return (
                  <Fragment key={r.id}>
                    <tr style={{ borderTop: '1px solid rgba(128,128,128,.3)' }}>
                      <td style={{ padding: '8px' }}>
                        <button
                          type="button"
                          onClick={() => setOpen(o => ({ ...o, [r.id]: !expanded }))}
                          style={{ background: 'none', border: 0, cursor: 'pointer', color: 'inherit', padding: 0 }}
                          aria-expanded={expanded}
                        >
                          {expanded ? '▾' : '▸'}
                        </button>{' '}
                        <Link to={r.html_url}>
                          #{r.run_number} {r.display_title}
                        </Link>
                      </td>
                      <td style={{ padding: '8px' }}><RunStatus status={r.status} conclusion={r.conclusion} /></td>
                      <td style={{ padding: '8px' }}>{seconds(r.run_started_at ?? r.created_at, running(r) ? undefined : r.updated_at)}</td>
                      <td style={{ padding: '8px' }}>
                        {pr ? (
                          <Link to={pr.html_url}>
                            #{pr.number} {pr.merged_at ? 'merged' : pr.state === 'open' ? 'open' : 'closed'}
                          </Link>
                        ) : running(r) ? (
                          'not yet'
                        ) : (
                          '–'
                        )}
                      </td>
                      <td style={{ padding: '8px' }}><Deployment pr={pr} app={app} seen={seen} /></td>
                      <td style={{ padding: '8px' }}>{new Date(r.run_started_at ?? r.created_at).toLocaleString()}</td>
                    </tr>
                    {expanded && (
                      <tr>
                        <td colSpan={6}>
                          <Steps run={r} refreshMs={refreshMs} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        )}
        <p style={{ marginTop: 12 }}>
          A request is a pull request that merges itself once the checks pass; Argo CD then applies it, which the Deployment column follows. <Link to={`${GITHUB}/pulls`}>All pull requests</Link>
        </p>
      </InfoCard>
    </Content>
  );
}
