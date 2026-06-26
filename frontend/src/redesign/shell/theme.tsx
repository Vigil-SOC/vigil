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

export interface AccentState {
  /** preset key, or null when a custom hex is in use */
  key: string | null
  /** base accent (--accent) */
  a: string
  /** lightened highlight tone (--accent-2) */
  b: string
}

interface SocThemeValue {
  mode: 'light' | 'dark'
  setMode: (mode: 'light' | 'dark') => void
  accent: AccentState
  /** apply a named preset from ACCENTS */
  setPreset: (key: string) => void
  /** apply a free-typed/picked hex; returns true if it was valid */
  setHex: (hex: string) => boolean
}

const DEFAULT_ACCENT: AccentState = { key: 'violet', a: '#7d74f3', b: '#9a92f7' }
const ACCENT_KEY = 'soc.accent'

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

const SocThemeContext = createContext<SocThemeValue | undefined>(undefined)

export function useSocTheme(): SocThemeValue {
  const ctx = useContext(SocThemeContext)
  if (!ctx) throw new Error('useSocTheme must be used within RedesignThemeProvider')
  return ctx
}

export function RedesignThemeProvider({ children }: { children: ReactNode }) {
  const { mode, setMode } = useTheme()
  const [accent, setAccent] = useState<AccentState>(loadAccent)

  useEffect(() => {
    try {
      localStorage.setItem(ACCENT_KEY, JSON.stringify(accent))
    } catch {
      /* localStorage unavailable — keep the in-memory accent only */
    }
  }, [accent])

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

  const value = useMemo<SocThemeValue>(
    () => ({ mode, setMode, accent, setPreset, setHex }),
    [mode, setMode, accent, setPreset, setHex],
  )

  return <SocThemeContext.Provider value={value}>{children}</SocThemeContext.Provider>
}
