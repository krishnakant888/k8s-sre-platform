import { createUnifiedTheme, genPageTheme } from '@backstage/theme';

/**
 * "Digital": a deep navy workspace with electric cyan and violet accents, a network-and-circuit pattern in the page headers and glassy,
 * softly glowing cards. A dark variant (default) and a light variant share the structure.
 */

// a tile of nodes and links, drawn in the accent colour at low opacity over the header gradient
const network = (stroke: string) =>
  `url("data:image/svg+xml;utf8,${encodeURIComponent(
    `<svg xmlns='http://www.w3.org/2000/svg' width='520' height='220' viewBox='0 0 520 220' fill='none' stroke='${stroke}' stroke-width='1.2'>` +
      `<g stroke-opacity='.34'><path d='M0 150 L70 110 L150 130 L230 70 L320 96 L410 40 L520 70'/><path d='M70 110 L96 190 L190 176 L150 130'/><path d='M230 70 L250 160 L350 180 L320 96'/>` +
      `<path d='M410 40 L470 130 L520 150'/><path d='M0 40 L60 30 L120 60 L150 20 L230 20'/><path d='M350 180 L430 200 L470 130'/></g>` +
      `<g fill='${stroke}' stroke='none' fill-opacity='.7'><circle cx='70' cy='110' r='3'/><circle cx='150' cy='130' r='3.5'/><circle cx='230' cy='70' r='4'/><circle cx='320' cy='96' r='3'/>` +
      `<circle cx='410' cy='40' r='4'/><circle cx='96' cy='190' r='3'/><circle cx='190' cy='176' r='3'/><circle cx='250' cy='160' r='3'/><circle cx='350' cy='180' r='3.5'/>` +
      `<circle cx='470' cy='130' r='3'/><circle cx='120' cy='60' r='3'/><circle cx='150' cy='20' r='3'/><circle cx='60' cy='30' r='2.5'/></g></svg>`,
  )}")`;

const FONT = '"Inter", "Segoe UI Variable", "Segoe UI", system-ui, -apple-system, Roboto, "Helvetica Neue", Arial, sans-serif';

const pageThemes = (shape: string) => ({
  home: genPageTheme({ colors: ['#071a52', '#0b4fd6', '#00c2ff'], shape }),
  documentation: genPageTheme({ colors: ['#0a1f5c', '#5a3df0', '#a35cff'], shape }),
  tool: genPageTheme({ colors: ['#06264d', '#0891b2', '#2ee6d6'], shape }),
  service: genPageTheme({ colors: ['#081c4d', '#1d4ed8', '#00d4ff'], shape }),
  website: genPageTheme({ colors: ['#0b1f4d', '#2563eb', '#7c5cff'], shape }),
  library: genPageTheme({ colors: ['#0a1d45', '#6d28d9', '#22d3ee'], shape }),
  other: genPageTheme({ colors: ['#0a1a40', '#334fb8', '#5aa9ff'], shape }),
  app: genPageTheme({ colors: ['#071a52', '#0b4fd6', '#00c2ff'], shape }),
  apis: genPageTheme({ colors: ['#0a1f5c', '#7c3aed', '#38bdf8'], shape }),
  card: genPageTheme({ colors: ['#0a1d45', '#1d4ed8', '#22d3ee'], shape }),
});

