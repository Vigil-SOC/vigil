// Finding and case ids as Vigil writes them (f-20261028-3b5c585e, INC-1042, case-2026-0142, a UUID).
const LOOKS_LIKE_ID = /^(f-\d{8}-[\w-]+|inc-h?\d+|case-[\w-]+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i

/** The id a Run field means: the one listed row whose id or title holds the text (as the list filters), or an id
 *  the list did not load. Null when it names no single row: a title, host or user the server would take as an id. */
export function pickId(text: string, opts: { id: string; label: string }[]): string | null {
  const t = text.trim()
  if (!t || opts.some((o) => o.id === t)) return t
  const low = t.toLowerCase()
  const hits = opts.filter((o) => o.id.toLowerCase().includes(low) || o.label.toLowerCase().includes(low))
  if (hits.length === 1) return hits[0].id
  return hits.length === 0 && LOOKS_LIKE_ID.test(t) ? t : null
}
