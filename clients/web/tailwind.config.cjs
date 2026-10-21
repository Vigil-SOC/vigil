/** @type {import('tailwindcss').Config} */
// Loaded from styles.css via @config. Preflight stays off there (theme.css and
// utilities.css only) so this file does not set corePlugins — v4 ignores it.
// Color tokens point at the CSS variables defined on .soc-console.
module.exports = {
  content: ['./src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        bg: 'var(--bg)',
        'bg-1': 'var(--bg-1)',
        'bg-2': 'var(--bg-2)',
        'bg-3': 'var(--bg-3)',
        panel: 'var(--panel)',
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
        high: 'var(--high)',
        ok: 'var(--ok)',
        'ok-dim': 'var(--ok-dim)',
      },
      borderRadius: {
        sm: '6px',
        DEFAULT: '9px',
        lg: '13px',
      },
      fontFamily: {
        sans: ['var(--font-ui)'],
        mono: ['var(--font-mono)'],
      },
      boxShadow: {
        panel: '0 8px 24px -16px var(--shadow)',
      },
    },
  },
  plugins: [],
}