const common = (dark: boolean) => ({
  fontFamily: FONT,
  htmlFontSize: 16,
  typography: {
    htmlFontSize: 16,
    fontFamily: FONT,
    h1: { fontSize: 38, fontWeight: 800, letterSpacing: '-0.03em', marginBottom: 10 },
    h2: { fontSize: 30, fontWeight: 800, letterSpacing: '-0.025em', marginBottom: 8 },
    h3: { fontSize: 24, fontWeight: 700, letterSpacing: '-0.02em', marginBottom: 6 },
    h4: { fontSize: 20, fontWeight: 700, letterSpacing: '-0.015em', marginBottom: 6 },
    h5: { fontSize: 17, fontWeight: 700, marginBottom: 4 },
    h6: { fontSize: 15, fontWeight: 700, marginBottom: 2 },
  },
  components: {
    // the palette's background.paper does not reach the Material UI v4 surfaces, so the surface is set here
    MuiPaper: {
      styleOverrides: {
        root: { backgroundColor: dark ? '#0c1530' : '#ffffff', color: dark ? '#e8efff' : '#0b1533' },
        rounded: { borderRadius: 14 },
        elevation1: { boxShadow: dark ? '0 6px 24px rgba(0, 0, 0, 0.4)' : '0 4px 18px rgba(10, 30, 90, 0.08)' },
      },
    },
    MuiCard: {
      styleOverrides: {
        root: {
          borderRadius: 16,
          border: dark ? '1px solid rgba(0, 212, 255, 0.16)' : '1px solid rgba(0, 87, 255, 0.12)',
          boxShadow: dark ? '0 0 0 1px rgba(124, 92, 255, 0.05), 0 8px 30px rgba(0, 0, 0, 0.45), 0 0 28px rgba(0, 212, 255, 0.05)' : '0 6px 24px rgba(10, 30, 90, 0.08)',
          backgroundImage: dark ? 'linear-gradient(160deg, rgba(20, 34, 82, 0.55), rgba(10, 18, 46, 0.92))' : 'none',
          transition: 'border-color .2s, box-shadow .2s',
          '&:hover': { borderColor: dark ? 'rgba(0, 212, 255, 0.42)' : 'rgba(0, 87, 255, 0.35)' },
        },
      },
    },
    MuiCardHeader: {
      styleOverrides: { root: { borderBottom: dark ? '1px solid rgba(0, 212, 255, 0.12)' : '1px solid rgba(0, 87, 255, 0.10)' }, title: { fontWeight: 700 } },
    },
    MuiButton: {
      styleOverrides: {
        root: { borderRadius: 10, textTransform: 'none', fontWeight: 700, letterSpacing: '0.01em' },
        containedPrimary: {
          background: dark ? 'linear-gradient(135deg, #00c2ff 0%, #5a67ff 60%, #8b5cf6 100%)' : 'linear-gradient(135deg, #0057ff 0%, #5a3df0 100%)',
          color: '#ffffff',
          boxShadow: dark ? '0 4px 20px rgba(0, 194, 255, 0.28)' : '0 4px 16px rgba(0, 87, 255, 0.25)',
          '&:hover': { filter: 'brightness(1.1)', boxShadow: dark ? '0 6px 26px rgba(0, 194, 255, 0.42)' : '0 6px 22px rgba(0, 87, 255, 0.35)' },
        },
        outlinedPrimary: { borderColor: dark ? 'rgba(0, 212, 255, 0.5)' : undefined },
      },
    },
    MuiChip: { styleOverrides: { root: { borderRadius: 8, fontWeight: 600 } } },
    MuiTableHead: { styleOverrides: { root: { '& th': { fontWeight: 700, letterSpacing: '0.04em', textTransform: 'uppercase', fontSize: 12 } } } },
    MuiTableRow: { styleOverrides: { root: { '&:hover': { backgroundColor: dark ? 'rgba(0, 212, 255, 0.05)' : 'rgba(0, 87, 255, 0.04)' } } } },
    MuiTab: { styleOverrides: { root: { textTransform: 'none', fontWeight: 700 } } },
    MuiTabs: { styleOverrides: { indicator: { height: 3, borderRadius: 3, background: 'linear-gradient(90deg, #00d4ff, #7c5cff)' } } },
    MuiOutlinedInput: { styleOverrides: { root: { borderRadius: 10 } } },
    MuiLink: { styleOverrides: { root: { fontWeight: 600 } } },
    BackstageHeader: {
      styleOverrides: {
        header: { boxShadow: 'inset 0 -1px 0 rgba(0, 212, 255, 0.25), 0 10px 40px rgba(0, 20, 80, 0.35)' },
        title: { fontWeight: 800, letterSpacing: '-0.03em', textShadow: '0 2px 18px rgba(0, 212, 255, 0.35)' },
        subtitle: { opacity: 0.85, letterSpacing: '0.04em' },
      },
    },
    BackstageSidebarItem: {
      styleOverrides: {
        root: { borderRadius: 10, margin: '1px 8px', width: 'calc(100% - 16px)' },
        selected: { background: 'linear-gradient(90deg, rgba(0, 212, 255, 0.18), rgba(124, 92, 255, 0.08))' },
      },
    },
  },
});

