import { relativeTime } from '../settings/integrationHealth'

export type Stage = 'shadow' | 'live' | 'off'

/** A rule row's rollout fields, as the rule listing sends them (absent on SIEM rules). */
export interface StageFields {
  stage?: Stage | null
  /** When its current stage began (ISO). */
  stage_since?: string | null
  /** Whole days since a shadow stage began. */
  days_in_shadow?: number | null
  /** The stage before the current one, and who made the last move ('system' is Vigil). */
  stage_from?: Stage | null
  stage_actor?: string
  /** Why the last move happened, as its stage row says it. */
  stage_reason?: string
  /** A person's Revert holds it in shadow until a person releases it. */
  held?: boolean
  held_by?: string | null
  /** Its shadow count came from Vigil's Wazuh sandbox replaying a sample of the archive (#4099). */
  sampled?: SampledCount | null
}

export interface SampledCount {
  /** What counted it: Vigil's Wazuh sandbox, and its logtest release. */
  engine: string
  /** Events replayed and counted, of the archived events with a raw line in the window. */
  events: number
  of: number
  /** Left out: the sandbox decoded them with another decoder than the customer's manager. */
  decoded_differently: number
  /** Left out: logtest refused them. */
  refused: number
}

/** How a sandbox count was made, as the rule's Rollout panel says it. */
export function sampledText(s: SampledCount): string {
  const left = s.decoded_differently + s.refused
  return (
    `Counted by ${s.engine} on a sample: ${s.events.toLocaleString()} of ` +
    `${s.of.toLocaleString()} archived events replayed in its last run` +
    (left ? `, ${left.toLocaleString()} left out (${s.decoded_differently.toLocaleString()} decoded differently than on the customer's manager)` : '') +
    '. Shadow hits are hits in the sample, not in every event.'
  )
}

export interface StageLine {
  text: string
  title: string
  /** Vigil moved it back to shadow in the last 7 days. */
  amber: boolean
  live: boolean
  held: boolean
}

/** The reason Vigil writes when a live rule's new version goes back to shadow for its
 * week: not a Revert of the rule. */
export const NEW_VERSION = 'new version'

/** How long Vigil's own move back to shadow stays amber on the rule's line. */
const RECENT_MS = 7 * 24 * 60 * 60 * 1000
const STAGE_NAME: Record<Stage, string> = { shadow: 'Shadow', live: 'Live', off: 'Off' }

export const who = (actor: string) => (actor === 'system' ? 'Vigil' : actor)
export const dayMonth = (iso: string) => new Date(iso).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })
const days = (d: number) => `${d.toLocaleString()} ${d === 1 ? 'day' : 'days'}`

/** A Vigil rule's stage, as the grey line under its name says it; null for a rule with no stage. */
export function stageLine(r: StageFields): StageLine | null {
  if (!r.stage) return null
  const since = r.stage_since ?? null
  const title = r.stage_actor ? `Last move by ${who(r.stage_actor)}${r.stage_from ? `: ${STAGE_NAME[r.stage_from]} → ${STAGE_NAME[r.stage]}` : ''}` : ''
  const base = { title, amber: false, live: r.stage === 'live', held: !!r.held }
  if (r.stage === 'shadow') {
    const backByVigil = r.stage_from === 'live' && r.stage_actor === 'system' && !r.held
    const recent = !!since && Date.now() - new Date(since).getTime() < RECENT_MS
    if (backByVigil && recent && r.stage_reason === NEW_VERSION) {
      return { ...base, text: 'New version · back in shadow for its week' }
    }
    if (backByVigil && recent) {
      return { ...base, amber: true, text: `Vigil moved it back to shadow ${relativeTime(since)}${r.stage_reason ? `: ${r.stage_reason}` : ''}` }
    }
    if (r.held) return { ...base, text: `In shadow, held by ${r.held_by ? who(r.held_by) : 'a person'}${since ? ` since ${dayMonth(since)}` : ''}` }
    const d = r.days_in_shadow
    return { ...base, text: d == null ? 'In shadow' : d === 0 ? 'In shadow · since today' : `In shadow · ${days(d)}` }
  }
  if (r.stage === 'live') return { ...base, text: since ? `Live since ${dayMonth(since)}` : 'Live' }
  return { ...base, text: since ? `Off since ${dayMonth(since)}` : 'Switched off' }
}
