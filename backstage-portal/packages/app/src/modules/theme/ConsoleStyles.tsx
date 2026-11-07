/**
 * Global styling of the "Console" look, on top of the Material UI theme: the Backstage UI components (cards, tabs, buttons) read CSS variables that are
 * set here per mode, the page and form surfaces are flat, the templates page gets a wide search bar on top, and the overview page has its own classes (pc-*).
 */
const CSS = `
[data-theme-mode='dark'] {
  --pc-bg: #0b0e14; --pc-surface: #12161f; --pc-surface-2: #171c27; --pc-border: #232a38; --pc-border-2: #2f3849; --pc-text: #e6e9f0; --pc-muted: #8b95a8; --pc-accent: #818cf8; --pc-accent-solid: #6366f1; --pc-accent-soft: rgba(129, 140, 248, 0.14);
  --pc-ok: #34d399; --pc-warn: #fbbf24; --pc-bad: #f87171; --pc-shadow: 0 1px 2px rgba(0, 0, 0, 0.4);
  --bui-bg-app: #0b0e14; --bui-bg-neutral-1: #12161f; --bui-bg-neutral-2: #171c27; --bui-bg-neutral-3: #1d2330; --bui-border-1: #232a38; --bui-border-2: #2f3849;
  --bui-fg-primary: #e6e9f0; --bui-fg-secondary: #8b95a8; --bui-bg-solid: #6366f1; --bui-bg-solid-hover: #7578ff; --bui-bg-solid-pressed: #4f46e5; --bui-fg-solid: #ffffff;
}
[data-theme-mode='light'] {
  --pc-bg: #f6f7f9; --pc-surface: #ffffff; --pc-surface-2: #f3f4f6; --pc-border: #e5e7eb; --pc-border-2: #d1d5db; --pc-text: #0f172a; --pc-muted: #64748b; --pc-accent: #4f46e5; --pc-accent-solid: #4f46e5; --pc-accent-soft: #eef2ff;
  --pc-ok: #16a34a; --pc-warn: #d97706; --pc-bad: #dc2626; --pc-shadow: 0 1px 2px rgba(16, 24, 40, 0.06);
  --bui-bg-app: #f6f7f9; --bui-bg-neutral-1: #ffffff; --bui-bg-neutral-2: #f3f4f6; --bui-bg-neutral-3: #e5e7eb; --bui-border-1: #e5e7eb; --bui-border-2: #d1d5db;
  --bui-fg-primary: #0f172a; --bui-fg-secondary: #64748b; --bui-bg-solid: #4f46e5; --bui-bg-solid-hover: #4338ca; --bui-bg-solid-pressed: #3730a3; --bui-fg-solid: #ffffff;
}
html, body, #root { background: var(--pc-bg) !important; }
main, [class*='BackstagePage-root'], [class*='MuiContainer-root'] { background: transparent !important; }
body { -webkit-font-smoothing: antialiased; }

/* surfaces: flat, thin border */
.MuiPaper-root:not(.MuiAppBar-root):not([class*='BackstageHeader']):not(.MuiMenu-paper):not(.MuiPopover-paper) { background-color: var(--pc-surface) !important; background-image: none !important; color: var(--pc-text); }
.MuiPaper-root.MuiPaper-elevation1, .MuiCard-root, .bui-Card { border: 1px solid var(--pc-border) !important; box-shadow: var(--pc-shadow) !important; border-radius: 12px !important; }
.bui-Card { background: var(--pc-surface) !important; background-image: none !important; }
.MuiPopover-paper, .MuiMenu-paper, .MuiAutocomplete-paper { background-color: var(--pc-surface) !important; border: 1px solid var(--pc-border-2); box-shadow: 0 8px 24px rgba(16, 24, 40, 0.18) !important; }
.MuiTableCell-root { border-color: var(--pc-border) !important; }
.MuiDivider-root, hr { background-color: var(--pc-border); }

/* inputs and forms */
.MuiOutlinedInput-root { background: var(--pc-surface) !important; border-radius: 8px !important; }
.MuiOutlinedInput-notchedOutline { border-color: var(--pc-border-2) !important; }
.MuiOutlinedInput-root:hover .MuiOutlinedInput-notchedOutline { border-color: var(--pc-muted) !important; }
.MuiOutlinedInput-root.Mui-focused .MuiOutlinedInput-notchedOutline { border-color: var(--pc-accent-solid) !important; border-width: 1.5px !important; }
.MuiOutlinedInput-root.Mui-focused { box-shadow: 0 0 0 3px var(--pc-accent-soft); }
.MuiFormLabel-root { color: var(--pc-muted) !important; font-weight: 500; }
.MuiFormLabel-root.Mui-focused { color: var(--pc-accent) !important; }
.MuiFormHelperText-root { color: var(--pc-muted) !important; }
.MuiInputBase-input { color: var(--pc-text) !important; }
form.rjsf .MuiFormControl-root { margin-bottom: 6px; }
form.rjsf label.MuiFormLabel-root { font-size: 13px; }
.MuiStepper-root { background: transparent !important; }
.MuiStepIcon-root { color: var(--pc-border-2); }
.MuiStepIcon-root.MuiStepIcon-active, .MuiStepIcon-root.MuiStepIcon-completed { color: var(--pc-accent-solid) !important; }
.MuiStepLabel-label { color: var(--pc-muted); font-weight: 500; }
.MuiStepLabel-label.MuiStepLabel-active, .MuiStepLabel-label.MuiStepLabel-completed { color: var(--pc-text); font-weight: 600; }
.bui-Button { border-radius: 8px !important; font-weight: 600 !important; }

/* tabs as buttons */
.bui-TabList { display: flex !important; flex-wrap: wrap; gap: 8px !important; padding: 10px 0 14px !important; border-bottom: 0 !important; box-shadow: none !important; }
.bui-Tab { display: inline-flex !important; align-items: center; padding: 8px 16px !important; border-radius: 8px !important; font-weight: 600 !important; text-decoration: none !important; border: 1px solid var(--pc-border) !important;
  background: var(--pc-surface) !important; color: var(--pc-muted) !important; transition: background 0.15s, color 0.15s, border-color 0.15s; }
.bui-Tab:hover { color: var(--pc-text) !important; border-color: var(--pc-border-2) !important; }
.bui-Tab[aria-selected='true'] { background: var(--pc-accent-solid) !important; color: #ffffff !important; border-color: transparent !important; }
.bui-TabList [class*='Indicator'], .bui-TabList [class*='indicator'], .bui-Tabs [class*='TabActive'] { display: none !important; }

/* the templates page: search on top across the page, the filters in a row, then the cards */
.MuiGrid-container > .MuiGrid-item:has(.MuiToolbar-root input) { flex-basis: 100% !important; max-width: 100% !important; display: flex !important; flex-wrap: wrap; gap: 14px; align-items: flex-end; padding-bottom: 6px !important; }
.MuiGrid-container > .MuiGrid-item:has(.MuiToolbar-root input) ~ .MuiGrid-item { flex-basis: 100% !important; max-width: 100% !important; }
.MuiGrid-item:has(.MuiToolbar-root input) > .MuiToolbar-root { flex: 1 1 100%; order: -1; padding: 0 !important; min-height: 0; }
.MuiGrid-item:has(.MuiToolbar-root input) > .MuiToolbar-root .MuiFormControl-root { width: 100%; }
.MuiGrid-item:has(.MuiToolbar-root input) > .MuiToolbar-root .MuiInputBase-root { height: 48px; padding: 0 14px; border: 1px solid var(--pc-border-2); border-radius: 10px; background: var(--pc-surface); box-shadow: var(--pc-shadow); }
.MuiGrid-item:has(.MuiToolbar-root input) > .MuiToolbar-root .MuiInputBase-root::before, .MuiGrid-item:has(.MuiToolbar-root input) > .MuiToolbar-root .MuiInputBase-root::after { display: none; }
.MuiGrid-item:has(.MuiToolbar-root input) > .MuiToolbar-root .MuiInputBase-root:focus-within { border-color: var(--pc-accent-solid); box-shadow: 0 0 0 3px var(--pc-accent-soft); }
.MuiGrid-item:has(.MuiToolbar-root input) > .MuiToolbar-root input { font-size: 15px; }
.MuiGrid-item:has(.MuiToolbar-root input) > .MuiPaper-root { display: none; }
.MuiGrid-item:has(.MuiToolbar-root input) > div:not(.MuiToolbar-root):not(.MuiPaper-root) { flex: 0 1 230px; }

/* template cards: a clean header instead of a coloured banner */
.bui-Card { transition: border-color 0.15s, box-shadow 0.15s, transform 0.15s; }
.bui-Card:hover { border-color: var(--pc-accent-solid) !important; box-shadow: 0 6px 20px rgba(16, 24, 40, 0.12) !important; transform: translateY(-2px); }
.bui-CardHeader { background: var(--pc-surface-2) !important; background-image: none !important; border-bottom: 1px solid var(--pc-border); color: var(--pc-text) !important; }
.bui-CardHeader * { color: var(--pc-text) !important; text-shadow: none !important; }
.bui-CardHeader svg { color: var(--pc-accent) !important; }
[class*='MuiChip-root'], [class*='bui-Tag'] { background: var(--pc-surface-2) !important; color: var(--pc-muted) !important; border: 1px solid var(--pc-border); }

/* overview page */
.pc-page { padding: 6px 4px 40px; color: var(--pc-text); }
.pc-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; flex-wrap: wrap; margin: 8px 0 22px; }
.pc-head h1 { margin: 0; font-size: 28px; font-weight: 700; letter-spacing: -0.025em; }
.pc-head p { margin: 4px 0 0; color: var(--pc-muted); font-size: 14px; }
.pc-cta { display: inline-flex; align-items: center; gap: 8px; padding: 9px 16px; border-radius: 8px; background: var(--pc-accent-solid); color: #fff !important; font-weight: 600; text-decoration: none !important; }
.pc-cta:hover { filter: brightness(1.08); }
.pc-kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; margin-bottom: 18px; }
.pc-kpi { background: var(--pc-surface); border: 1px solid var(--pc-border); border-radius: 12px; padding: 16px 18px; box-shadow: var(--pc-shadow); }
.pc-kpi .l { font-size: 12px; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase; color: var(--pc-muted); }
.pc-kpi .v { font-size: 34px; font-weight: 700; letter-spacing: -0.03em; line-height: 1.15; margin-top: 6px; }
.pc-kpi .v small { font-size: 16px; font-weight: 600; color: var(--pc-muted); margin-left: 4px; }
.pc-kpi .s { margin-top: 4px; font-size: 13px; color: var(--pc-muted); }
.pc-kpi.ok .v { color: var(--pc-ok); } .pc-kpi.warn .v { color: var(--pc-warn); } .pc-kpi.bad .v { color: var(--pc-bad); }
.pc-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 14px; align-items: start; }
.pc-card { background: var(--pc-surface); border: 1px solid var(--pc-border); border-radius: 12px; box-shadow: var(--pc-shadow); overflow: hidden; }
.pc-card > header { display: flex; align-items: center; justify-content: space-between; padding: 14px 18px; border-bottom: 1px solid var(--pc-border); }
.pc-card > header h3 { margin: 0; font-size: 14px; font-weight: 650; }
.pc-card > header a { font-size: 13px; font-weight: 600; color: var(--pc-accent); text-decoration: none; }
.pc-card .empty { padding: 22px 18px; color: var(--pc-muted); font-size: 14px; }
.pc-row { display: flex; align-items: center; gap: 12px; padding: 11px 18px; border-bottom: 1px solid var(--pc-border); font-size: 14px; }
.pc-row:last-child { border-bottom: 0; }
.pc-row .grow { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pc-row .muted { color: var(--pc-muted); font-size: 12.5px; white-space: nowrap; }
.pc-row a { color: var(--pc-text); font-weight: 500; text-decoration: none; } .pc-row a:hover { color: var(--pc-accent); }
.pc-pill { display: inline-flex; align-items: center; gap: 6px; padding: 2px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; white-space: nowrap; border: 1px solid transparent; }
.pc-pill::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.pc-pill.ok { color: var(--pc-ok); background: color-mix(in srgb, var(--pc-ok) 14%, transparent); }
.pc-pill.warn { color: var(--pc-warn); background: color-mix(in srgb, var(--pc-warn) 16%, transparent); }
.pc-pill.bad { color: var(--pc-bad); background: color-mix(in srgb, var(--pc-bad) 14%, transparent); }
.pc-pill.run { color: var(--pc-accent); background: var(--pc-accent-soft); }
.pc-pill.run::before { animation: pc-pulse 1.2s ease-in-out infinite; }
.pc-pill.idle { color: var(--pc-muted); background: var(--pc-surface-2); }
@keyframes pc-pulse { 50% { opacity: 0.25; } }
.pc-updated { font-size: 12.5px; color: var(--pc-muted); }

/* form fields: boxed, the label above */
form.rjsf .MuiInputLabel-formControl { position: static !important; transform: none !important; display: block; margin-bottom: 6px; font-size: 13px; font-weight: 600; color: var(--pc-text) !important; }
form.rjsf .MuiInput-formControl { margin-top: 0 !important; }
form.rjsf .MuiInput-root { border: 1px solid var(--pc-border-2); border-radius: 8px; padding: 6px 12px; background: var(--pc-surface); }
form.rjsf .MuiInput-root:focus-within { border-color: var(--pc-accent-solid); box-shadow: 0 0 0 3px var(--pc-accent-soft); }
form.rjsf .MuiInput-underline::before, form.rjsf .MuiInput-underline::after { display: none; }
form.rjsf .MuiSelect-select:focus { background: transparent; }
form.rjsf .MuiFormControl-root { margin-bottom: 14px; }
form.rjsf .MuiFormHelperText-root { margin-top: 5px; font-size: 12.5px; }

/* the Tasks tab (/create/tasks): the owner filter as a row on top, the table across the page, nothing wrapped letter by letter */
[data-pc-route='/create/tasks'] .MuiGrid-container { flex-direction: column; flex-wrap: nowrap; }
[data-pc-route='/create/tasks'] .MuiGrid-container > .MuiGrid-item { flex-basis: auto !important; max-width: 100% !important; width: 100% !important; }
[data-pc-route='/create/tasks'] .MuiGrid-item:has(.MuiCard-root):not(:has(table)) .MuiCard-root { display: flex; align-items: center; gap: 14px; padding: 8px 14px; width: fit-content; }
[data-pc-route='/create/tasks'] .MuiGrid-item:has(.MuiCard-root):not(:has(table)) .MuiList-root { display: flex; flex-direction: row; gap: 6px; padding: 0; }
[data-pc-route='/create/tasks'] .MuiGrid-item:has(.MuiCard-root):not(:has(table)) .MuiListItem-root { border-radius: 8px; width: auto; padding: 6px 14px; }
[data-pc-route='/create/tasks'] .MuiGrid-item:has(table) > .MuiBox-root { width: 100%; }
[data-pc-route='/create/tasks'] .MuiGrid-item:has(table) > .MuiBox-root > .MuiPaper-root { flex: 1 1 100% !important; width: 100% !important; max-width: 100% !important; }
[data-pc-route='/create/tasks'] table td, [data-pc-route='/create/tasks'] table th { white-space: nowrap; padding: 12px 16px; }
[data-pc-route='/create/tasks'] table td a { color: var(--pc-accent); font-weight: 500; }
[data-pc-route='/create/tasks'] table td:first-child a { display: inline-block; max-width: 150px; overflow: hidden; text-overflow: ellipsis; vertical-align: bottom; font-family: ui-monospace, Consolas, monospace; font-size: 13px; }

/* the catalog (/catalog): the filters in a row on top, the table across the whole page, a wide search bar */
[data-pc-route='/catalog'] .MuiGrid-container { flex-direction: column; flex-wrap: nowrap; }
[data-pc-route='/catalog'] .MuiGrid-container > .MuiGrid-item { flex-basis: auto !important; max-width: 100% !important; width: 100% !important; }
[data-pc-route='/catalog'] .MuiGrid-container > .MuiGrid-item:not(:has(table)) { display: flex !important; flex-wrap: wrap; gap: 14px; align-items: flex-end; }
[data-pc-route='/catalog'] .MuiGrid-container > .MuiGrid-item:not(:has(table)) > * { flex: 0 1 220px; min-width: 160px; margin: 0 !important; }
[data-pc-route='/catalog'] .MuiGrid-container > .MuiGrid-item:not(:has(table)) > .MuiPaper-root { flex: 0 0 240px; align-self: stretch; }
[data-pc-route='/catalog'] .MuiGrid-item:has(table) > *, [data-pc-route='/catalog'] .MuiGrid-item:has(table) .MuiPaper-root { width: 100% !important; max-width: 100% !important; }
[data-pc-route='/catalog'] .MuiToolbar-root .MuiFormControl-root { flex: 1 1 auto; }
[data-pc-route='/catalog'] .MuiToolbar-root .MuiInputBase-root { height: 44px; padding: 0 14px; border: 1px solid var(--pc-border-2); border-radius: 10px; background: var(--pc-surface); }
[data-pc-route='/catalog'] .MuiToolbar-root .MuiInputBase-root::before, [data-pc-route='/catalog'] .MuiToolbar-root .MuiInputBase-root::after { display: none; }
[data-pc-route='/catalog'] .MuiToolbar-root .MuiInputBase-root:focus-within { border-color: var(--pc-accent-solid); box-shadow: 0 0 0 3px var(--pc-accent-soft); }
[data-pc-route='/catalog'] table td, [data-pc-route='/catalog'] table th { white-space: nowrap; padding: 12px 16px; }
[data-pc-route='/catalog'] table td:nth-child(6) { white-space: normal; max-width: 360px; }

/* catalog, tidier: the filters on an even grid, the personal filter as one compact bar, the table on the whole width */
[data-pc-route='/catalog'] .MuiGrid-container > .MuiGrid-item:not(:has(table)) { display: grid !important; grid-template-columns: repeat(auto-fill, minmax(165px, 1fr)); gap: 14px 14px; align-items: end; padding-bottom: 8px !important; }
[data-pc-route='/catalog'] .MuiGrid-container > .MuiGrid-item:not(:has(table)) > * { flex: none; min-width: 0; width: auto; }
[data-pc-route='/catalog'] .MuiGrid-container > .MuiGrid-item:not(:has(table)) > .MuiPaper-root { grid-column: 1 / -1; order: -1; display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px; padding: 8px 14px; }
[data-pc-route='/catalog'] .MuiGrid-item:not(:has(table)) > .MuiPaper-root .MuiList-root { display: flex; flex-direction: row; gap: 6px; padding: 0; }
[data-pc-route='/catalog'] .MuiGrid-item:not(:has(table)) > .MuiPaper-root .MuiListItem-root { display: inline-flex; flex-wrap: nowrap !important; align-items: center; width: auto; gap: 12px; padding: 5px 12px; border-radius: 8px; }
[data-pc-route='/catalog'] .MuiGrid-item:not(:has(table)) > .MuiPaper-root .MuiListItem-root > * { position: static !important; transform: none !important; flex: none; margin: 0; }
[data-pc-route='/catalog'] .MuiGrid-item:not(:has(table)) > .MuiPaper-root .MuiListItemText-root { margin: 0; flex: none; }
[data-pc-route='/catalog'] .MuiGrid-item:not(:has(table)) > .MuiPaper-root .MuiListItemSecondaryAction-root { position: static; transform: none; margin-left: 4px; }
[data-pc-route='/catalog'] .MuiGrid-item:has(table) > .MuiBox-root { flex: 1 1 100% !important; width: 100% !important; max-width: 100% !important; }
`;

export const ConsoleStyles = () => <style>{CSS}</style>;