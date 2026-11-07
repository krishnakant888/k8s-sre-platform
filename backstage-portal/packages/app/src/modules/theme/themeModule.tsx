import { createFrontendModule } from '@backstage/frontend-plugin-api';
import { ThemeBlueprint } from '@backstage/plugin-app-react';
import { UnifiedThemeProvider } from '@backstage/theme';
import { consoleDark, consoleLight } from './consoleTheme';
import { ConsoleStyles } from './ConsoleStyles';
import { TerminalEffects } from './TerminalEffects';

// The portal's own themes. The two built-in ones are switched off in app-config (app.extensions: theme:app/light, theme:app/dark),
// so the person gets the dark one or the light one by their system setting, and can pick in Settings.
const dark = ThemeBlueprint.make({
  name: 'console-dark',
  params: {
    theme: {
      id: 'console-dark',
      title: 'Console (dark)',
      variant: 'dark',
      Provider: ({ children }) => (
        <UnifiedThemeProvider theme={consoleDark}>
          <ConsoleStyles />
          <TerminalEffects />
          {children}
        </UnifiedThemeProvider>
      ),
    },
  },
});

const light = ThemeBlueprint.make({
  name: 'console-light',
  params: {
    theme: {
      id: 'console-light',
      title: 'Console (light)',
      variant: 'light',
      Provider: ({ children }) => (
        <UnifiedThemeProvider theme={consoleLight}>
          <ConsoleStyles />
          <TerminalEffects />
          {children}
        </UnifiedThemeProvider>
      ),
    },
  },
});

export const themeModule = createFrontendModule({
  pluginId: 'app',
  extensions: [dark, light],
});
