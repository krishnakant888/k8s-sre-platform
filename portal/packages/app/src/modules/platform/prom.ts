import { useEffect, useState } from 'react';
import { discoveryApiRef, fetchApiRef, useApi } from '@backstage/frontend-plugin-api';
import type { Loaded } from './proxy';

/**
 * Instant PromQL queries against Mimir, through the portal backend's /grafana proxy (Grafana's datasource proxy, with the portal's read-only token).
 * Returns the first value of each query, or undefined when it has no data.
 */
export function usePromQueries(queries: Record<string, string>): Loaded<Record<string, number | undefined>> {
  const discovery = useApi(discoveryApiRef);
  const { fetch } = useApi(fetchApiRef);
  const [state, setState] = useState<Loaded<Record<string, number | undefined>>>({ loading: true });
  const key = JSON.stringify(queries);

  useEffect(() => {
    let cancelled = false;
    setState({ loading: true });
    (async () => {
      try {
        const base = await discovery.getBaseUrl('proxy');
        const out: Record<string, number | undefined> = {};
        await Promise.all(
          Object.entries(queries).map(async ([name, query]) => {
            const res = await fetch(`${base}/grafana/api/datasources/proxy/uid/mimir/api/v1/query?query=${encodeURIComponent(query)}`);
            if (!res.ok) throw new Error(`${res.status} ${res.statusText} for ${name}`);
            const body = await res.json();
            const v = body?.data?.result?.[0]?.value?.[1];
            out[name] = v === undefined ? undefined : Number(v);
          }),
        );
        if (!cancelled) setState({ loading: false, value: out });
      } catch (e) {
        if (!cancelled) setState({ loading: false, error: e as Error });
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [discovery, fetch, key]);

  return state;
}

/** The workload a component stands for: namespace and the app.kubernetes.io/name of its label selector. */
export function workloadOf(entity: { metadata: { name: string; annotations?: Record<string, string> } }) {
  const a = entity.metadata.annotations ?? {};
  const selector = a['backstage.io/kubernetes-label-selector'] ?? '';
  const name = /app\.kubernetes\.io\/name=([\w.-]+)/.exec(selector)?.[1] ?? entity.metadata.name;
  return { name, namespace: a['backstage.io/kubernetes-namespace'] ?? 'default', pods: `${name}-[a-z0-9]+-[a-z0-9]+` };
}
