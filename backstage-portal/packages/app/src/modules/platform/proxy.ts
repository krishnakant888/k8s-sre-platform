import { useEffect, useState } from 'react';
import { discoveryApiRef, fetchApiRef, useApi } from '@backstage/frontend-plugin-api';

export type Loaded<T> = { loading: boolean; error?: Error; value?: T };

/**
 * Calls one of the portal backend's proxy endpoints (configured in app-config: /argocd, /grafana, /rca). The backend adds
 * the token or login of the target, so nothing secret reaches the browser.
 */
export function useProxy<T>(path: string, parse: 'json' | 'text'): Loaded<T> {
  const discovery = useApi(discoveryApiRef);
  const { fetch } = useApi(fetchApiRef);
  const [state, setState] = useState<Loaded<T>>({ loading: true });

  useEffect(() => {
    let cancelled = false;
    setState({ loading: true });
    (async () => {
      try {
        const base = await discovery.getBaseUrl('proxy');
        const res = await fetch(`${base}${path}`);
        if (!res.ok) {
          throw new Error(`${res.status} ${res.statusText} from ${path}`);
        }
        const value = (parse === 'json' ? await res.json() : await res.text()) as T;
        if (!cancelled) setState({ loading: false, value });
      } catch (e) {
        if (!cancelled) setState({ loading: false, error: e as Error });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [discovery, fetch, path, parse]);

  return state;
}

/** Like useProxy, refreshed every `everyMs`; keeps showing the last value while it refreshes and remembers when it last succeeded. */
export function usePolledProxy<T>(path: string, parse: 'json' | 'text', everyMs: number): Loaded<T> & { updated?: Date } {
  const discovery = useApi(discoveryApiRef);
  const { fetch } = useApi(fetchApiRef);
  const [state, setState] = useState<Loaded<T> & { updated?: Date }>({ loading: true });

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      try {
        const base = await discovery.getBaseUrl('proxy');
        const res = await fetch(`${base}${path}`);
        if (!res.ok) throw new Error(`${res.status} ${res.statusText} from ${path}`);
        const value = (parse === 'json' ? await res.json() : await res.text()) as T;
        if (!cancelled) setState({ loading: false, value, updated: new Date() });
      } catch (e) {
        if (!cancelled) setState(s => ({ ...s, loading: false, error: e as Error }));
      }
      if (!cancelled) timer = setTimeout(load, everyMs);
    };
    load();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [discovery, fetch, path, parse, everyMs]);

  return state;
}

/** "key=value,key2=value2" (the Grafana plugin's annotation format) as a list of pairs. */
export function parseSelector(selector?: string): Array<[string, string]> {
  return (selector ?? '')
    .split(',')
    .map(s => s.trim())
    .filter(Boolean)
    .map(s => {
      const i = s.indexOf('=');
      return [s.slice(0, i), s.slice(i + 1)] as [string, string];
    });
}
