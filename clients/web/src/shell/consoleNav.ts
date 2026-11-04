import { useMemo } from 'react'
import { useAuth } from '../contexts/AuthContext'
import { NAV, TITLES, type ConsoleScreenKey, type NavGate } from '../data/data'
import { useExtensions } from '../extensions/ExtensionProvider'
import type { IconName } from '../shared/icons'
import { HOME_PERM } from './landing'

/** The only permission check in the app; ProtectedRoute handles auth alone.
 *  Screens absent here are ungated, and DEV_MODE grants everything. */
export const SCREEN_PERMS: Partial<Record<ConsoleScreenKey, string>> = {
  cases: 'cases.read',
  home: HOME_PERM,
  medic: 'medic.read',
}

/** key is a plain string, so extension screens can join the nav row */
export type NavItem = [IconName, string, string, NavGate?]

/** Every screen with its title and permission, extension mounts included, and
 *  the nav items this operator may open. The bar and Settings › Navigation
 *  both read this, so neither filters on its own. Must sit under ExtensionProvider. */
export function useConsoleNav() {
  const { hasPermission } = useAuth()
  const { mountPoints, enabledIntegrations } = useExtensions()

  // built-ins win, so an extension can't shadow a core screen
  const { navItems, titles, screenPerms } = useMemo(() => {
    const titles: Record<string, [string, string]> = { ...TITLES }
    const screenPerms: Record<string, string | undefined> = { ...SCREEN_PERMS }
    const navItems: NavItem[] = [...(NAV as NavItem[])]
    const extNav: NavItem[] = []
    for (const { mount } of mountPoints) {
      if (mount.key in titles) continue
      titles[mount.key] = [mount.title, mount.subtitle ?? '']
      if (mount.permission) screenPerms[mount.key] = mount.permission
      extNav.push([
        (mount.icon || 'brain') as IconName,
        mount.navLabel,
        mount.key,
        mount.gate?.integration ? { integration: mount.gate.integration } : undefined,
      ])
    }
    // extension tabs slot above Settings
    const settingsIdx = navItems.findIndex(([, , key]) => key === 'settings')
    navItems.splice(settingsIdx === -1 ? navItems.length : settingsIdx, 0, ...extNav)
    return { navItems, titles, screenPerms }
  }, [mountPoints])

  const visibleNav = navItems.filter(([, , key, gate]) => {
    const perm = screenPerms[key]
    if (perm && !hasPermission(perm)) return false
    return !gate?.integration || enabledIntegrations.includes(gate.integration)
  })
  return { navItems, titles, screenPerms, visibleNav }
}

/** Splits the visible items into the bar, in `shown` order with Home first,
 *  and the rest in nav order. Stored keys that are gone or not permitted drop out. */
export function splitNav(visibleNav: NavItem[], shown: string[]): { bar: NavItem[]; rest: NavItem[] } {
  const byKey = new Map(visibleNav.map((item) => [item[2], item]))
  const keys = ['home', ...shown.filter((key) => key !== 'home')]
  const bar = [...new Set(keys)].map((key) => byKey.get(key)).filter((item): item is NavItem => Boolean(item))
  const onBar = new Set(bar.map((item) => item[2]))
  return { bar, rest: visibleNav.filter((item) => !onBar.has(item[2])) }
}
