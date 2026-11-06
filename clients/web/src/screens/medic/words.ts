import { utcClock, utcDay } from '../../shared/utc'
import type { DecisionRecord, EvidenceItem, Incident, IncidentOpened } from './medicTypes'
import { safeText } from './safeText'

const str = (v: unknown) => safeText(v)
const num = (v: unknown) => (typeof v === 'number' ? v : 0)

/** "13:41", with the day first when it is not the reference day ("Nov 2 · 13:41"). */
export function stamp(iso: string | null | undefined, ref?: string | null): string {
  const clock = utcClock(iso)
  if (!clock) return '—'
  const day = utcDay(iso)
  return ref && utcDay(ref) !== day ? `${day?.replace(/, \d{4}$/, '')} · ${clock}` : clock
}

/** "27 min", "1 h 38 m", "2 d 3 h"; "< 1 min" under a minute. */
export function spanWord(ms: number): string {
  const m = Math.floor(ms / 60_000)
  if (!(m >= 1)) return '< 1 min'
  if (m < 60) return `${m} min`
  if (m < 1440) return `${Math.floor(m / 60)} h ${m % 60} m`
  return `${Math.floor(m / 1440)} d ${Math.floor((m % 1440) / 60)} h`
}

/** "3 d 4 h" from seconds of uptime. */
export function uptimeWord(s: number): string {
  const m = Math.floor(s / 60)
  if (m < 60) return `${m} min`
  if (m < 1440) return `${Math.floor(m / 60)} h ${m % 60} m`
  return `${Math.floor(m / 1440)} d ${Math.floor((m % 1440) / 60)} h`
}

/** The sensor a watcher incident is about: its group's `sensor` value, else the first value. */
const sensorOf = (i: Incident) => (i.group.find((g) => g.name === 'sensor') ?? i.group[0])?.value

/** UX-11: the state column's words, from the summary. */
export function stateWord(i: Incident): string {
  if (i.state === 'resolved') {
    if (i.resolved_how === 'closed_quietly') return 'Closed quietly'
    if (i.resolved_how === 'rule_retired') return 'Resolved: rule retired'
    if (i.resolved_how === 'group_retired') return 'Resolved: group retired'
    return 'Resolved'
  }
  if (i.state === 'suppressed') return 'Suppressed'
  if (i.state === 'held') {
    const until = utcClock(i.held_until)
    return until ? `Held: upgrade in progress until ${until}` : 'Held'
  }
  if (i.subject === 'watcher') return `Medic can't see: ${str(sensorOf(i) ?? i.title)}`
  if (i.last_eval === 'unknown') return "Can't evaluate"
  if (i.reopen_count > 0) return 'Flapping'
  return i.state === 'resolving' ? 'Resolving' : 'Firing'
}

/** Red for what needs a look, amber for what is unsure, plain for the rest. */
export function stateTone(i: Incident): 'poor' | 'fair' | 'idle' {
  if (i.state === 'resolved' || i.state === 'suppressed' || i.state === 'held') return 'idle'
  if (i.last_eval === 'unknown' || i.reopen_count > 0 || i.state === 'resolving') return 'fair'
  return 'poor'
}

export function durationWord(i: Incident, now: number): string {
  if (i.reopen_count > 0) return `reopened ×${i.reopen_count}`
  const start = Date.parse(i.opened_at)
  const end = i.resolved_at ? Date.parse(i.resolved_at) : now
  return Number.isNaN(start) ? '—' : spanWord(end - start)
}

const FEEDBACK: Record<string, string> = { agree: 'Agree', disagree: 'Fix is wrong', wrong_lane: 'Wrong lane', false_alarm: 'False alarm' }
export const feedbackWord = (value: string, lane?: number) =>
  `${FEEDBACK[value] ?? str(value)}${value === 'wrong_lane' && lane ? ` (${lane})` : ''}`

/** The rating column (read-only): "—" when not rateable, "Unrated", else each admin's current rating. */
export function ratingWord(i: Incident): string | null {
  if (i.ratings.length > 0) {
    const words = new Map<string, number>()
    for (const r of i.ratings) {
      const w = feedbackWord(r.value, r.suggested_lane)
      words.set(w, (words.get(w) ?? 0) + 1)
    }
    return [...words].map(([w, n]) => (n > 1 ? `${w} ×${n}` : w)).join(', ')
  }
  return i.rateable ? null : '—'
}

const LANE_REASON: Record<string, string> = { rule: 'rule lane', lane1_gate_failed: 'lane 1 gate failed', unknown_signature: 'unknown signature' }

// UX-15: what each action reads as; `fix` is the pack's advice text, kept for notify_admin
function action(a: string, fix?: string): string {
  switch (a) {
    case 'record': return 'recorded this incident'
    case 'notify_admin': return `tell the admin${fix ? `: “${fix}”` : ''}`
    case 'propose_runbook': return 'propose the runbook'
    case 'run_runbook': return 'run the runbook'
    case 'prepare_support_bundle': return 'would have prepared a support bundle'
    default: return str(a)
  }
}

