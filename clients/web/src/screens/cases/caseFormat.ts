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
