import { createFrontendModule } from '@backstage/frontend-plugin-api';
import { EntityCardBlueprint } from '@backstage/plugin-catalog-react/alpha';
import { PageBlueprint } from '@backstage/frontend-plugin-api';
import PlaylistAddCheckIcon from '@material-ui/icons/PlaylistAddCheck';
import DashboardIcon from '@material-ui/icons/Dashboard';

const has = (annotation: string) => (entity: { metadata: { annotations?: Record<string, string> } }) => Boolean(entity.metadata.annotations?.[annotation]);

// The platform views on a component's overview page. Each shows only for components that carry its annotation.
const argoCdCard = EntityCardBlueprint.make({
  name: 'argocd',
  params: {
    type: 'info',
    filter: has('argocd/app-name'),
    loader: () => import('./ArgoCdCard').then(m => <m.ArgoCdCard />),
  },
});

const grafanaAlertsCard = EntityCardBlueprint.make({
  name: 'grafana-alerts',
  params: {
    type: 'info',
    filter: has('grafana/alert-label-selector'),
    loader: () => import('./GrafanaAlertsCard').then(m => <m.GrafanaAlertsCard />),
  },
});

const rcaReportsCard = EntityCardBlueprint.make({
  name: 'rca-reports',
  params: {
    type: 'info',
    filter: (entity: { kind: string }) => entity.kind.toLowerCase() === 'component',
    loader: () => import('./RcaReportsCard').then(m => <m.RcaReportsCard />),
  },
});

const k8sWorkload = (entity: { kind: string; metadata: { annotations?: Record<string, string> } }) =>
  entity.kind.toLowerCase() === 'component' && Boolean(entity.metadata.annotations?.['backstage.io/kubernetes-label-selector']);

const resourcesCard = EntityCardBlueprint.make({
  name: 'resources',
  params: {
    type: 'info',
    filter: k8sWorkload,
    loader: () => import('./ResourcesCard').then(m => <m.ResourcesCard />),
  },
});

const scorecardCard = EntityCardBlueprint.make({
  name: 'scorecard',
  params: {
    type: 'info',
    filter: k8sWorkload,
    loader: () => import('./ScorecardCard').then(m => <m.ScorecardCard />),
  },
});

// What was asked for from the templates: the runs of the selfservice workflow with their steps, and the pull requests they opened
const selfServicePage = PageBlueprint.make({
  name: 'self-service',
  params: {
    path: '/self-service',
    title: 'Requests',
    icon: <PlaylistAddCheckIcon />,
    loader: () => import('./SelfServicePage').then(m => <m.SelfServicePage />),
  },
});

// the landing page: the state of the platform at a glance
const overviewPage = PageBlueprint.make({
  name: 'overview',
  params: {
    path: '/',
    title: 'Overview',
    icon: <DashboardIcon />,
    loader: () => import('./OverviewPage').then(m => <m.OverviewPage />),
  },
});

export const platformModule = createFrontendModule({
  pluginId: 'catalog',
  extensions: [argoCdCard, grafanaAlertsCard, rcaReportsCard, resourcesCard, scorecardCard, selfServicePage, overviewPage],
});
