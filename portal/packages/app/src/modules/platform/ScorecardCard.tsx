import { useEffect, useState } from 'react';
import { useEntity } from '@backstage/plugin-catalog-react';
import { discoveryApiRef, fetchApiRef, useApi } from '@backstage/frontend-plugin-api';
import { InfoCard, Progress, StatusError, StatusOK, StatusWarning } from '@backstage/core-components';
import { usePromQueries, workloadOf } from './prom';

type Check = { label: string; ok?: boolean; detail: string };

/** Fetches what the checks need from Argo CD and Grafana through the portal proxy; a failed call leaves its checks as "could not check". */
function useProxyChecks(app: string, name: string) {
  const discovery = useApi(discoveryApiRef);
  const { fetch } = useApi(fetchApiRef);
  const [state, setState] = useState<{ done: boolean; argo?: { sync?: string; health?: string }; ownRule?: boolean; dashboards?: number }>({ done: false });
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const base = await discovery.getBaseUrl('proxy');
      const get = async (path: string) => {
        try {
          const res = await fetch(`${base}${path}`);
          return res.ok ? await res.json() : undefined;
        } catch {
          return undefined;
        }
      };
      const [argo, rules, dash] = await Promise.all([
        app ? get(`/argocd/api/v1/applications/${encodeURIComponent(app)}`) : undefined,
        get('/grafana/api/prometheus/grafana/api/v1/rules'),
        get(`/grafana/api/search?type=dash-db&query=${encodeURIComponent(name)}`),
      ]);
      if (cancelled) return;
      const allRules: Array<{ labels?: Record<string, string> }> = (rules?.data?.groups ?? []).flatMap((g: { rules?: unknown[] }) => g.rules ?? []);
      setState({
        done: true,
        argo: argo ? { sync: argo.status?.sync?.status, health: argo.status?.health?.status } : undefined,
        ownRule: rules ? allRules.some(r => r.labels?.service === name) : undefined,
        dashboards: Array.isArray(dash) ? dash.length : undefined,
      });
    })();
    return () => {
      cancelled = true;
    };
  }, [discovery, fetch, app, name]);
  return state;
}

/** How well a component is set up to be run: the things the platform expects of every service, each checked from the catalog entry or the live data. */
export function ScorecardCard() {
  const { entity } = useEntity();
  const w = workloadOf(entity);
  const app = entity.metadata.annotations?.['argocd/app-name'] ?? '';
  const proxy = useProxyChecks(app, w.name);
  const sel = `namespace="${w.namespace}"`;
  const prom = usePromQueries({
    memLim: `sum(kube_pod_container_resource_limits{${sel}, pod=~"${w.pods}", resource="memory"})`,
    replicas: `max(kube_deployment_spec_replicas{${sel}, deployment="${w.name}"})`,
    pdb: `max(kube_poddisruptionbudget_status_desired_healthy{${sel}, poddisruptionbudget="${w.name}"})`,
  });

  if (!proxy.done || prom.loading) return <Progress />;
  const p = prom.value;
  const owner = (entity.spec?.owner as string | undefined) ?? '';
  const links = entity.metadata.links ?? [];
  const checks: Check[] = [
    { label: 'Has an owner', ok: Boolean(owner) && owner !== 'unknown', detail: owner || 'none' },
    { label: 'Has a runbook', ok: links.some(l => /runbook/i.test(`${l.title ?? ''} ${l.url}`)), detail: 'a link named Runbook on the component' },
    { label: 'Deployed by Argo CD and healthy', ok: proxy.argo ? proxy.argo.sync === 'Synced' && proxy.argo.health === 'Healthy' : undefined, detail: proxy.argo ? `${proxy.argo.sync}, ${proxy.argo.health}` : 'could not read Argo CD' },
    { label: 'Has an alert rule of its own', ok: proxy.ownRule, detail: 'a Grafana rule labelled service=' + w.name + ' (the generic rules cover it anyway)' },
    { label: 'Has a dashboard', ok: proxy.dashboards === undefined ? undefined : proxy.dashboards > 0, detail: 'a Grafana dashboard named after it' },
    { label: 'Sets a memory limit', ok: p ? (p.memLim ?? 0) > 0 : undefined, detail: 'Kyverno requires it in demo' },
    { label: 'Runs more than one replica', ok: p ? (p.replicas ?? 0) >= 2 : undefined, detail: p?.replicas === undefined ? 'no Deployment found' : `${p.replicas} replicas` },
    { label: 'Has a PodDisruptionBudget', ok: p ? p.pdb !== undefined : undefined, detail: 'keeps a node drain from taking every replica' },
  ];
  const known = checks.filter(c => c.ok !== undefined);
  const passed = known.filter(c => c.ok).length;
  return (
    <InfoCard title="Scorecard" subheader={`${passed} of ${known.length} checks pass${known.length < checks.length ? ` (${checks.length - known.length} could not be checked)` : ''}`}>
      <table style={{ borderSpacing: '0 4px' }}>
        <tbody>
          {checks.map(c => (
            <tr key={c.label} title={c.detail}>
              <td>{c.ok === undefined ? <StatusWarning /> : c.ok ? <StatusOK /> : <StatusError />}</td>
              <td>{c.label}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </InfoCard>
  );
}
