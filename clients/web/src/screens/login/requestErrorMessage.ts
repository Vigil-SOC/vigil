const UNREACHABLE_MSG = "Can't reach the Vigil API. Is the backend running?"

// No response, or a 5xx without a server `detail` (e.g. Vite's proxy answering
// ECONNREFUSED with a 500), means the API never saw the request.
export function requestErrorMessage(err: any, fallback: string): string {
  const status: number = err?.response?.status
  const detail = err?.response?.data?.detail
  const hasDetail = typeof detail === 'string' && detail.length > 0
  if (!err?.response || (status >= 500 && !hasDetail)) return UNREACHABLE_MSG
  return hasDetail ? detail : fallback
}
