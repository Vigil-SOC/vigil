const UNREACHABLE_MSG = "Can't reach the Vigil API. Is the backend running?"

// No response, or a 5xx without a server `detail` (e.g. Vite's proxy answering
// ECONNREFUSED with a 500), means the API never saw the request.
export function requestErrorMessage(err: any, fallback: string): string {
  const status: number = err?.response?.status
  const raw = err?.response?.data?.detail
  // a 422's detail is a list of {loc, msg}
  const detail = Array.isArray(raw) ? raw.map((d) => d?.msg).filter(Boolean).join('; ') : raw
  const hasDetail = typeof detail === 'string' && detail.length > 0
  if (!err?.response || (status >= 500 && !hasDetail)) return UNREACHABLE_MSG
  // slowapi answers 429 with `error`, not `detail`
  if (status === 429) return 'Too many attempts. Wait a while and try again.'
  return hasDetail ? detail : fallback
}
