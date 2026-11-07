import { createUnifiedTheme, genPageTheme } from '@backstage/theme';

/**
 * "Console": a calm, professional product look. Neutral slate surfaces, one indigo accent, thin borders, small radii, no glow and no gradients except
 * the page header. A light and a dark variant; the left navigation is dark slate in both.
 */

const FONT = '"Inter", "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, Roboto, "Helvetica Neue", Arial, sans-serif';

const pageThemes = (dark: boolean) => {
  const th = dark
    ? genPageTheme({ colors: ['#171c27', '#171c27'], shape: 'none', options: { fontColor: '#e6e9f0' } })
    : genPageTheme({ colors: ['#f3f4f6', '#f3f4f6'], shape: 'none', options: { fontColor: '#0f172a' } });
  return { home: th, documentation: th, tool: th, service: th, website: th, library: th, other: th, app: th, apis: th, card: th };
};

const components = (dark: boolean) => {
  const border = dark ? '#232a38' : '#e5e7eb';
  const surface = dark ? '#12161f' : '#ffffff';
  const text = dark ? '#e6e9f0' : '#0f172a';
  return {
    MuiPaper: {
      styleOverrides: {
        root: { backgroundColor: surface, color: text, backgroundImage: 'none' },
        rounded: { borderRadius: 12 },
        elevation1: { boxShadow: dark ? '0 1px 2px rgba(0, 0, 0, 0.4)' : '0 1px 2px rgba(16, 24, 40, 0.06)', border: `1px solid ${border}` },
      },
    },
    MuiCard: { styleOverrides: { root: { borderRadius: 12, border: `1px solid ${border}`, boxShadow: dark ? '0 1px 2px rgba(0, 0, 0, 0.4)' : '0 1px 2px rgba(16, 24, 40, 0.06)' } } },
    MuiCardHeader: { styleOverrides: { root: { borderBottom: `1px solid ${border}` }, title: { fontWeight: 600 } } },
    MuiButton: {
      styleOverrides: {
        root: { borderRadius: 8, textTransform: 'none', fontWeight: 600 },
        containedPrimary: { backgroundColor: dark ? '#6366f1' : '#4f46e5', color: '#ffffff', boxShadow: 'none', '&:hover': { backgroundColor: dark ? '#7578ff' : '#4338ca', boxShadow: 'none' } },
        outlinedPrimary: { borderColor: dark ? '#3b4256' : '#cbd5e1' },
      },
    },
    MuiChip: { styleOverrides: { root: { borderRadius: 6, fontWeight: 500 } } },
    MuiTableHead: { styleOverrides: { root: { '& th': { fontWeight: 600, letterSpacing: '0.04em', textTransform: 'uppercase', fontSize: 11.5 } } } },
    MuiTableRow: { styleOverrides: { root: { '&:hover': { backgroundColor: dark ? 'rgba(129, 140, 248, 0.06)' : 'rgba(79, 70, 229, 0.04)' } } } },
    MuiTab: { styleOverrides: { root: { textTransform: 'none', fontWeight: 600 } } },
    MuiTabs: { styleOverrides: { indicator: { height: 2, backgroundColor: dark ? '#818cf8' : '#4f46e5' } } },
    MuiOutlinedInput: { styleOverrides: { root: { borderRadius: 8 } } },
    MuiLink: { styleOverrides: { root: { fontWeight: 500 } } },
    BackstageHeader: {
      styleOverrides: {
        header: { boxShadow: 'none', borderBottom: dark ? '1px solid #232a38' : '1px solid #e5e7eb' },
        title: { fontWeight: 700, letterSpacing: '-0.02em' },
        subtitle: { opacity: 0.8 },
      },
    },
    BackstageSidebarItem: {
      styleOverrides: {
        root: { borderRadius: 8, margin: '1px 8px', width: 'calc(100% - 16px)' },
        selected: { background: 'rgba(129, 140, 248, 0.16)' },
      },
    },
  };
};

