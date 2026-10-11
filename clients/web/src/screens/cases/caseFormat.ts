import { utcDayClock } from '../../shared/utc'

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
