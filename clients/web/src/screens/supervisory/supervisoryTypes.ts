/** The functions the supervisory model watches in phase 1. */
export type SupervisedFunction = 'triage' | 'investigation' | 'response'

/** running: checks on schedule. on_hold: a related Medic incident is open, so it suggests nothing. off: not running on this install. unknown: no check has run yet, the last one is more than two intervals old, or the server could not read its own state. */
export type SupervisorState = 'running' | 'on_hold' | 'off' | 'unknown'

export interface SupervisorStatus {
  state: SupervisorState
  /** ISO times; null before the first check */
  last_check_at: string | null
  next_check_at: string | null
  history_days: number
  functions_watched: number
  /** Medic incidents open that hold a suggestion; 0 when none */
  medic_open_related: number
  /** Parts of the last check that failed: a check's name, `medic_hold` or `follow_up` */
  failed: string[]
}

/** One function's labelled examples against the floor. `state` is the server's read, never derived here. */
export interface FunctionEvidence {
  fn: SupervisedFunction
  state: 'ready' | 'waiting'
  labelled: number
  floor: number
  /** what a labelled example is, e.g. "closed cases" */
  unit: string
}

/** How a column's cells read: plain text, a fixed-width value, or an id that opens a case. */
export type EvidenceColumnKind = 'text' | 'mono' | 'case' | 'action'

export interface EvidenceCell {
  text: string
  /** an analyst found it was an attack */
  attack?: boolean
}

export interface EvidenceTable {
  title: string
  columns: { label: string; kind: EvidenceColumnKind }[]
  rows: EvidenceCell[][]
  /** e.g. "8 more · all from the last 30 days" */
  foot: string
}

/** One measure on the customer's history, as it is now and as it would have been. */
export interface ReplayRow {
  label: string
  now: string
  next: string
  effect: 'better' | 'worse' | 'same'
}

export interface Suggestion {
  id: string
  fn: SupervisedFunction
  /** "Raise", "Lower", "Turn on" … */
  verb: string
  key: string
  from: string
  to: string
  widens_autonomy: boolean
  suggested_at: string
  why: string
  /** e.g. "71 decisions · 30 days" */
  evidence: string
  /** e.g. "11 fewer wrong proposals" */
  replay: string
  /** the 30-case eval; investigation suggestions only, null elsewhere */
  check: { passed: boolean; text: string } | null
  plain_words: string
  table: EvidenceTable
  replay_title: string
  replay_rows: ReplayRow[]
  /** whether Vigil can change the value itself; `reason` says why not */
  apply: { possible: boolean; reason?: string }
}

/** A suggestion that waits while a related Medic incident is open. */
export interface HeldSuggestion {
  id: string
  fn: SupervisedFunction
  verb: string
  key: string
  from: string
  to: string
  incident: { id: string; title: string; firing_since: string }
}

export interface FailureReason {
  reason: string
  count: number
  note: string | null
}

export interface HistoryEntry {
  id: string
  at: string
  key: string
  from: string
  to: string
  outcome: 'applied' | 'dismissed' | 'undone'
  /** applied: what changed since; dismissed: the reason given; '' when there is nothing yet */
  note: string
  /** applied only: whether results improved after */
  improved?: boolean | null
  /** applied here and can still be put back */
  undoable?: boolean
}

export interface CurrentSetting {
  key: string
  value: string
  /** where the value comes from */
  source: 'env' | 'database' | 'default' | 'INTENT.md'
  /** whether Vigil can change it itself; `reason` says why not, or why only one way */
  change: { possible: boolean; reason?: string }
}

export interface LearnsFrom {
  label: string
  count: number
}

export const DISMISS_REASONS = ['Too risky', 'Evidence looks wrong', 'Not now', 'Already handled'] as const
export type DismissReason = (typeof DISMISS_REASONS)[number]

/** Everything the page reads, in one response. */
export interface SupervisoryRead {
  status: SupervisorStatus
  functions: FunctionEvidence[]
  suggestions: Suggestion[]
  held: HeldSuggestion[]
  /** null: not measured yet; an empty `items` was measured and found nothing */
  failures: { days: number; items: FailureReason[] } | null
  history: HistoryEntry[]
  /** null: not measured yet; empty was measured and found nothing */
  settings: CurrentSetting[] | null
  learns_from: { days: number; items: LearnsFrom[] }
}
