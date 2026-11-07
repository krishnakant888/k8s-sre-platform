import { coreServices, createBackendModule } from '@backstage/backend-plugin-api';
import { createTemplateAction, scaffolderActionsExtensionPoint } from '@backstage/plugin-scaffolder-node';
import { InputError } from '@backstage/errors';

/**
 * selfservice:run — starts the `selfservice` workflow of the platform repo and follows it, so the template's run page shows the GitHub Actions
 * progress live: each step of the workflow job is logged as it starts and finishes. It ends with the pull request the workflow opened, or fails
 * with the reason the workflow gave (a refused request, for example).
 */

type Run = { id: number; status: string; conclusion: string | null; created_at: string; run_started_at?: string; updated_at: string; html_url: string; display_title: string; run_number: number };
type Step = { name: string; number: number; status: string; conclusion: string | null };
type Job = { id: number; name: string; status: string; conclusion: string | null; steps?: Step[] };
type Pr = { number: number; title: string; html_url: string; created_at: string; head: { ref: string } };
type PrFull = Pr & { merged: boolean; state: string; merge_commit_sha: string | null; merged_at: string | null };
type ArgoApp = {
  metadata: { name: string };
  status?: {
    sync?: { status?: string; revision?: string; revisions?: string[] };
    health?: { status?: string };
    operationState?: { phase?: string; message?: string };
    resources?: Array<{ kind: string; name: string; status?: string; health?: { status?: string; message?: string } }>;
    conditions?: Array<{ type: string; message: string }>;
  };
};

const ARGOCD_UI = 'https://argocd.localhost:7443';
// the application each kind of request ends up in; the others are found by the Deployment they change
const FIXED_APPS: Record<string, string> = { sandbox: 'platform-base', access: 'platform-base', expire: 'platform-base', drill: 'platform-drills', alert: 'observability-alerting', dashboard: 'observability-dashboards' };

const sleep = (ms: number) => new Promise(r => setTimeout(r, ms));

