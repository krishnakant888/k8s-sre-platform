import { createApp } from '@backstage/frontend-defaults';
import catalogPlugin from '@backstage/plugin-catalog/alpha';
import { navModule } from './modules/nav';
import { homeModule } from './modules/home';
import { platformModule } from './modules/platform';
import { themeModule } from './modules/theme';

export default createApp({
  features: [catalogPlugin, navModule, homeModule, platformModule, themeModule],
});
