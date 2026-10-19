/** What an answer can cite on the case it was asked on, and how a chip names it (components/citation-chip.md). */
export interface Citation {
  /** The id the answer writes: an evidence row, an alert or an explanation. */
  id: string
  kind: 'evidence' | 'alert' | 'explanation'
  /** What the chip reads, e.g. "step 3 · CrowdStrike". */
  label: string
  /** The cited row's own text, shown on hover. */
  text: string
  /** Set when the chip opens a page rather than a tab of the case. */
  href?: string
}

const ID_EDGE = 'A-Za-z0-9_-'

function escape(id: string): string {
  return id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** Matches any of `ids` standing alone (not inside a longer id); null when there are none. */
export function citePattern(ids: readonly string[]): RegExp | null {
  const usable = [...new Set(ids.filter((id) => id.length > 0))].sort((a, b) => b.length - a.length)
  if (usable.length === 0) return null
  return new RegExp(`(?<![${ID_EDGE}])(?:${usable.map(escape).join('|')})(?![${ID_EDGE}])`, 'g')
}

/** The citations `text` makes, once each, in the order it first makes them. */
export function citedIn(text: string, citations: readonly Citation[]): Citation[] {
  const pattern = citePattern(citations.map((c) => c.id))
  if (!pattern) return []
  const byId = new Map(citations.map((c) => [c.id, c]))
  const seen = new Set<string>()
  const out: Citation[] = []
  for (const match of text.matchAll(pattern)) {
    const cite = byId.get(match[0])
    if (cite && !seen.has(cite.id)) {
      seen.add(cite.id)
      out.push(cite)
    }
  }
  return out
}
