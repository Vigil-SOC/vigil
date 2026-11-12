/* The shapes /api/medic/* returns (services/medic/contracts/medic-api.openapi.yaml). The
   proxy passes Medic's bytes through, so every field is read defensively. */

export type Eval = 'true' | 'false' | 'unknown'

export interface Incident {
  incident_id: string
  subject: 'vigil' | 'watcher'
  rule: { id: string | null; revision: number; pack_id: string; pack_version: string }
  title: string
  lane: { value: 1 | 2 | 3; reason: string }
  state: 'firing' | 'resolving' | 'held' | 'suppressed' | 'resolved'
  last_eval: Eval
  opened_at: string
  active_since: string
  routed_at: string | null
  held_until: string | null
  suppressed_by: string | null
  suppressed_children: { count: number; incident_ids: string[] }
  reopen_count: number
  escalation_simulated: boolean
  resolved_at: string | null
  resolved_how: 'cleared' | 'rule_retired' | 'group_retired' | 'closed_quietly' | null
  group: { name: string; value: string }[]
  has_fix: boolean
  rateable: boolean
  allowed_feedback: string[]
  ratings: { admin: string; value: string; suggested_lane?: number; at: string; seq: number }[]
  last_seq: number
}

export interface IncidentPage {
  items: Incident[]
  next_cursor: string | null
  counts: { incidents: number; rateable: number; rated: number }
}

export interface EvidenceItem {
  observation_id: string
  signal: string
  t: string
  key?: string
  value?: string | number | boolean
  excerpt?: { text: string; redaction_version: string }
}

/** One record of the decision log; `body` is typed by `type`, read loosely. */
export interface DecisionRecord {
  seq: number
  at: string
  type: string
  body: Record<string, unknown>
}

export interface IncidentOpened {
  active_since?: string
  route?: string
  held_by_upgrade_until?: string
  lane?: { value: number; reason: string }
  evidence?: EvidenceItem[]
  advice?: { summary: string; fix: string } | null
  would_have?: { L0: string; L1: string; L2: { action: string; not_eligible?: string } }
}

export interface IncidentDetail {
  summary: Incident
  opened: DecisionRecord
  /** Vigil's record of the notice for this reopen. Medic does not send it. */
  told?: 'none' | 'opened' | 'summary'
}

export interface StatusRead {
  state: 'off' | 'running' | 'down' | 'unknown'
  last_seen_at?: string
  medic?: {
    state: string
    heartbeat_at: string
    uptime_s: number
    restarts_24h: number
    last_exit: { reason: string; at: string } | null
    dev_mode: boolean
    store: { states: string[]; bytes_used: number; cap_bytes: number }
    sensors: { reporting: number; cant_see: number; off: number }
    pack: { id: string; version: string; channel: string; expires_at: string; stale: boolean } | null
  }
}

/** One row of GET /api/medic/sensors (Sensor). `state` and `reason` are enums on the contract but read as any string. */
export interface Sensor {
  id: string
  state: string
  since: string | null
  last_good_at: string | null
  reason: string | null
  dropped_last_hour: number | null
  watcher_incident_id: string | null
}

export interface SensorList {
  as_of: string
  items: Sensor[]
}

/** The feedback request (FeedbackRequest): no user field, no source; an empty comment is left out. */
export interface FeedbackBody {
  value: string
  suggested_lane?: number
  comment?: string
}

/** One pack's facts (PackFacts). */
export interface PackFacts {
  pack: { id: string; version: string; channel: string; created_at: string; expires_at: string; publisher: string }
  sha256: string
  vigil: { min: string; max_exclusive: string }
  source: 'bundled' | 'admin_import' | string
  imported_by: string | null
  activated_at: string
  stale: boolean
  in_vigil_range: boolean
  rules_skipped: number
}

/** GET /api/medic/pack, and what each pack write returns (PackState). */
export interface PackState {
  active: PackFacts | null
  previous: PackFacts | null
  bundled: PackFacts | null
  withdrawn: { id: string; channel: string; version: string }[]
  suppression_7d: { rule: string; suppressed: number }[]
}
