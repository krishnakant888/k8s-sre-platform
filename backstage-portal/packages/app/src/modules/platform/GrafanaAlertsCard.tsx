import { useEntity } from '@backstage/plugin-catalog-react';
import { InfoCard, Progress, ResponseErrorPanel, StatusError, StatusOK, StatusWarning } from '@backstage/core-components';
import { parseSelector, useProxy } from './proxy';

type Rule = { name: string; state?: string; alerts?: Array<{ state: string; labels: Record<string, string>; annotations?: Record<string, string>; activeAt?: string }> };
type Rules = { data?: { groups?: Array<{ name: string; rules: Rule[] }> } };

const GRAFANA_UI = 'https://grafana.localhost:9443';

/** The Grafana alerts that are firing or pending for the component: those whose labels match grafana/alert-label-selector. */
export function GrafanaAlertsCard() {
  const { entity } = useEntity();
  const selector = entity.metadata.annotations?.['grafana/alert-label-selector'];
  const { loading, error, value } = useProxy<Rules>('/grafana/api/prometheus/grafana/api/v1/rules', 'json');

  if (loading) return <Progress />;
  if (error) return <ResponseErrorPanel error={error} />;
  const want = parseSelector(selector);
  const active = (value?.data?.groups ?? [])
    .flatMap(g => g.rules)
    .flatMap(r => (r.alerts ?? []).map(a => ({ rule: r.name, ...a })))
    .filter(a => ['alerting', 'firing', 'pending'].includes(a.state.toLowerCase()) && want.every(([k, v]) => a.labels[k] === v));
  return (
    <InfoCard title="Grafana alerts" subheader={selector} deepLink={{ title: 'Open alerting in Grafana', link: `${GRAFANA_UI}/alerting/list` }}>
      {active.length === 0 ? (
        <StatusOK>No alert is firing or pending</StatusOK>
      ) : (
        <ul style={{ margin: 0, paddingLeft: 16 }}>
          {active.map((a, i) => (
            <li key={`${a.rule}-${i}`}>
              {a.state.toLowerCase() === 'pending' ? <StatusWarning>pending</StatusWarning> : <StatusError>firing</StatusError>} <b>{a.rule}</b>
              {a.annotations?.summary ? ` - ${a.annotations.summary}` : ''} {a.labels.severity ? `(${a.labels.severity})` : ''}
            </li>
          ))}
        </ul>
      )}
    </InfoCard>
  );
}
