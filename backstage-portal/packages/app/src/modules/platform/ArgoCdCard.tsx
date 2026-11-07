import { useEntity } from '@backstage/plugin-catalog-react';
import { InfoCard, Link, Progress, ResponseErrorPanel, StatusError, StatusOK, StatusPending, StatusRunning, StatusWarning } from '@backstage/core-components';
import { usePolledProxy } from './proxy';

type Application = {
  spec?: { destination?: { name?: string; namespace?: string }; project?: string };
  status?: {
    sync?: { status?: string };
    health?: { status?: string };
    operationState?: { phase?: string; message?: string; finishedAt?: string };
    history?: Array<{ deployedAt?: string; revision?: string; revisions?: string[] }>;
    resources?: Array<{ kind: string; name: string; namespace?: string; status?: string; health?: { status?: string; message?: string } }>;
  };
};

type TreeNode = {
  kind: string;
  name: string;
  namespace?: string;
  health?: { status?: string; message?: string };
  info?: Array<{ name: string; value: string }>;
  parentRefs?: Array<{ kind: string; name: string }>;
  createdAt?: string;
};

const ARGOCD_UI = 'https://argocd.localhost:7443';
const WORKLOADS = ['Deployment', 'StatefulSet', 'DaemonSet', 'Rollout'];
const infoOf = (n: TreeNode, key: string) => n.info?.find(i => i.name === key)?.value;

function Health({ value }: { value?: string }) {
  if (value === 'Healthy') return <StatusOK>Healthy</StatusOK>;
  if (value === 'Progressing') return <StatusRunning>Progressing</StatusRunning>;
  if (value === 'Suspended') return <StatusPending>Suspended</StatusPending>;
  if (value === 'Degraded' || value === 'Missing') return <StatusError>{value}</StatusError>;
  return <StatusWarning>{value ?? 'Unknown'}</StatusWarning>;
}

function Sync({ value }: { value?: string }) {
  if (value === 'Synced') return <StatusOK>Synced</StatusOK>;
  if (value === 'OutOfSync') return <StatusWarning>OutOfSync</StatusWarning>;
  return <StatusWarning>{value ?? 'Unknown'}</StatusWarning>;
}

/** Pods that belong to a workload: the tree links Pod -> ReplicaSet -> Deployment, so walk the parents. */
function podsOf(workload: TreeNode, nodes: TreeNode[]) {
  const owners = new Set<string>([`${workload.kind}/${workload.name}`]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const n of nodes) {
      const key = `${n.kind}/${n.name}`;
      if (!owners.has(key) && n.parentRefs?.some(p => owners.has(`${p.kind}/${p.name}`))) {
        owners.add(key);
        grew = true;
      }
    }
  }
  return nodes.filter(n => n.kind === 'Pod' && owners.has(`${n.kind}/${n.name}`));
}

