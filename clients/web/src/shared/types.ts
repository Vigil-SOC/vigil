/* Shared contract the console's keyed screens implement — the ones
   ConsoleScreenKey names and SocConsole routes. Login, Setup and the
   404 render outside the shell and do not implement it. */
import type { ConsoleScreenKey } from '../data/data'

export type SettingsSectionKey =
  | 'appearance'
  | 'ai-config'
  | 'services'
  | 'integrations'
  | 'users'
  | 'sla'
  | 'autoinvestigate'
  | 'federation'
  | 'system'
  | 'general'
  | 'dev'

export interface ConsoleScreenGoOptions {
  search?: string
  replace?: boolean
}

export interface ConsoleScreenProps {
  /** open the Vigil chat dock; pass a prompt to auto-send it (used by
      "investigate with Vigil" affordances) */
  openChat: (prompt?: string) => void
  /** navigate within the console shell */
  go: (screen: ConsoleScreenKey, options?: ConsoleScreenGoOptions) => void
  /** navigate to a concrete Settings section */
  goSettings: (section: SettingsSectionKey) => void
  /** tell the shell this screen wants the full-height, non-scrolling view
      (used by the cases / decisions master-detail split layouts) */
  setViewFull: (full: boolean) => void
}
