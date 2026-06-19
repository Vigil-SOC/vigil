/* ============================================================
   Shared view-model types (Finding, CaseRow) + nav/title config
   for the SOC console. Screens fetch real data via services/api
   and map it into these shapes (see data/mappers.ts).
   ============================================================ */
import type { IconName } from '../shared/icons'

export type ScreenKey =
  | 'dashboard'
  | 'cases'
  | 'metrics'
  | 'analytics'
  | 'decisions'
  | 'workflows'
  | 'settings'

/** [icon, label, screen-key | null] — null marks a not-yet-wired rail item
 *  (none today; Entity Graph now lives as a Dashboard tab, not a rail item) */
export const NAV: [IconName, string, ScreenKey | null][] = [
  ['grid', 'Dashboard', 'dashboard'],
  ['folder', 'Cases', 'cases'],
  ['bars', 'Case Metrics', 'metrics'],
  ['pie', 'Analytics', 'analytics'],
  ['brain', 'AI Decisions', 'decisions'],
  ['flow', 'Workflows & Skills', 'workflows'],
  ['gear', 'Settings', 'settings'],
]

export interface Finding {
  id: string
  sev: 'Critical' | 'High' | 'Medium' | 'Low'
  tech: string
  conf: number
  tactic: string
  src: string
  host: string
  user: string
  time: string
  /** epoch ms for the finding's timestamp — used to sort the Time column
   *  (the `time` string above is display-only and not safely comparable) */
  ts?: number
  score: number
  status: 'open' | 'investigating' | 'closed'
}

export interface CaseRow {
  id: string
  title: string
  /** case description (optional; populated from the API) */
  desc?: string
  status: 'open' | 'investigating' | 'closed'
  prio: 'critical' | 'high' | 'medium' | 'low'
  owner: string
  ownerName: string
  findings: number
  tactic: string
  age: string
  sla: string
  slaState: 'warn' | 'danger' | 'ok'
  updated: string
  /** epoch ms for chronological sorting (display strings can't sort) */
  updatedTs?: number
  createdTs?: number
}

/** title + subtitle per screen (drives the topbar) */
export const TITLES: Record<ScreenKey, [string, string]> = {
  dashboard: ['Dashboard', 'Security operations overview'],
  cases: ['Cases', 'Manage investigation cases'],
  metrics: ['Case Metrics', 'Real-time SOC performance analytics'],
  analytics: ['Analytics Dashboard', 'Security operations analytics'],
  decisions: ['AI Decisions', 'Review and provide feedback for AI decisions'],
  workflows: ['Workflows & Skills', 'Pre-built multi-agent workflows for common SOC operations'],
  settings: ['Settings', 'Configure Vigil — AI, integrations, users and platform'],
}
