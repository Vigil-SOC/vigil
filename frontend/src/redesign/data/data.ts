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
  | 'autoops'
  | 'settings'

/** Runtime-dynamic rail membership, mirroring production's NavigationRail.
 *  A rail item carrying a gate only renders when the gate is satisfied. */
export interface NavGate {
  /** show only when this integration id is in the enabled-integrations list */
  integration?: string
  /** show only when the master orchestrator reports enabled */
  orchestrator?: boolean
}

/** [icon, label, screen-key | null, gate?] — null marks a not-yet-wired rail
 *  item (none today; Entity Graph now lives as a Dashboard tab). `gate` mirrors
 *  production's dynamic membership; the plumbing is live in SocConsole. No item
 *  is gated today: Auto Ops is intentionally always-visible (gating it made it
 *  vanish confusingly), and Timesketch has no redesign screen yet — when one
 *  lands, add `['…', 'Timesketch', 'timesketch', { integration: 'timesketch' }]`
 *  and the gating is done. */
export const NAV: [IconName, string, ScreenKey | null, NavGate?][] = [
  ['grid', 'Dashboard', 'dashboard'],
  ['folder', 'Cases', 'cases'],
  ['bars', 'Case Metrics', 'metrics'],
  ['pie', 'Analytics', 'analytics'],
  ['brain', 'AI Decisions', 'decisions'],
  ['flow', 'Workflows & Skills', 'workflows'],
  ['bot', 'Auto Ops', 'autoops'],
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
  autoops: ['Auto Ops', 'Autonomous operations — master orchestrator and sub-agent investigations'],
  settings: ['Settings', 'Configure Vigil — AI, integrations, users and platform'],
}
