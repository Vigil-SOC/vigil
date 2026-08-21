/* ============================================================
   SOC theme context — the single source of truth the Appearance
   settings page writes to and the SOC console shell reads from (so
   the deeply-nested settings section can drive the top-level
   .soc-console styling without prop-drilling).

   Layered over ColorSchemeContext on purpose; do not collapse them:
   - scheme (light/dark) delegates to ColorSchemeContext, which is
     app-wide and backend-persisted. Login and Setup render outside
     THIS provider but still need the scheme, and the preference has
     to outlive the browser.
   - accent + bg are console-only, persisted to localStorage. Nothing
     outside .soc-console reads them.
   ============================================================ */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { useColorScheme } from '../contexts/ColorSchemeContext'
import { ACCENTS, lighten, normHex } from './accent'
import { BG_PRESETS, defaultBaseForScheme, isDarkBase, normHex as normBgHex } from './bg'

export interface AccentState {
  /** preset key, or null when a custom hex is in use */
  key: string | null
  /** base accent (--accent) */
  a: string
  /** lightened highlight tone (--accent-2) */
  b: string
}

export interface BgState {
  /** preset key, or null when a custom hex is in use */
  key: string | null
  /** base color (--bg); the rest of the ramp is derived from it */
  base: string
}

interface SocThemeValue {
  scheme: 'light' | 'dark'
  setScheme: (scheme: 'light' | 'dark') => void
  accent: AccentState
  /** apply a named accent preset from ACCENTS */
  setPreset: (key: string) => void
  /** apply a free-typed/picked accent hex; returns true if it was valid */
  setHex: (hex: string) => boolean
  bg: BgState
  /** apply a named background preset from BG_PRESETS (also drives scheme) */
  setBgPreset: (key: string) => void
  /** apply a free-typed/picked background hex (also drives scheme); true if valid */
  setBgHex: (hex: string) => boolean
}

const DEFAULT_ACCENT: AccentState = { key: 'violet', a: '#7d74f3', b: '#9a92f7' }
const ACCENT_KEY = 'soc.accent'

const DEFAULT_BG: BgState = { key: 'slate', base: BG_PRESETS.slate }
const BG_KEY = 'soc.bg'

function loadAccent(): AccentState {
  try {
    const raw = localStorage.getItem(ACCENT_KEY)
    if (raw) {
      const p = JSON.parse(raw) as Partial<AccentState>
      if (p && typeof p.a === 'string' && typeof p.b === 'string') {
        return { key: typeof p.key === 'string' ? p.key : null, a: p.a, b: p.b }
      }
    }
  } catch {
    /* malformed / unavailable localStorage — fall back to the default */
  }
  return DEFAULT_ACCENT
}

function loadBg(): BgState {
  try {
    const raw = localStorage.getItem(BG_KEY)
    if (raw) {
      const p = JSON.parse(raw) as Partial<BgState>
      if (p && typeof p.base === 'string') {
        return { key: typeof p.key === 'string' ? p.key : null, base: p.base }
      }
    }
  } catch {
    /* malformed / unavailable localStorage — fall back to the default */
  }
  return DEFAULT_BG
}

const SocThemeContext = createContext<SocThemeValue | undefined>(undefined)

export function useSocTheme(): SocThemeValue {
  const ctx = useContext(SocThemeContext)
  if (!ctx) throw new Error('useSocTheme must be used within SocThemeProvider')
  return ctx
}

export function SocThemeProvider({ children }: { children: ReactNode }) {
  const { scheme, setScheme } = useColorScheme()
  const [accent, setAccent] = useState<AccentState>(loadAccent)
  const [bg, setBg] = useState<BgState>(loadBg)

  useEffect(() => {
    try {
      localStorage.setItem(ACCENT_KEY, JSON.stringify(accent))
    } catch {
      /* localStorage unavailable — keep the in-memory accent only */
    }
  }, [accent])

  useEffect(() => {
    try {
      localStorage.setItem(BG_KEY, JSON.stringify(bg))
    } catch {
      /* localStorage unavailable — keep the in-memory background only */
    }
  }, [bg])

  // Keep the base coherent with the shared light/dark scheme. The bg setters push
  // scheme to match the base they apply; this effect handles the other direction —
  // when scheme changes from outside this provider (backend load, Setup)
  // and disagrees with the base's lightness, snap the base to that scheme's default.
  // No-ops whenever a setter already aligned them, so it can't loop with scheme.
  useEffect(() => {
    if (isDarkBase(bg.base) !== (scheme === 'dark')) {
      setBg(defaultBaseForScheme(scheme))
    }
    // intentionally only reacts to `scheme`; reacting to `bg` would fight the setters
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scheme])

  const setPreset = useCallback((key: string) => {
    const preset = ACCENTS[key]
    if (!preset) return
    const [a, b] = preset
    setAccent({ key, a, b })
  }, [])

  const setHex = useCallback((input: string): boolean => {
    const a = normHex(input)
    if (!a) return false
    setAccent({ key: null, a, b: lighten(a, 0.22) })
    return true
  }, [])

  const setBgPreset = useCallback(
    (key: string) => {
      const base = BG_PRESETS[key]
      if (!base) return
      setBg({ key, base })
      const next = isDarkBase(base) ? 'dark' : 'light'
      if (next !== scheme) setScheme(next)
    },
    [scheme, setScheme],
  )

  const setBgHex = useCallback(
    (input: string): boolean => {
      const base = normBgHex(input)
      if (!base) return false
      setBg({ key: null, base })
      const next = isDarkBase(base) ? 'dark' : 'light'
      if (next !== scheme) setScheme(next)
      return true
    },
    [scheme, setScheme],
  )

  const value = useMemo<SocThemeValue>(
    () => ({ scheme, setScheme, accent, setPreset, setHex, bg, setBgPreset, setBgHex }),
    [scheme, setScheme, accent, setPreset, setHex, bg, setBgPreset, setBgHex],
  )

  return <SocThemeContext.Provider value={value}>{children}</SocThemeContext.Provider>
}