export const digitalDark = createUnifiedTheme({
  ...common(true),
  palette: {
    type: 'dark',
    primary: { main: '#00d4ff', light: '#6fe7ff', dark: '#0099cc', contrastText: '#03101f' },
    secondary: { main: '#8b6cff', light: '#b3a0ff', dark: '#5a3df0' },
    error: { main: '#ff5470' },
    warning: { main: '#ffb020' },
    info: { main: '#5aa9ff' },
    success: { main: '#2ee6a8' },
    background: { default: '#050a1a', paper: '#0c1530' },
    text: { primary: '#e8efff', secondary: '#9db0dc' },
    divider: 'rgba(120, 150, 255, 0.16)',
    link: '#5fd8ff',
    linkHover: '#a6ecff',
    textVerySubtle: '#2a3a6b',
    textSubtle: '#7f93c4',
    highlight: '#ffdd57',
    border: 'rgba(120, 150, 255, 0.22)',
    banner: { info: '#1d4ed8', error: '#e11d48', text: '#ffffff', link: '#a6ecff', warning: '#d97706' },
    errorBackground: '#3a1020',
    warningBackground: '#3a2a08',
    infoBackground: '#0c2a54',
    errorText: '#ffd1da',
    infoText: '#cfe6ff',
    warningText: '#ffe6b0',
    status: { ok: '#2ee6a8', warning: '#ffb020', error: '#ff5470', running: '#00d4ff', pending: '#9db0dc', aborted: '#7f93c4' },
    bursts: { fontColor: '#ffffff', slackChannelText: '#9db0dc', backgroundColor: { default: '#0b4fd6' }, gradient: { linear: 'linear-gradient(90deg, #00d4ff, #7c5cff)' } },
    navigation: {
      background: '#040816',
      indicator: '#00d4ff',
      color: '#9db0dc',
      selectedColor: '#ffffff',
      navItem: { hoverBackground: 'rgba(0, 212, 255, 0.10)' },
      submenu: { background: '#0a1230' },
    },
    pinSidebarButton: { icon: '#040816', background: '#9db0dc' },
    tabbar: { indicator: '#00d4ff' },
  },
  defaultPageTheme: 'home',
  pageTheme: pageThemes(network('#7fe8ff')),
});

export const digitalLight = createUnifiedTheme({
  ...common(false),
  palette: {
    type: 'light',
    primary: { main: '#0057ff', light: '#4d86ff', dark: '#003fbd', contrastText: '#ffffff' },
    secondary: { main: '#6a3dff', light: '#9a7bff', dark: '#4a22d6' },
    error: { main: '#e11d48' },
    warning: { main: '#d97706' },
    info: { main: '#0284c7' },
    success: { main: '#059669' },
    background: { default: '#eef3ff', paper: '#ffffff' },
    text: { primary: '#0b1533', secondary: '#4a5b85' },
    divider: 'rgba(0, 40, 140, 0.12)',
    link: '#0057ff',
    linkHover: '#003fbd',
    textVerySubtle: '#dbe5ff',
    textSubtle: '#6b7ba6',
    highlight: '#ffdd57',
    border: 'rgba(0, 40, 140, 0.16)',
    banner: { info: '#1d4ed8', error: '#e11d48', text: '#ffffff', link: '#dbeafe', warning: '#d97706' },
    errorBackground: '#fde8ee',
    warningBackground: '#fef3dc',
    infoBackground: '#e0edff',
    errorText: '#8a1230',
    infoText: '#0b2f6b',
    warningText: '#7a4a06',
    status: { ok: '#059669', warning: '#d97706', error: '#e11d48', running: '#0057ff', pending: '#6b7ba6', aborted: '#6b7ba6' },
    bursts: { fontColor: '#ffffff', slackChannelText: '#4a5b85', backgroundColor: { default: '#0b4fd6' }, gradient: { linear: 'linear-gradient(90deg, #0057ff, #6a3dff)' } },
    navigation: {
      background: '#071238',
      indicator: '#00d4ff',
      color: '#b4c3ee',
      selectedColor: '#ffffff',
      navItem: { hoverBackground: 'rgba(0, 212, 255, 0.12)' },
      submenu: { background: '#0c1a4d' },
    },
    pinSidebarButton: { icon: '#071238', background: '#b4c3ee' },
    tabbar: { indicator: '#0057ff' },
  },
  defaultPageTheme: 'home',
  pageTheme: pageThemes(network('#bfe9ff')),
});
