/* ============================================================
   Redesign theme context — the single source of truth the
   Appearance settings page writes to and the SOC console shell
   reads from (so the deeply-nested settings section can drive
   the top-level .soc-console styling without prop-drilling).

   - mode (light/dark) delegates to the app-wide, backend-persisted
     ThemeContext (contexts/ThemeContext) so the redesign and the
     legacy MUI app share one preference.
   - accent is redesign-only and persisted to localStorage.
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
import { useTheme } from '../../contexts/ThemeContext'
import { ACCENTS, lighten, normHex } from './accent'
import { BG_PRESETS, defaultBaseForMode, isDarkBase, normHex as normBgHex } from './bg'

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
  mode: 'light' | 'dark'
  setMode: (mode: 'light' | 'dark') => void
  accent: AccentState
  /** apply a named accent preset from ACCENTS */
  setPreset: (key: string) => void
  /** apply a free-typed/picked accent hex; returns true if it was valid */
  setHex: (hex: string) => boolean
  bg: BgState
  /** apply a named background preset from BG_PRESETS (also drives mode) */
  setBgPreset: (key: string) => void
  /** apply a free-typed/picked background hex (also drives mode); true if valid */
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
  if (!ctx) throw new Error('useSocTheme must be used within RedesignThemeProvider')
  return ctx
}

export function RedesignThemeProvider({ children }: { children: ReactNode }) {
  const { mode, setMode } = useTheme()
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

  // Keep the base coherent with the shared light/dark mode. The bg setters push
  // mode to match the base they apply; this effect handles the other direction —
  // when mode changes from outside the redesign (legacy MUI toggle, backend load)
  // and disagrees with the base's lightness, snap the base to that mode's default.
  // No-ops whenever a setter already aligned them, so it can't loop with mode.
  useEffect(() => {
    if (isDarkBase(bg.base) !== (mode === 'dark')) {
      setBg(defaultBaseForMode(mode))
    }
    // intentionally only reacts to `mode`; reacting to `bg` would fight the setters
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode])

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
      if (next !== mode) setMode(next)
    },
    [mode, setMode],
  )

  const setBgHex = useCallback(
    (input: string): boolean => {
      const base = normBgHex(input)
      if (!base) return false
      setBg({ key: null, base })
      const next = isDarkBase(base) ? 'dark' : 'light'
      if (next !== mode) setMode(next)
      return true
    },
    [mode, setMode],
  )

  const value = useMemo<SocThemeValue>(
    () => ({ mode, setMode, accent, setPreset, setHex, bg, setBgPreset, setBgHex }),
    [mode, setMode, accent, setPreset, setHex, bg, setBgPreset, setBgHex],
  )

  return <SocThemeContext.Provider value={value}>{children}</SocThemeContext.Provider>
}
