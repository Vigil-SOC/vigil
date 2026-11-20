import { stepUpDetailOf } from '../services/stepUp'

/** The API's `detail` or `error` string, else the error's own message, else the fallback. */
export function errorText(error: unknown, fallback: string): string {
  // a cancelled "Confirm it's you": the caller's message, not axios's "status code 403"
  if (stepUpDetailOf(error)) return fallback
  const data = (error as { response?: { data?: { detail?: unknown; error?: unknown } } })?.response?.data
  if (typeof data?.detail === 'string' && data.detail.trim()) return data.detail
  if (typeof data?.error === 'string' && data.error.trim()) return data.error
  const message = (error as { message?: unknown })?.message
  return typeof message === 'string' && message.trim() ? message : fallback
}
