/* Shared contract every redesign screen component implements. */
import type { ScreenKey } from '../data/data'

export type SettingsSectionKey =
  | 'appearance'
  | 'ai-config'
  | 'integrations'
  | 'users'
  | 'sla'
  | 'autoinvestigate'
  | 'federation'
  | 'system'
  | 'general'
  | 'dev'

export interface ScreenGoOptions {
  search?: string
  replace?: boolean
}

export interface ScreenProps {
  /** open the Vigil chat dock; pass a prompt to auto-send it (used by
      "investigate with Vigil" affordances) */
  openChat: (prompt?: string) => void
  /** navigate within the redesign shell */
  go: (screen: ScreenKey, options?: ScreenGoOptions) => void
  /** navigate to a concrete Settings section */
  goSettings: (section: SettingsSectionKey) => void
  /** tell the shell this screen wants the full-height, non-scrolling view
      (used by the cases / decisions master-detail split layouts) */
  setViewFull: (full: boolean) => void
}