/** UX-15: the "would have" lines for L0, L1 and L2, always ending "Nothing was done." */
export function wouldHave(opened: IncidentOpened, lane: number, updates: DecisionRecord[]): string[] {
  const w = opened.would_have
  if (!w) return ['Nothing was done. Medic is in observe-only mode.']
  const fix = opened.advice?.fix
  const lines = ['Observe only (L0): recorded this incident.', `Recommend (L1): ${action(w.L1, fix)}`]
  const { action: a2, not_eligible: why } = w.L2 ?? {}
  let l2: string
  if (why === 'not_lane1' && lane === 3) l2 = 'the same'
  else if (why === 'not_lane1') l2 = 'the same notice. Lane 2 is yours to fix, so nothing runs'
  else if (why === 'no_runbook') l2 = 'would only have notified the admin. No runbook, so nothing runs'
  else if (why === 'runtime_untrusted') l2 = 'would only have proposed this, because a value came from untrusted input'
  else l2 = action(a2, fix)
  lines.push(`Autonomous (L2): ${l2}${l2.endsWith('.') ? '' : '.'}`)
  const esc = [...updates].reverse().find((r) => r.type === 'incident_updated' && r.body.change === 'escalation_simulated')
  if (esc) lines.push(`Still open after ${num(esc.body.simulated_failures)} fix attempts: would have escalated to support (lane unchanged).`)
  lines.push('Nothing was done. Medic is in observe-only mode.')
  return lines
}

export interface TimelineRow {
  key: string
  at: string
  text: string
  /** an incident this row points at (suppressed by it), opened from the row */
  link?: string
  /** free text the row carries (a comment), shown beneath as text */
  note?: string
}

const evalWord = (e: unknown) => (e === 'unknown' ? "Medic couldn't evaluate" : null)

/** UX-16: every record of the incident in `seq` order; `active_since` is its own earlier row. */
export function timeline(opened: DecisionRecord, records: DecisionRecord[]): TimelineRow[] {
  const o = opened.body as IncidentOpened
  const rows: TimelineRow[] = []
  if (o.active_since) rows.push({ key: 'since', at: o.active_since, text: 'Condition first true (active since)' })
  for (const r of [...records].sort((a, b) => a.seq - b.seq)) {
    const b = r.body
    const k = `${r.seq}`
    if (r.type === 'incident_opened') {
      const lane = b.lane as { value?: number; reason?: string } | undefined
      rows.push({ key: k, at: r.at, text: `Opened · lane ${num(lane?.value)} (${LANE_REASON[String(lane?.reason)] ?? str(lane?.reason)})` })
      if (b.held_by_upgrade_until) rows.push({ key: `${k}h`, at: r.at, text: `Held: upgrade in progress until ${stamp(String(b.held_by_upgrade_until), r.at)}` })
      else if (b.route === 'held') rows.push({ key: `${k}h`, at: r.at, text: 'Held for routing (a symptom waits 5 minutes for its cause)' })
      else rows.push({ key: `${k}r`, at: r.at, text: 'Routed' })
    } else if (r.type === 'incident_updated') {
      const unsure = evalWord(b.eval)
      switch (b.change) {
        case 'evidence_added': rows.push({ key: k, at: r.at, text: `Evidence added (${Array.isArray(b.evidence) ? b.evidence.length : 0})` }); break
        case 'resolving': rows.push({ key: k, at: r.at, text: unsure ? `Resolving: ${unsure}` : 'Resolving (condition false)' }); break
        case 'refiring': rows.push({ key: k, at: r.at, text: unsure ? `Firing again: ${unsure}` : 'Firing again (condition true)' }); break
        case 'routed': rows.push({ key: k, at: r.at, text: 'Routed' }); break
        case 'suppressed': rows.push({ key: k, at: r.at, text: 'Suppressed by', link: String(b.suppressed_by) }); break
        case 'unsuppressed': rows.push({ key: k, at: r.at, text: 'No longer suppressed: now routed' }); break
        case 'reopened': rows.push({ key: k, at: r.at, text: `Reopened (flapping) ×${num(b.reopen_count)}` }); break
        case 'escalation_simulated': rows.push({ key: k, at: r.at, text: `Escalation simulated after ${num(b.simulated_failures)} failed fix attempts` }); break
        default: rows.push({ key: k, at: r.at, text: `Updated: ${str(b.change)}` })
      }
    } else if (r.type === 'incident_resolved') {
      const how: Record<string, string> = { cleared: 'condition cleared', rule_retired: 'rule retired', group_retired: 'group retired', closed_quietly: 'closed quietly' }
      rows.push({ key: k, at: r.at, text: `Resolved (${how[String(b.how)] ?? str(b.how)})` })
    } else if (r.type === 'feedback') {
      const c = b.comment as { text?: string } | undefined
      rows.push({ key: k, at: r.at, text: `${str(b.admin)}: ${feedbackWord(String(b.value), b.suggested_lane as number | undefined)}`, note: c?.text })
    } else {
      rows.push({ key: k, at: r.at, text: `Record: ${str(r.type)}` })
    }
  }
  return rows
}

/** Evidence of the opened record, then of each evidence_added update, oldest first. */
export function evidenceOf(opened: IncidentOpened, records: DecisionRecord[]): EvidenceItem[] {
  const added = records
    .filter((r) => r.type === 'incident_updated' && r.body.change === 'evidence_added' && Array.isArray(r.body.evidence))
    .flatMap((r) => r.body.evidence as EvidenceItem[])
  return [...(opened.evidence ?? []), ...added]
}
