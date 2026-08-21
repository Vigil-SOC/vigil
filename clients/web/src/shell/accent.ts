/* Accent palette + hex helpers (ported from main.js tweaks logic). */
import type { CSSProperties } from 'react'

export const ACCENTS: Record<string, [string, string]> = {
  violet: ['#7d74f3', '#9a92f7'],
  cyan: ['#28a9bd', '#45c2d4'],
  emerald: ['#3aab74', '#54c08c'],
  coral: ['#e2705f', '#ec8a7b'],
}

/** preset swatches shown in the tweaks panel, in order */
export const ACCENT_SWATCHES: { key: string; color: string }[] = [
  { key: 'violet', color: '#7d74f3' },
  { key: 'cyan', color: '#28a9bd' },
  { key: 'emerald', color: '#3aab74' },
  { key: 'coral', color: '#e2705f' },
]

/** normalize "#abc" / "abc" / "aabbcc" -> "#aabbcc"; null if invalid */
export function normHex(v: string): string | null {
  if (!v) return null
  let h = v.trim().replace(/^#/, '').toLowerCase()
  if (/^[0-9a-f]{3}$/.test(h)) h = h.split('').map((c) => c + c).join('')
  return /^[0-9a-f]{6}$/.test(h) ? '#' + h : null
}

/** lighten a hex toward white by amt (0..1) for the --accent-2 highlight tone */
export function lighten(hex: string, amt: number): string {
  const n = parseInt(hex.slice(1), 16)
  const r = n >> 16
  const g = (n >> 8) & 255
  const b = n & 255
  const mix = (c: number) => Math.round(c + (255 - c) * amt)
  return '#' + [mix(r), mix(g), mix(b)].map((c) => c.toString(16).padStart(2, '0')).join('')
}

/** build the inline CSS-var style block that paints the accent onto .soc-console */
export function accentVars(a: string, b: string): CSSProperties {
  return {
    '--accent': a,
    '--accent-2': b,
    '--accent-dim': a + '24',
    '--accent-line': a + '55',
  } as CSSProperties
}