/** Sync and health of the Argo CD application behind the component, and what runs now: its workloads and pods, refreshed every 10 seconds. */
export function ArgoCdCard() {
  const { entity } = useEntity();
  const name = entity.metadata.annotations?.['argocd/app-name'] ?? '';
  const enc = encodeURIComponent(name);
  const appState = usePolledProxy<Application>(`/argocd/api/v1/applications/${enc}`, 'json', 10000);
  const treeState = usePolledProxy<{ nodes?: TreeNode[] }>(`/argocd/api/v1/applications/${enc}/resource-tree`, 'json', 10000);

  if (appState.loading && !appState.value) return <Progress />;
  if (appState.error && !appState.value) return <ResponseErrorPanel error={appState.error} />;
  const app = appState.value!;
  const last = app.status?.history?.[app.status.history.length - 1];
  const revision = (last?.revisions?.find(r => /^[0-9a-f]{40}$/.test(r)) ?? last?.revision ?? '').slice(0, 9);
  const nodes = treeState.value?.nodes ?? [];
  const workloads = nodes.filter(n => WORKLOADS.includes(n.kind));
  const outOfSync = (app.status?.resources ?? []).filter(r => r.status && r.status !== 'Synced');
  const updated = appState.updated ? appState.updated.toLocaleTimeString() : '';

  return (
    <InfoCard
      title="Argo CD"
      subheader={`${name}${updated ? ` · updated ${updated}` : ''}`}
      deepLink={{ title: 'Open in Argo CD', link: `${ARGOCD_UI}/applications/argocd/${name}` }}
    >
      <table>
        <tbody>
          <tr><td>Sync&nbsp;&nbsp;</td><td><Sync value={app.status?.sync?.status} /></td></tr>
          <tr><td>Health&nbsp;&nbsp;</td><td><Health value={app.status?.health?.status} /></td></tr>
          <tr><td>Deploys into&nbsp;&nbsp;</td><td>{app.spec?.destination?.name ?? 'this cluster'} / {app.spec?.destination?.namespace}</td></tr>
          <tr><td>Last sync&nbsp;&nbsp;</td><td>{app.status?.operationState?.phase ?? 'none'} {app.status?.operationState?.finishedAt ?? ''}</td></tr>
          <tr><td>Revision&nbsp;&nbsp;</td><td>{revision ? <Link to={`https://github.com/krishnakant888/k8s-sre-platform/commit/${revision}`}>{revision}</Link> : 'unknown'}</td></tr>
        </tbody>
      </table>

      {outOfSync.length > 0 && (
        <p style={{ marginTop: 10 }}>
          <StatusWarning>Out of sync: {outOfSync.map(r => `${r.kind}/${r.name}`).join(', ')}</StatusWarning>
        </p>
      )}

      <h4 style={{ margin: '14px 0 6px' }}>Running now</h4>
      {treeState.error && !treeState.value && <span>Could not read the resource tree: {treeState.error.message}</span>}
      {workloads.length === 0 && !treeState.error && <span>{treeState.loading ? 'Loading' : 'No workload in this application.'}</span>}
      {workloads.map(w => {
        const pods = podsOf(w, nodes);
        const ready = pods.filter(p => p.health?.status === 'Healthy').length;
        return (
          <div key={`${w.kind}/${w.name}`} style={{ marginBottom: 10 }}>
            <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
              <strong>{w.kind} {w.name}</strong>
              <Health value={w.health?.status} />
              <span>{ready} of {pods.length} pods healthy</span>
            </div>
            {w.health?.message && <div style={{ opacity: 0.8 }}>{w.health.message}</div>}
            {pods.length > 0 && (
              <table style={{ width: '100%', marginTop: 4, borderCollapse: 'collapse' }}>
                <thead>
                  <tr style={{ textAlign: 'left', opacity: 0.7, fontSize: 12 }}>
                    <th>Pod</th><th>Status</th><th>Containers</th><th>Restarts</th><th>Node</th>
                  </tr>
                </thead>
                <tbody>
                  {pods.map(p => (
                    <tr key={p.name}>
                      <td style={{ paddingRight: 10 }}>{p.name}</td>
                      <td style={{ paddingRight: 10 }}>
                        {p.health?.status === 'Healthy' ? (
                          <StatusOK>{infoOf(p, 'Status Reason') ?? 'Running'}</StatusOK>
                        ) : p.health?.status === 'Progressing' ? (
                          <StatusRunning>{infoOf(p, 'Status Reason') ?? 'Starting'}</StatusRunning>
                        ) : (
                          <StatusError>{infoOf(p, 'Status Reason') ?? p.health?.status ?? 'Unknown'}</StatusError>
                        )}
                      </td>
                      <td style={{ paddingRight: 10 }}>{infoOf(p, 'Containers') ?? ''}</td>
                      <td style={{ paddingRight: 10 }}>{infoOf(p, 'Restart Count') ?? 0}</td>
                      <td>{infoOf(p, 'Node') ?? ''}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        );
      })}
    </InfoCard>
  );
}
