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

/** The one name for a run on the case page: its workflow and when it started, else a short id. */
export function runLabel(ref: CaseInvestigationRef, names: Record<string, string>): string {
  const name = names[ref.workflow_id] || ref.workflow_id || ref.run_id.slice(0, 8)
  // refs carry naive UTC stamps; without an offset a browser would read them as local time
  const at = ref.created_at && (/(Z|[+-]\d\d:?\d\d)$/.test(ref.created_at) ? ref.created_at : `${ref.created_at}Z`)
  const clock = utcClock(at)
  return clock ? `${name} · started ${clock}` : name
}

/** What several runs spent together, null when none is priced, and how many had no price to add: a run nothing priced is counted, never $0. */
export function costAcross(costs: (number | null)[]): { total: string | null; unpriced: number } {
  const priced = costs.flatMap((cost) => (cost === null ? [] : [cost]))
  return { total: priced.length ? fmtCost(priced.reduce((a, b) => a + b, 0)) : null, unpriced: costs.length - priced.length }
}

export function errorDetail(error: unknown, fallback: string): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  return (error as { message?: string })?.message || fallback
}
