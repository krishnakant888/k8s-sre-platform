import { createFrontendModule } from '@backstage/frontend-plugin-api';
import { HomePageWidgetBlueprint } from '@backstage/plugin-home-react/alpha';
import { MarkdownContent } from '@backstage/core-components';

const content = `
## SRE Platform Portal

One place for what runs on the platform, who owns it and how it is doing.

- **Catalog**: every service, the three clusters and the platform components, with owner and links - open the [Software Catalog](/catalog)
- **Create**: [New service on the platform](/create) generates the manifests for a service that already fits the Kyverno policies, Pod Security and the gateway
- **On a component's page**: its pods in the three clusters (Kubernetes tab), the Argo CD sync and health, the Grafana alerts that are firing and its RCA reports

### Tools

- [Grafana](https://grafana.localhost:9443) dashboards and alerting
- [Argo CD](https://argocd.localhost:7443) deployments
- [Argo Workflows](https://workflows.localhost:9443) investigations
- [RCA reports](https://reports.localhost:9443) written by the SRE agent
`;

const gettingStartedWidget = HomePageWidgetBlueprint.make({
  name: 'getting-started',
  params: {
    name: 'GettingStarted',
    title: 'Getting Started',
    description: 'Tips and links to help you get started with Backstage',
    components: async () => ({
      Content: () => <MarkdownContent content={content} />,
    }),
  },
});

export const homeModule = createFrontendModule({
  pluginId: 'home',
  extensions: [gettingStartedWidget],
});
