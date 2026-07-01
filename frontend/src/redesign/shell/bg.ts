/* Background palette + ramp helpers — parallel to accent.ts, but instead of
   painting just the accent it derives the WHOLE surface + line + text ramp from
   a single base color the user picks. Dark vs. light is decided by the base's
   relative luminance; every other token is the base mixed toward white/black. */
import type { CSSProperties } from 'react'
import { normHex } from './accent'

/** base hex for each named preset */
export const BG_PRESETS: Record<string, string> = {
  slate: '#0c0f14',
  ink: '#08090c',
  navy: '#0b1020',
  espresso: '#14110d',
  light: '#f4f5f7',
}

/** preset swatches shown in the tweaks panel, in order (first = default) */
export const BG_SWATCHES: { key: string; color: string }[] = [
  { key: 'slate', color: '#0c0f14' },
  { key: 'ink', color: '#08090c' },
  { key: 'navy', color: '#0b1020' },
  { key: 'espresso', color: '#14110d' },
  { key: 'light', color: '#f4f5f7' },
]

/** base whose luminance is below this is treated as a dark base */
export const BG_DARK_CUTOFF = 0.42

/** per-channel lerp between two hex colors: mix(a,b,0)=a, mix(a,b,1)=b */
export function mix(a: string, b: string, t: number): string {
  const na = parseInt(a.slice(1), 16)
  const nb = parseInt(b.slice(1), 16)
  const ch = (shift: number) => {
    const ca = (na >> shift) & 255
    const cb = (nb >> shift) & 255
    return Math.round(ca + (cb - ca) * t)
  }
  return '#' + [ch(16), ch(8), ch(0)].map((c) => c.toString(16).padStart(2, '0')).join('')
}

/** sRGB-linearized relative luminance (0..1): 0.2126r + 0.7152g + 0.0722b */
export function lum(hex: string): number {
  const n = parseInt(hex.slice(1), 16)
  const lin = (c: number) => {
    const s = c / 255
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4)
  }
  return 0.2126 * lin(n >> 16) + 0.7152 * lin((n >> 8) & 255) + 0.0722 * lin(n & 255)
}

/** whether a base hex should produce the dark ramp */
export function isDarkBase(base: string): boolean {
  return lum(base) < BG_DARK_CUTOFF
}

const WHITE = '#ffffff'
const BLACK = '#000000'

/** build the inline CSS-var style block that paints the surface/line/text ramp
   from a single base color. Dark/light is chosen by the base's luminance.
   --shadow + color-scheme are intentionally NOT set here — they come from
   data-theme, which the theme context keeps aligned with the base's lightness. */
export function bgVars(base: string): CSSProperties {
  if (isDarkBase(base)) {
    return {
      '--bg': base,
      '--bg-1': mix(base, WHITE, 0.035),
      '--bg-2': mix(base, WHITE, 0.07),
      '--bg-3': mix(base, WHITE, 0.12),
      '--panel': mix(base, WHITE, 0.045),
      '--hover': mix(base, WHITE, 0.09),
      '--line': mix(base, WHITE, 0.14),
      '--line-soft': mix(base, WHITE, 0.075),
      // not enumerated in the spec, but a real line token — derived just above
      // --line so the ramp stays coherent under a custom base.
      '--line-strong': mix(base, WHITE, 0.2),
      '--tx': '#e8ebf0',
      '--tx-2': '#aeb6c2',
      '--tx-3': '#7c8593',
      '--tx-faint': '#58616e',
    } as CSSProperties
  }
  return {
    '--bg': base,
    '--bg-1': mix(base, BLACK, 0.03),
    '--bg-2': mix(base, BLACK, 0.055),
    '--bg-3': mix(base, BLACK, 0.09),
    '--panel': mix(base, WHITE, 0.55),
    '--hover': mix(base, BLACK, 0.05),
    '--line': mix(base, BLACK, 0.13),
    '--line-soft': mix(base, BLACK, 0.07),
    '--line-strong': mix(base, BLACK, 0.2),
    '--tx': '#1a1d24',
    '--tx-2': '#464d59',
    '--tx-3': '#6c7480',
    '--tx-faint': '#99a1ad',
  } as CSSProperties
}

/** the default base for a given mode (used when snapping base ↔ mode) */
export function defaultBaseForMode(mode: 'light' | 'dark'): { key: string; base: string } {
  return mode === 'dark'
    ? { key: 'slate', base: BG_PRESETS.slate }
    : { key: 'light', base: BG_PRESETS.light }
}

/** normalize a free-typed/picked hex (re-exported for the bg control) */
export { normHex }
