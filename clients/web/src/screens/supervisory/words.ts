import type { SupervisedFunction } from './supervisoryTypes'

export const FN_LABEL: Record<SupervisedFunction, string> = {
  triage: 'Triage',
  investigation: 'Investigation',
  response: 'Response',
}

/** "6 min ago", "2 h ago", "3 d ago"; "just now" under a minute. */
export function ago(iso: string | null, now = Date.now()): string {
  const t = iso ? Date.parse(iso) : NaN
  if (Number.isNaN(t)) return '—'
  const s = Math.max(0, Math.round((now - t) / 1000))
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)} min ago`
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`
  return `${Math.floor(s / 86400)} d ago`
}

/** "3 h 12 m" since a stamp. */
export function since(iso: string, now = Date.now()): string {
  const m = Math.max(0, Math.floor((now - Date.parse(iso)) / 60_000))
  if (m < 60) return `${m} m`
  const h = Math.floor(m / 60)
  return h < 24 ? `${h} h ${m % 60} m` : `${Math.floor(h / 24)} d ${h % 24} h`
}