export const selfServiceModule = createBackendModule({
  pluginId: 'scaffolder',
  moduleId: 'selfservice-run',
  register(env) {
    env.registerInit({
      deps: { scaffolder: scaffolderActionsExtensionPoint, config: coreServices.rootConfig },
      async init({ scaffolder, config }) {
        const tokenOf = () =>
          (config.getOptionalConfigArray('integrations.github') ?? [])
            .find(c => (c.getOptionalString('host') ?? 'github.com') === 'github.com')
            ?.getOptionalString('token');

        scaffolder.addActions(
          createTemplateAction({
            id: 'selfservice:run',
            description: 'Starts the selfservice workflow of the platform repo and follows its steps in the log until it ends; returns the pull request it opened.',
            schema: {
              input: {
                action: z => z.string({ description: 'What the workflow does: scale, resources, env, image, rollback, route, promote, alert, dashboard, sandbox, access, drill' }),
                params: z => z.record(z.string(), z.any(), { description: 'The parameters of the action (see scripts/selfservice.py in the platform repo)' }).optional(),
                requestedBy: z => z.string({ description: 'Who asked, for the pull request text' }).optional(),
                repo: z => z.string({ description: 'owner/repo of the platform repo' }).optional(),
                workflow: z => z.string({ description: 'The workflow file' }).optional(),
                ref: z => z.string({ description: 'Branch the workflow runs on' }).optional(),
                timeoutMinutes: z => z.number({ description: 'How long to follow the run' }).optional(),
              },
              output: {
                runUrl: z => z.string({ description: 'The workflow run on GitHub' }),
                pullRequestUrl: z => z.string({ description: 'The pull request the workflow opened, when it opened one' }).optional(),
                pullRequestNumber: z => z.number({ description: 'Its number' }).optional(),
                argoApp: z => z.string({ description: 'The Argo CD application the change was deployed by' }).optional(),
                argoUrl: z => z.string({ description: 'That application in the Argo CD UI' }).optional(),
              },
            },
            async handler(ctx) {
              const { action, params = {}, requestedBy = 'the developer portal', repo = 'krishnakant888/k8s-sre-platform', workflow = 'selfservice.yaml', ref = 'main', timeoutMinutes = 10 } = ctx.input;
              const token = tokenOf();
              if (!token) throw new InputError('No GitHub token: set GITHUB_TOKEN in the portal secret (docs/self-service.md in the platform repo).');
              const gh = async <T>(path: string, init?: { method: string; body: string }): Promise<{ status: number; body: T }> => {
                const res = await fetch(`https://api.github.com/repos/${repo}${path}`, {
                  method: init?.method ?? 'GET',
                  body: init?.body,
                  headers: { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json' },
                });
                const text = await res.text();
                return { status: res.status, body: (text ? JSON.parse(text) : undefined) as T };
              };

              const askedAt = Date.now() - 3000;
              ctx.logger.info('━━ [1/3] GitHub workflow ━━');
              ctx.logger.info(`Asking ${repo} to run "${action}" (workflow ${workflow} on ${ref}).`);
              const sent = await gh<{ message?: string }>(`/actions/workflows/${workflow}/dispatches`, {
                method: 'POST',
                body: JSON.stringify({ ref, inputs: { action, params: JSON.stringify(params), requested_by: requestedBy } }),
              });
              if (sent.status !== 204) throw new Error(`GitHub did not start the workflow (${sent.status}): ${sent.body?.message ?? 'no reason given'}`);

              // the dispatch does not say which run it started: take the first run created after it (the one named for this action when the run name says so)
              let run: Run | undefined;
              for (let i = 0; i < 30 && !run; i++) {
                await sleep(2000);
                const list = await gh<{ workflow_runs: Run[] }>(`/actions/workflows/${workflow}/runs?event=workflow_dispatch&per_page=10`);
                const fresh = (list.body?.workflow_runs ?? []).filter(r => Date.parse(r.created_at) >= askedAt - 2000).sort((a, b) => a.id - b.id);
                run = fresh.find(r => r.display_title.includes(`selfservice: ${action} `)) ?? fresh[0];
              }
              if (!run) throw new Error('The workflow was started but no run appeared within a minute. Check the Actions tab of the repo.');
              ctx.output('runUrl', run.html_url);
              ctx.logger.info(`Workflow run #${run.run_number} started: ${run.html_url}`);

              const icon = (s: { status: string; conclusion: string | null }) =>
                s.status === 'completed' ? (s.conclusion === 'success' ? '✓' : s.conclusion === 'skipped' ? '–' : '✗') : s.status === 'in_progress' ? '▶' : '…';
              const seen = new Map<string, string>();
              const deadline = Date.now() + timeoutMinutes * 60000;
              let jobs: Job[] = [];
              for (;;) {
                const cur = (await gh<Run>(`/actions/runs/${run.id}`)).body;
                jobs = (await gh<{ jobs: Job[] }>(`/actions/runs/${run.id}/jobs`)).body?.jobs ?? [];
                for (const j of jobs) {
                  const jk = `job:${j.id}`;
                  const js = `${j.status}/${j.conclusion}`;
                  if (seen.get(jk) !== js && (j.status !== 'queued' || !seen.has(jk))) {
                    ctx.logger.info(`${icon(j)} Job "${j.name}": ${j.status === 'completed' ? j.conclusion : j.status === 'in_progress' ? 'running' : 'waiting for a runner'}`);
                  }
                  seen.set(jk, js);
                  for (const s of j.steps ?? []) {
                    const sk = `step:${j.id}:${s.number}`;
                    const ss = `${s.status}/${s.conclusion}`;
                    if (seen.get(sk) !== ss && s.status !== 'queued') ctx.logger.info(`  ${icon(s)} ${s.name}${s.status === 'completed' ? ` (${s.conclusion})` : ''}`);
                    seen.set(sk, ss);
                  }
                }
                if (cur.status === 'completed') {
                  run = cur;
                  break;
                }
                if (Date.now() > deadline) throw new Error(`The workflow is still running after ${timeoutMinutes} minutes. It continues on GitHub: ${run.html_url}`);
                await sleep(3000);
              }

              if (run.conclusion !== 'success') {
                const failed = jobs.find(j => j.conclusion && j.conclusion !== 'success');
                let reason = '';
                if (failed) {
                  const notes = (await gh<Array<{ message?: string; title?: string; annotation_level?: string }>>(`/check-runs/${failed.id}/annotations`)).body ?? [];
                  const useful = notes.filter(n => n.annotation_level === 'failure' && n.message && !/^Process completed with exit code/.test(n.message));
                  for (const n of useful) ctx.logger.error(`${n.title ? n.title + ': ' : ''}${n.message}`);
                  reason = useful.map(n => n.message).join(' ');
                  if (!reason) reason = `step "${failed.steps?.find(s => s.conclusion === 'failure')?.name ?? failed.name}" failed`;
                }
                throw new Error(`The request was not made: ${reason || run.conclusion}. Details: ${run.html_url}`);
              }

              // after the workflow: wait for the merge, then follow the Argo CD application until it is Synced and Healthy
              const followDeployment = async (opened: Pr) => {
                ctx.output('argoUrl', `${ARGOCD_UI}/applications`);   // the application list, until the application is known
                const argoToken = process.env.ARGOCD_TOKEN;
                const argoBase = process.env.ARGOCD_URL ?? 'http://argocd-server.argocd.svc.cluster.local';
                if (!argoToken || argoToken === 'not-created') {
                  ctx.logger.info('Argo CD is not set up for the portal here, so the deployment is not followed.');
                  return;
                }
                const argo = async <T>(path: string): Promise<T | undefined> => {
                  try {
                    const res = await fetch(`${argoBase}/api/v1/${path}`, { headers: { Authorization: `Bearer ${argoToken}` } });
                    return res.ok ? ((await res.json()) as T) : undefined;
                  } catch {
                    return undefined;
                  }
                };

                // 1. the merge (the workflow merges it itself once the checks pass)
                ctx.logger.info('━━ [2/3] Pull request and merge ━━');
                ctx.logger.info('Waiting for the pull request to be merged...');
                let full: PrFull | undefined;
                for (let i = 0; i < 60; i++) {
                  full = (await gh<PrFull>(`/pulls/${opened.number}`)).body;
                  if (full?.merged || full?.state === 'closed') break;
                  await sleep(3000);
                }
                if (!full?.merged || !full.merge_commit_sha) {
                  ctx.logger.warn(`Pull request #${opened.number} is not merged (${full?.state ?? 'unknown'}): nothing is deployed until it is. ${opened.html_url}`);
                  return;
                }
                const sha = full.merge_commit_sha;
                ctx.logger.info(`✓ Merged as ${sha.slice(0, 9)}.`);

                // 2. which application
                let appName: string | undefined = FIXED_APPS[action];
                if (!appName) {
                  const service = String((params as Record<string, unknown>).service ?? '');
                  const list = await argo<{ items: ArgoApp[] }>('applications');
                  appName = list?.items?.find(a => a.status?.resources?.some(r => r.kind === 'Deployment' && r.name === service))?.metadata.name;
                }
                if (!appName) {
                  ctx.logger.info('No Argo CD application found for this request, so the deployment is not followed.');
                  return;
                }
                ctx.output('argoApp', appName);
                ctx.output('argoUrl', `${ARGOCD_UI}/applications/argocd/${appName}`);
                ctx.logger.info('━━ [3/3] Argo CD deployment ━━');
                ctx.logger.info(`Following Argo CD application ${appName} (${ARGOCD_UI}/applications/argocd/${appName})`);

                // 3. until Argo CD has the commit and the application is Synced and Healthy
                const seenState = new Map<string, string>();
                const note = (key: string, text: string) => {
                  if (seenState.get(key) !== text) {
                    seenState.set(key, text);
                    ctx.logger.info(text);
                  }
                };
                const contains = new Map<string, boolean>();
                const deployDeadline = Date.now() + 10 * 60000;
                let degradedSince = 0;
                for (;;) {
                  const app = await argo<ArgoApp>(`applications/${encodeURIComponent(appName)}`);
                  if (app) {
                    const rev = app.status?.sync?.revisions?.find(x => /^[0-9a-f]{40}$/.test(x)) ?? app.status?.sync?.revision ?? '';
                    let has = contains.get(rev);
                    if (has === undefined && rev) {
                      if (rev === sha) has = true;
                      else {
                        const cmp = (await gh<{ status?: string }>(`/compare/${sha}...${rev}`)).body;
                        has = cmp?.status === 'ahead' || cmp?.status === 'identical' ? true : cmp?.status === 'behind' ? false : undefined;
                      }
                      if (has !== undefined) contains.set(rev, has);
                    }
                    const sync = app.status?.sync?.status ?? 'Unknown';
                    const health = app.status?.health?.status ?? 'Unknown';
                    const phase = app.status?.operationState?.phase;
                    if (!has) {
                      note('seen', '… Argo CD has not picked up the commit yet (it checks Git every few minutes)');
                    } else {
                      note('seen', '✓ Argo CD has the commit');
                      if (phase === 'Running' || sync === 'OutOfSync') note('sync', '▶ Syncing');
                      if (phase === 'Failed' || phase === 'Error') {
                        throw new Error(`Argo CD could not apply the change: ${app.status?.operationState?.message ?? phase}. ${ARGOCD_UI}/applications/argocd/${appName}`);
                      }
                      if (sync === 'Synced') note('sync', '✓ Synced');
                      note('health', `${health === 'Healthy' ? '✓' : health === 'Progressing' ? '▶' : '…'} Health: ${health}`);
                      if (sync === 'Synced' && health === 'Healthy') {
                        ctx.logger.info(`✓ Deployed: ${appName} is Synced and Healthy.`);
                        return;
                      }
                      if (sync === 'Synced' && (health === 'Degraded' || health === 'Missing')) {
                        degradedSince ||= Date.now();
                        if (Date.now() - degradedSince > 60000) {
                          const bad = (app.status?.resources ?? []).filter(r => r.health && r.health.status !== 'Healthy').map(r => `${r.kind}/${r.name}: ${r.health?.message ?? r.health?.status}`);
                          if (action === 'drill') {
                            ctx.logger.info(`✓ Deployed: the drill is running and failing on purpose (${bad.join('; ') || health}). The alerts and the SRE agent should react.`);
                            return;
                          }
                          throw new Error(`Deployed, but ${appName} is ${health}: ${bad.join('; ') || 'see Argo CD'}. ${ARGOCD_UI}/applications/argocd/${appName}`);
                        }
                      } else {
                        degradedSince = 0;
                      }
                    }
                  }
                  if (Date.now() > deployDeadline) throw new Error(`${appName} is not Synced and Healthy ten minutes after the merge. ${ARGOCD_UI}/applications/argocd/${appName}`);
                  await sleep(5000);
                }
              };

              // the pull request the run opened: created while it ran
              const from = Date.parse(run.run_started_at ?? run.created_at) - 5000;
              const to = Date.parse(run.updated_at) + 120000;
              let pr: Pr | undefined;
              for (let i = 0; i < 4 && !pr; i++) {
                const prs = (await gh<Pr[]>(`/pulls?state=all&per_page=15&sort=created&direction=desc`)).body ?? [];
                pr = prs.find(p => p.head.ref.startsWith('selfservice/') && Date.parse(p.created_at) >= from && Date.parse(p.created_at) <= to);
                if (!pr) await sleep(2000);
              }
              if (pr) {
                ctx.output('pullRequestUrl', pr.html_url);
                ctx.output('pullRequestNumber', pr.number);
                ctx.logger.info(`✓ Pull request #${pr.number} opened: ${pr.html_url}`);
                await followDeployment(pr);
              } else {
                ctx.output('pullRequestUrl', run.html_url);
                ctx.output('argoUrl', `${ARGOCD_UI}/applications`);
                ctx.logger.info('The workflow finished without opening a pull request (nothing to change).');
              }
            },
          }),
        );
      },
    });
  },
});
