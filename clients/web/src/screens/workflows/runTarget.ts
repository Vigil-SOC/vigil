/** The id a Run field means: the text when it is a listed id, else the one listed row whose id or title
 *  holds it (as the list filters). Null when several rows hold it. When none does, the text itself, for
 *  the caller to look up: the list loads only the newest, and a pasted id may be older. */
export function pickId(text: string, opts: { id: string; label: string }[]): string | null {
  const t = text.trim()
  if (!t || opts.some((o) => o.id === t)) return t
  const low = t.toLowerCase()
  const hits = opts.filter((o) => o.id.toLowerCase().includes(low) || o.label.toLowerCase().includes(low))
  if (hits.length > 1) return null
  return hits.length === 1 ? hits[0].id : t
}
