/** HH:MM:SS of an ISO timestamp, as written. */
export function clock(iso: string | null): string {
  return iso?.match(/[T ](\d\d:\d\d:\d\d)/)?.[1] ?? '—'
}
