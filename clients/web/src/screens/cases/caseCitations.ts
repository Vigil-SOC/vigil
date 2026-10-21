import { basePath } from '../../config/basePath'
import type { Citation } from '../../shell/citations'
import { evidenceSource, type RunFold } from './caseFold'
import type { CaseLinkedFinding } from './useCases'

/**
 * What an answer on this case can cite, named as its chip reads: an evidence row
 * by the step and source it came from, an alert by its title, an explanation by
 * its statement. The case brief gives the model these ids (core/cases/case_brief.py).
 */
export function caseCitations(fold: RunFold | null, alerts: readonly CaseLinkedFinding[]): Citation[] {
  const out: Citation[] = []
  if (fold?.kind === 'hunt') {
    const rows: Citation[] = []
    for (const row of fold.evidence) {
      if (!row.evidence_id) continue
      const source = row.is_gap ? 'blind spot' : evidenceSource(row)
      rows.push({
        id: row.evidence_id,
        kind: 'evidence',
        // a row from before the first step (a deployment gap) names no step
        label: [row.iteration > 0 && `step ${row.iteration}`, source].filter(Boolean).join(' · '),
        text: row.is_gap ? row.gap_detail || row.summary : row.summary,
      })
    }
    out.push(...distinct(rows))
    for (const h of fold.hypotheses) {
      if (!h.hypothesis_id) continue
      const statement = h.statement || h.hypothesis_id
      out.push({ id: h.hypothesis_id, kind: 'explanation', label: `explanation · ${statement}`, text: statement })
    }
  }
  for (const alert of alerts) {
    const name = alert.title || alert.finding_id
    out.push({
      id: alert.finding_id,
      kind: 'alert',
      label: `alert · ${name}`,
      text: alert.description || name,
      // its own page, in a new tab, so the case stays open
      href: `${basePath}/overview?alert=${encodeURIComponent(alert.finding_id)}`,
    })
  }
  return out
}

/** An evidence row's id as a hunt writes it. */
const EVIDENCE_ID = /\bev-[A-Za-z0-9]+\b/g
/** A bracketed or parenthesised list of nothing but evidence ids, with the space before it. */
const EVIDENCE_LIST = /\s*[[(]\s*ev-[A-Za-z0-9]+(?:\s*[,;]\s*ev-[A-Za-z0-9]+)*\s*[\])]/g

/**
 * A note with the evidence ids it cites taken out (#2459): "abandoned: … [ev-b680…, ev-6e07…]" reads
 * "abandoned: …", and the ids come back once each, in order, to be named as chips or counted in words.
 */
export function evidenceInNote(note: string): { text: string; ids: string[] } {
  const ids = [...new Set(note.match(EVIDENCE_ID) ?? [])]
  if (ids.length === 0) return { text: note, ids }
  const text = note
    .replace(EVIDENCE_LIST, '')
    .replace(EVIDENCE_ID, '')
    .replace(/\s+([.,;:])/g, '$1')
    .replace(/\s{2,}/g, ' ')
    .trim()
  return { text, ids }
}

/** Longest distinguisher a chip carries, so the 22px mono chip stays readable. */
const HINT_CHARS = 16

/** The first words of a row's text, cut at a word to fit a chip. */
function hint(text: string): string {
  const words = text.replace(/\s+/g, ' ').trim().split(' ')
  let out = ''
  for (const word of words) {
    const next = out ? `${out} ${word}` : word
    if (next.length > HINT_CHARS) return out ? `${out}…` : `${word.slice(0, HINT_CHARS - 1)}…`
    out = next
  }
  return out
}

/** Rows that would share a label get the first words of their own text added; a label no other row has stays as it is. */
function distinct(rows: Citation[]): Citation[] {
  const count = new Map<string, number>()
  for (const row of rows) count.set(row.label, (count.get(row.label) ?? 0) + 1)
  const used = new Set(rows.filter((row) => count.get(row.label) === 1).map((row) => row.label))
  return rows.map((row) => {
    if (count.get(row.label) === 1) return row
    const base = [row.label, hint(row.text)].filter(Boolean).join(' · ')
    // two rows with the same text still read apart
    let label = base
    for (let n = 2; used.has(label); n++) label = `${base} (${n})`
    used.add(label)
    return { ...row, label }
  })
}
