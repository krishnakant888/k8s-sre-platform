import { useEntity } from '@backstage/plugin-catalog-react';
import { InfoCard, Progress, ResponseErrorPanel } from '@backstage/core-components';
import { usePromQueries, workloadOf } from './prom';

const GRAFANA_UI = 'https://grafana.localhost:9443';

const mib = (b?: number) => (b === undefined ? 'n/a' : b >= 1024 ** 3 ? `${(b / 1024 ** 3).toFixed(2)} GiB` : `${Math.round(b / 1024 ** 2)} MiB`);
const cores = (c?: number) => (c === undefined ? 'n/a' : c < 1 ? `${Math.round(c * 1000)}m` : c.toFixed(2));

function Bar({ used, of, color }: { used?: number; of?: number; color: string }) {
  const pct = used !== undefined && of ? Math.min(100, (used / of) * 100) : 0;
  return (
    <div style={{ background: 'rgba(128,128,128,.25)', borderRadius: 4, height: 8, width: 160 }}>
      <div style={{ background: color, borderRadius: 4, height: 8, width: `${pct}%` }} />
    </div>
  );
}

/** What the component asks for and what it uses, so over-provisioned and nearly-full services stand out. From Mimir through the Grafana proxy. */
export function ResourcesCard() {
  const { entity } = useEntity();
  const w = workloadOf(entity);
  const sel = `namespace="${w.namespace}", pod=~"${w.pods}"`;
  const { loading, error, value: v } = usePromQueries({
    cpuUse: `sum(rate(container_cpu_usage_seconds_total{${sel}, container!=""}[5m]))`,
    cpuReq: `sum(kube_pod_container_resource_requests{${sel}, resource="cpu"})`,
    memUse: `sum(container_memory_working_set_bytes{${sel}, container!=""})`,
    memReq: `sum(kube_pod_container_resource_requests{${sel}, resource="memory"})`,
    memLim: `sum(kube_pod_container_resource_limits{${sel}, resource="memory"})`,
    replicas: `max(kube_deployment_spec_replicas{namespace="${w.namespace}", deployment="${w.name}"})`,
  });

  if (loading) return <Progress />;
  if (error) return <ResponseErrorPanel error={error} />;
  const r = v!;
  const hints: string[] = [];
  if (r.memUse !== undefined && r.memReq && r.memUse < r.memReq * 0.3) hints.push('Uses under 30% of the memory it requests: the request can be lowered.');
  if (r.memUse !== undefined && r.memLim && r.memUse > r.memLim * 0.85) hints.push('Uses over 85% of its memory limit: raise the limit before it is OOMKilled.');
  if (r.cpuUse !== undefined && r.cpuReq && r.cpuUse < r.cpuReq * 0.2) hints.push('Uses under 20% of the CPU it requests.');
  if (r.memLim === undefined && r.memUse !== undefined) hints.push('No memory limit: Kyverno requires one in demo.');
  return (
    <InfoCard
      title="Resources"
      subheader={`${w.namespace}/${w.name}, ${r.replicas ?? '?'} replicas (all pods together)`}
      deepLink={{ title: 'Workload dashboard', link: `${GRAFANA_UI}/d/obs-workload?var-namespace=${w.namespace}&var-workload=${w.name}` }}
    >
      {r.memUse === undefined && r.cpuUse === undefined ? (
        <span>No metrics for this workload.</span>
      ) : (
        <table style={{ borderSpacing: '0 6px' }}>
          <tbody>
            <tr>
              <td>CPU&nbsp;&nbsp;</td>
              <td><Bar used={r.cpuUse} of={r.cpuReq} color="#2e7d32" /></td>
              <td>&nbsp;&nbsp;{cores(r.cpuUse)} used of {cores(r.cpuReq)} requested</td>
            </tr>
            <tr>
              <td>Memory&nbsp;&nbsp;</td>
              <td><Bar used={r.memUse} of={r.memLim ?? r.memReq} color={r.memLim && r.memUse && r.memUse > r.memLim * 0.85 ? '#c62828' : '#1565c0'} /></td>
              <td>&nbsp;&nbsp;{mib(r.memUse)} used, {mib(r.memReq)} requested, limit {mib(r.memLim)}</td>
            </tr>
          </tbody>
        </table>
      )}
      {hints.length > 0 && (
        <ul style={{ margin: '8px 0 0', paddingLeft: 18 }}>
          {hints.map(h => <li key={h}>{h}</li>)}
        </ul>
      )}
    </InfoCard>
  );
}
