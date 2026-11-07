import { useEntity } from '@backstage/plugin-catalog-react';
import { InfoCard, Link, Progress, ResponseErrorPanel } from '@backstage/core-components';
import { useProxy } from './proxy';

const REPORTS_UI = 'https://reports.localhost:9443';

/** The RCA reports the SRE agent wrote for the component. Report ids are rca-<service>-<yyyymmdd>-<hhmmss> (annotation rca/service-name overrides the service). */
export function RcaReportsCard() {
  const { entity } = useEntity();
  const service = entity.metadata.annotations?.['rca/service-name'] ?? entity.metadata.name;
  const { loading, error, value } = useProxy<string>('/rca/', 'text');

  if (loading) return <Progress />;
  if (error) return <ResponseErrorPanel error={error} />;
  const rx = new RegExp(`href="(rca-${service.replace(/[^a-z0-9-]/gi, '')}-(\\d{8})-(\\d{6}))/"`, 'g');
  const reports = Array.from((value ?? '').matchAll(rx))
    .map(m => ({ id: m[1], when: `${m[2].slice(0, 4)}-${m[2].slice(4, 6)}-${m[2].slice(6, 8)} ${m[3].slice(0, 2)}:${m[3].slice(2, 4)}:${m[3].slice(4, 6)} UTC` }))
    .sort((a, b) => b.id.localeCompare(a.id));
  return (
    <InfoCard title="RCA reports" subheader={`${reports.length} for ${service}`} deepLink={{ title: 'Open the report viewer', link: REPORTS_UI }}>
      {reports.length === 0 ? (
        <span>No report for this component yet.</span>
      ) : (
        <ul style={{ margin: 0, paddingLeft: 16 }}>
          {reports.slice(0, 6).map(r => (
            <li key={r.id}>
              <Link to={`${REPORTS_UI}/#${r.id}`}>{r.when}</Link>
            </li>
          ))}
        </ul>
      )}
    </InfoCard>
  );
}
