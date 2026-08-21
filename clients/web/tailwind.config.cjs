/** @type {import('tailwindcss').Config} */
// preflight is OFF: the console's own reset lives in styles.css under
// @layer base, so Tailwind's global reset would fight it. Color tokens point at
// the CSS variables defined on .soc-console, keeping the accent runtime-swappable
// from Settings -> Appearance.
module.exports = {
  content: ['./src/**/*.{ts,tsx}'],
  corePlugins: { preflight: false },
  theme: {
    extend: {
      colors: {
        bg: 'var(--bg)',
        'bg-1': 'var(--bg-1)',
        'bg-2': 'var(--bg-2)',
        'bg-3': 'var(--bg-3)',
        panel: 'var(--panel)',
        hover: 'var(--hover)',
        line: 'var(--line)',
        'line-soft': 'var(--line-soft)',
        tx: 'var(--tx)',
        'tx-2': 'var(--tx-2)',
        'tx-3': 'var(--tx-3)',
        'tx-faint': 'var(--tx-faint)',
        accent: 'var(--accent)',
        'accent-2': 'var(--accent-2)',
        'accent-dim': 'var(--accent-dim)',
        'accent-line': 'var(--accent-line)',
        crit: 'var(--crit)',
        'crit-dim': 'var(--crit-dim)',
        high: 'var(--high)',
        'high-dim': 'var(--high-dim)',
        med: 'var(--med)',
        'med-dim': 'var(--med-dim)',
        ok: 'var(--ok)',
        'ok-dim': 'var(--ok-dim)',
      },
      borderRadius: {
        sm: '6px',
        DEFAULT: '9px',
        lg: '13px',
      },
      fontFamily: {
        sans: ['"IBM Plex Sans"', 'system-ui', 'sans-serif'],
        mono: ['"IBM Plex Mono"', 'ui-monospace', 'monospace'],
      },
      boxShadow: {
        panel: '0 1px 0 #ffffff06 inset, 0 8px 24px -16px #000000cc',
      },
    },
  },
  plugins: [],
}
