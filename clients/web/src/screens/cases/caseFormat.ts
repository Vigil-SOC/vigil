import { fmtCost } from '../../shared/cost'
import { utcClock, utcDayClock } from '../../shared/utc'
import type { CaseInvestigationRef } from './useCases'

export function when(value?: string | null): string {
  if (!value) return '—'
  return utcDayClock(value) ?? value
}

/** Resolve-by clock: "7 h left", or "2 d over" once past due. */
export function timeLeft(due: string): string {
  const ms = new Date(due).getTime() - Date.now()
  if (Number.isNaN(ms)) return ''
  const min = Math.round(Math.abs(ms) / 60_000)
  const span = min < 60 ? `${min} min` : min < 48 * 60 ? `${Math.round(min / 60)} h` : `${Math.round(min / 1440)} d`
  return `${span} ${ms < 0 ? 'over' : 'left'}`
}

/** A run's name on the case: its workflow and when it started, else a short id. The one label for every tag, filter entry and cost line. */
export function runLabel(ref: CaseInvestigationRef, names: Record<string, string>): string {
  const name = names[ref.workflow_id] || ref.workflow_id
  const clock = ref.created_at ? utcClock(utcStamp(ref.created_at)) : null
  return name ? (clock ? `${name} · started ${clock}` : name) : ref.run_id.slice(0, 8)
}

/** A stamp read as UTC: the server sends some without an offset, which a browser would read as local time. */
export function utcStamp(at: string): string {
  return /(Z|[+-]\d\d:?\d\d)$/.test(at) ? at : `${at}Z`
}

/** What several runs cost together, and how many had no price to add; null is a run nothing priced, never $0. */
export function costAcross(costs: (number | null)[]): { total: string; unpriced: number } {
  const priced = costs.flatMap((cost) => (cost === null ? [] : [cost]))
  return { total: priced.length ? fmtCost(priced.reduce((a, b) => a + b, 0)) : '—', unpriced: costs.length - priced.length }
}
