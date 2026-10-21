/* The v3 Open questions tab's row model: the projection's open_questions (one list with the
   report's section 6) split into what the run could not settle, what the graph left unsettled,
   and what was flagged as possible prompt injection, each item worded from its fields. Kept out
   of WorkflowsScreen.tsx so it can be tested without rendering the screen. */

/** An event an item is about. alert: it is the alerted event. */
export interface V3OpenRef { id: string; label: string; alert: boolean }
export interface V3OpenLink { kind: string; source: V3OpenRef; target: V3OpenRef; confidence: number | null }
export interface V3Question {
  kind: string
  id: string
  /** The report's line, shown when the fields an item is worded from are missing. */
  text: string
  refs: V3OpenRef[]
  onChain: boolean
  link: V3OpenLink | null
  event: V3OpenRef | null
  refutations: { summary: string; turn: number | null }[]
  boundary: { origin: V3OpenRef | null; domain: string; reason: string } | null
  turn: number | null
  flagged: string[]
  question: string
}

export type OpenSectionKey = 'questions' | 'graph' | 'injection'
export interface OpenItem {
  kind: string
  id: string
  sentence: string
  /** "Disputed: 0.64." for a disputed link; empty otherwise. */
  standing: string
  /** One line of why: the latest refutation's first sentence, or a boundary's reason. */
  reason: string
  /** The full refutation, behind a click. */
  refutations: { summary: string; turn: number | null }[]
  refs: V3OpenRef[]
  onChain: boolean
  flagged: string[]
  turn: number | null
}
export interface OpenSection { key: OpenSectionKey; title: string; items: OpenItem[] }

const SECTION: Record<string, OpenSectionKey> = { model: 'questions', disputed: 'graph', 'ruled-out': 'graph', boundary: 'graph', flagged: 'injection' }
const TITLE: Record<OpenSectionKey, string> = { questions: 'Questions', graph: 'Unsettled in the graph', injection: 'Possible prompt injection' }
const SHORT = 60

/** A label short enough to sit in a sentence or a chip: up to its first dash clause, then cut at a word. */
export function shortLabel(label: string): string {
  const head = label.split(' — ')[0]!.trim()
  if (head.length <= SHORT) return head
  const cut = head.slice(0, SHORT)
  const at = cut.lastIndexOf(' ')
  return `${(at > SHORT / 2 ? cut.slice(0, at) : cut).replace(/[\s,;:(]+$/, '')}…`
}

/** The first sentence of a summary. */
export function firstSentence(text: string): string {
  return text.trim().split(/(?<=[.!?])\s+(?=[A-Z"“'(])/)[0] ?? ''
}

const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1)

/** An event in a sentence: the alert by its role, any other by its short label in quotes. */
function named(ref: V3OpenRef): string {
  if (ref.alert) return `the alert (${ref.id})`
  return ref.label ? `“${shortLabel(ref.label)}” (${ref.id})` : ref.id
}

/** An actor in a sentence: by its name as the logs give it. */
const actor = (ref: V3OpenRef) => (ref.label ? `${ref.label} (${ref.id})` : ref.id)

function linkSentence(link: V3OpenLink, ruledOut: boolean): string {
  const { kind, source, target } = link
  if (kind === 'attributed-to') return ruledOut ? `${cap(named(target))} did not run as ${actor(source)}.` : `Did ${named(target)} run as ${actor(source)}?`
  if (kind === 'operated-by') return ruledOut ? `${cap(named(target))} was not operated by ${actor(source)}.` : `Was ${named(target)} operated by ${actor(source)}?`
  return ruledOut ? `${cap(named(source))} did not cause ${named(target)}.` : `Did ${named(source)} cause ${named(target)}?`
}

/** One item in words. An item missing the fields it is worded from falls back to the report's line. */
export function openItem(q: V3Question): OpenItem {
  const base: OpenItem = { kind: q.kind, id: q.id, sentence: q.text, standing: '', reason: '', refutations: [], refs: q.refs, onChain: q.onChain, flagged: [], turn: q.turn }
  const latest = q.refutations[q.refutations.length - 1]
  const why = latest ? firstSentence(latest.summary) : ''
  switch (q.kind) {
    case 'model':
      return { ...base, sentence: q.question || q.text }
    case 'disputed':
      return q.link ? { ...base, sentence: linkSentence(q.link, false), standing: `Disputed: ${q.link.confidence === null ? '—' : q.link.confidence.toFixed(2)}.` } : base
    case 'ruled-out':
      if (q.link) return { ...base, sentence: linkSentence(q.link, true), reason: why, refutations: q.refutations }
      if (q.event) return { ...base, sentence: `${cap(named(q.event))} was ruled out.`, reason: why, refutations: q.refutations }
      return base
    case 'boundary': {
      const b = q.boundary
      if (!b) return base
      const of = b.origin ? named(b.origin) : 'an event'
      return { ...base, sentence: `The cause of ${of} is believed to be in ${b.domain || 'another source'}, where the run did not follow it.`, reason: b.reason }
    }
    case 'flagged':
      return { ...base, sentence: '', flagged: q.flagged.length ? q.flagged : [q.text] }
    default:
      return base
  }
}

/** The three sections, each holding only what is in it. Within one, what is on the chain to the
 *  alert comes first; otherwise the projection's order holds, which already puts those first
 *  within each kind. A section with nothing in it is left out. */
export function openSections(questions: readonly V3Question[]): OpenSection[] {
  const keys: OpenSectionKey[] = ['questions', 'graph', 'injection']
  return keys.flatMap((key) => {
    const mine = questions.filter((q) => (SECTION[q.kind] ?? 'graph') === key)
    const items = [...mine.filter((q) => q.onChain), ...mine.filter((q) => !q.onChain)].map(openItem)
    return items.length ? [{ key, title: TITLE[key], items }] : []
  })
}