const common = (dark: boolean) => ({
  fontFamily: FONT,
  htmlFontSize: 16,
  typography: {
    htmlFontSize: 16,
    fontFamily: FONT,
    h1: { fontSize: 30, fontWeight: 700, letterSpacing: '-0.025em', marginBottom: 8 },
    h2: { fontSize: 24, fontWeight: 700, letterSpacing: '-0.02em', marginBottom: 8 },
    h3: { fontSize: 20, fontWeight: 650, letterSpacing: '-0.015em', marginBottom: 6 },
    h4: { fontSize: 17, fontWeight: 650, marginBottom: 6 },
    h5: { fontSize: 15, fontWeight: 650, marginBottom: 4 },
    h6: { fontSize: 14, fontWeight: 650, marginBottom: 2 },
  },
  components: components(dark),
  defaultPageTheme: 'home',
  pageTheme: pageThemes(dark),
});

const nav = {
  background: '#0b1120',
  indicator: '#818cf8',
  color: '#94a3b8',
  selectedColor: '#ffffff',
  navItem: { hoverBackground: 'rgba(255, 255, 255, 0.06)' },
  submenu: { background: '#111a30' },
};

export const consoleDark = createUnifiedTheme({
  ...common(true),
  palette: {
    type: 'dark',
    primary: { main: '#818cf8', light: '#a5b4fc', dark: '#6366f1', contrastText: '#0b0e14' },
    secondary: { main: '#a78bfa' },
    error: { main: '#f87171' },
    warning: { main: '#fbbf24' },
    info: { main: '#60a5fa' },
    success: { main: '#34d399' },
    background: { default: '#0b0e14', paper: '#12161f' },
    text: { primary: '#e6e9f0', secondary: '#8b95a8' },
    divider: '#232a38',
    link: '#a5b4fc',
    linkHover: '#c7d2fe',
    textVerySubtle: '#1d2330',
    textSubtle: '#6b7588',
    highlight: '#fbbf24',
    border: '#2a3242',
    banner: { info: '#3730a3', error: '#b91c1c', text: '#ffffff', link: '#c7d2fe', warning: '#b45309' },
    errorBackground: '#3b1418',
    warningBackground: '#3a2c0c',
    infoBackground: '#1a2146',
    errorText: '#fecaca',
    infoText: '#c7d2fe',
    warningText: '#fde68a',
    status: { ok: '#34d399', warning: '#fbbf24', error: '#f87171', running: '#818cf8', pending: '#8b95a8', aborted: '#6b7588' },
    bursts: { fontColor: '#ffffff', slackChannelText: '#8b95a8', backgroundColor: { default: '#4f46e5' }, gradient: { linear: 'linear-gradient(90deg, #4f46e5, #7c3aed)' } },
    navigation: nav,
    pinSidebarButton: { icon: '#0b1120', background: '#94a3b8' },
    tabbar: { indicator: '#818cf8' },
  },
});

export const consoleLight = createUnifiedTheme({
  ...common(false),
  palette: {
    type: 'light',
    primary: { main: '#4f46e5', light: '#6366f1', dark: '#4338ca', contrastText: '#ffffff' },
    secondary: { main: '#7c3aed' },
    error: { main: '#dc2626' },
    warning: { main: '#d97706' },
    info: { main: '#2563eb' },
    success: { main: '#16a34a' },
    background: { default: '#f6f7f9', paper: '#ffffff' },
    text: { primary: '#0f172a', secondary: '#64748b' },
    divider: '#e5e7eb',
    link: '#4f46e5',
    linkHover: '#4338ca',
    textVerySubtle: '#eef0f4',
    textSubtle: '#94a3b8',
    highlight: '#fde047',
    border: '#d1d5db',
    banner: { info: '#4f46e5', error: '#dc2626', text: '#ffffff', link: '#e0e7ff', warning: '#d97706' },
    errorBackground: '#fef2f2',
    warningBackground: '#fffbeb',
    infoBackground: '#eef2ff',
    errorText: '#991b1b',
    infoText: '#3730a3',
    warningText: '#92400e',
    status: { ok: '#16a34a', warning: '#d97706', error: '#dc2626', running: '#4f46e5', pending: '#94a3b8', aborted: '#94a3b8' },
    bursts: { fontColor: '#ffffff', slackChannelText: '#64748b', backgroundColor: { default: '#4f46e5' }, gradient: { linear: 'linear-gradient(90deg, #4f46e5, #7c3aed)' } },
    navigation: nav,
    pinSidebarButton: { icon: '#0b1120', background: '#94a3b8' },
    tabbar: { indicator: '#4f46e5' },
  },
});
