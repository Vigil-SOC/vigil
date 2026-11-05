import { useEffect, useState } from 'react'

export type NavLayout = 'vertical' | 'top'

export interface NavPrefs {
  layout: NavLayout
  collapsed: boolean
  /** screen keys on the bar, in order; Home is pinned first whatever is stored */
  shown: string[]
  iconsOnly: boolean
}

export const NAV_SHOWN_DEFAULT = ['home', 'start', 'overview', 'triage', 'cases', 'workflows', 'settings']

export const NAV_PREFS_DEFAULT: NavPrefs = { layout: 'vertical', collapsed: false, shown: NAV_SHOWN_DEFAULT, iconsOnly: false }

/** Fired on this tab when a write changes the prefs; other tabs hear `storage`. */
export const NAV_PREFS_CHANGED = 'vigil:nav-prefs-changed'

function navPrefsKey(userId: string): string {
  return `vigil.nav.${userId}`
}

function readRaw(userId: string): Record<string, unknown> {
  try {
    const parsed = JSON.parse(localStorage.getItem(navPrefsKey(userId)) ?? 'null') as unknown
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? (parsed as Record<string, unknown>) : {}
  } catch {
    return {}
  }
}

export function readNavPrefs(userId: string | undefined): NavPrefs {
  if (!userId) return NAV_PREFS_DEFAULT
  const raw = readRaw(userId)
  return {
    layout: raw.layout === 'top' ? 'top' : 'vertical',
    collapsed: raw.collapsed === true,
    shown:
      Array.isArray(raw.shown) && raw.shown.every((key) => typeof key === 'string') ? raw.shown : NAV_SHOWN_DEFAULT,
    iconsOnly: raw.iconsOnly === true,
  }
}

/** Merges into the stored object, so fields other code owns survive. No user, no write. */
export function writeNavPrefs(userId: string | undefined, patch: Partial<NavPrefs>): void {
  if (!userId) return
  try {
    localStorage.setItem(navPrefsKey(userId), JSON.stringify({ ...readRaw(userId), ...patch }))
  } catch {
    return
  }
  window.dispatchEvent(new Event(NAV_PREFS_CHANGED))
}

/** The prefs, re-read when this tab or another one writes them. */
export function useNavPrefs(userId: string | undefined): NavPrefs {
  const [prefs, setPrefs] = useState(() => readNavPrefs(userId))
  useEffect(() => {
    const reread = () => setPrefs(readNavPrefs(userId))
    reread()
    const onStorage = (e: StorageEvent) => {
      if (!userId || e.key === null || e.key === navPrefsKey(userId)) reread()
    }
    window.addEventListener(NAV_PREFS_CHANGED, reread)
    window.addEventListener('storage', onStorage)
    return () => {
      window.removeEventListener(NAV_PREFS_CHANGED, reread)
      window.removeEventListener('storage', onStorage)
    }
  }, [userId])
  return prefs
}
