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
    for (const row of fold.evidence) {
      if (!row.evidence_id) continue
      const source = row.is_gap ? 'blind spot' : evidenceSource(row)
      out.push({
        id: row.evidence_id,
        kind: 'evidence',
        label: [`step ${row.iteration}`, source].filter(Boolean).join(' · '),
        text: row.is_gap ? row.gap_detail || row.summary : row.summary,
      })
    }
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
