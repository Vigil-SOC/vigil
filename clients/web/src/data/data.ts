import type { IconName } from '../shared/icons'

export type ConsoleScreenKey =
  | 'dashboard'
  | 'cases'
  | 'metrics'
  | 'analytics'
  | 'decisions'
  | 'workflows'
  | 'autoops'
  | 'settings'

/** A rail item carrying a gate only renders when the gate is satisfied. */
export interface NavGate {
  /** an integration id, matched against the enabled-integrations list */
  integration?: string
  orchestrator?: boolean
}

/** Nothing is gated today. Auto Ops is deliberately always-visible — gating it
 *  made it vanish confusingly — and Timesketch has no screen yet. */
export const NAV: [IconName, string, ConsoleScreenKey | null, NavGate?][] = [
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
  title?: string
  sourceIp?: string
  destinationIp?: string
  sev: 'Critical' | 'High' | 'Medium' | 'Low' | 'Unrated'
  tech: string
  conf: number
  tactic: string
  src: string
  host: string
  user: string
  time: string
  /** `time` above is display-only and not safely comparable */
  ts?: number
  score: number | null
  status: 'open' | 'investigating' | 'closed'
  /** entity_context keys the fixed fields don't cover. Sources disagree about
   *  these (CrowdStrike sends device_id and no dest_ips, Splunk the reverse), so
   *  they are carried through and rendered as columns derived from the rows. */
  extra?: Record<string, string>
}

export const MISSING_FINDING_SCORE = 'Not provided' as const
export const MISSING_FINDING_SEVERITY = 'Unrated' as const
export const MISSING_FINDING_TIME = 'Source time unavailable' as const

export interface CaseRow {
  id: string
  title: string
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
  /** display strings can't sort */
  updatedTs?: number
  createdTs?: number
}

export const TITLES: Record<ConsoleScreenKey, [string, string]> = {
  dashboard: ['Dashboard', 'Security operations overview'],
  cases: ['Cases', 'Manage investigation cases'],
  metrics: ['Case Metrics', 'Real-time SOC performance analytics'],
  analytics: ['Analytics Dashboard', 'Security operations analytics'],
  decisions: ['AI Decisions', 'Review and provide feedback for AI decisions'],
  workflows: ['Workflows & Skills', 'Pre-built multi-agent workflows for common SOC operations'],
  autoops: ['Auto Ops', 'Autonomous operations — master orchestrator and sub-agent investigations'],
  settings: ['Settings', 'Configure Vigil — AI, integrations, users and platform'],
}
