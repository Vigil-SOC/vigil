/* Implemented by the keyed screens only. Login, Setup and the 404 render
   outside the shell and do not implement it. */
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
  | 'data'

export interface ConsoleScreenGoOptions {
  search?: string
  replace?: boolean
}

export interface ConsoleScreenProps {
  /** a prompt is auto-sent on open */
  openChat: (prompt?: string) => void
  go: (screen: ConsoleScreenKey, options?: ConsoleScreenGoOptions) => void
  goSettings: (section: SettingsSectionKey) => void
  /** full-height, non-scrolling view — the master-detail splits want this */
  setViewFull: (full: boolean) => void
  /** Overview's wall mode. Hides the nav row and the top bar. */
  setWallMode?: (wall: boolean) => void
}
