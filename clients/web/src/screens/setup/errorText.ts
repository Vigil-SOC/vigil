/** The API's `detail` string when it sent one, else the fallback. */
export function errorText(err: unknown, fallback: string): string {
  if (typeof err === 'object' && err && 'response' in err) {
    const detail = (err as { response?: { data?: { detail?: unknown } } }).response?.data?.detail
    if (typeof detail === 'string' && detail) return detail
  }
  return fallback
}
