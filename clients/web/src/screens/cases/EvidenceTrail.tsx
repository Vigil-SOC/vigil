import { useEffect, useRef } from 'react'
import { InfoTip } from '../../shared/InfoTip'
import { EmptyState } from '../../shared/ui'
import { STANCE_WORD, TIER_WORD, leadingExplanation, relationWord, stanceOn, stanceTotals, workerAt, type RunFold, type Stance } from './caseFold'
import type { Phase } from './useCases'

interface TrailRow {
  id: string
  step: string
  observation: string
  bearsOn: string
  links: string[]
  source: string
  tier: string
  stance: Stance | null
  by: string
}

const DASH = '—'

function rowsOf(fold: RunFold): TrailRow[] {
  if (fold.kind === 'lead') {
    return fold.findings.map((row, i) => ({
      id: `${row.agent_id}-${i}`,
      step: String(i + 1),
      observation: row.answer || DASH,
      bearsOn: '',
      links: [],
      source: row.agent_id || DASH,
      tier: DASH,
      stance: null,
      by: row.agent_id || DASH,
    }))
  }
  const statements = new Map(fold.hypotheses.map((h) => [h.hypothesis_id, h.statement]))
  const leading = leadingExplanation(fold)?.hypothesis_id
  return fold.evidence.map((row) => ({
    id: row.evidence_id,
    step: String(row.iteration),
    observation: row.is_gap ? `${row.summary} (gap)` : row.summary || DASH,
    bearsOn: row.bears_on
      .map((link) => {
        const statement = statements.get(link.hypothesis_id) || link.hypothesis_id
        return row.bears_on.length > 1 ? `${relationWord(link.relation)} ${statement}` : statement
      })
      .join(' · '),
    links: row.bears_on.map((link) => link.relation),
    source: row.source_system || DASH,
    tier: (row.source_tier && TIER_WORD[row.source_tier]) || DASH,
    stance: stanceOn(row, leading),
    by: workerAt(fold, row.iteration) ?? DASH,
  }))
}

function header(fold: RunFold): string {
  const count = fold.kind === 'hunt' ? fold.evidenceCount : fold.findings.length
  const head = `Evidence trail · ${count} ${count === 1 ? 'row' : 'rows'}`
  if (fold.kind !== 'hunt') return head
  const n = stanceTotals(fold)
  return `${head} · ${n.supports} support · ${n.weakens} go against · ${n.neither} neither`
}

/** The ⓘ under the totals: a row is counted once, so which explanation it is read against is named. */
function totalsNote(fold: RunFold): string | null {
  if (fold.kind !== 'hunt') return null
  const leading = leadingExplanation(fold)
  const name = leading && (leading.statement || leading.hypothesis_id)
  return `Each row is counted once, by its stance on the leading explanation${name ? ` (${name.length > 70 ? `${name.slice(0, 70)}…` : name})` : ''}; a row that does not bear on it counts as neither. Totals cover the rows shown and leave out critic rows, which are listed but not counted. A row's stance on each explanation is under “bears on”.`
}

export function EvidenceTrail({ fold, phase, focusId }: { fold: RunFold | null; phase: Phase; focusId: string | null }) {
  const focusRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const node = focusRef.current
    if (node && typeof node.scrollIntoView === 'function') node.scrollIntoView({ block: 'nearest' })
  }, [focusId, fold])

  if (phase === 'loading') return <p>Loading the run…</p>
  if (phase === 'error') return <p>The run could not be read.</p>
  const rows = fold ? rowsOf(fold) : []
  if (!fold || rows.length === 0) return <EmptyState compact icon="shield" title="No evidence yet" />
  const note = totalsNote(fold)

  return (
    <section className="ev-trail" aria-label="Evidence trail">
      <div>
        <div className="ev-title">
          <h3>{header(fold)}</h3>
          {note && <InfoTip label="About the totals" text={note} align="start" />}
        </div>
        <p className="muted">Each observation, its source and tier, and whether it supports or goes against an explanation.</p>
      </div>
      <div className="ev-table" role="table" aria-label="Evidence trail rows">
        <div className="ev-grid ev-head" role="row">
          <span role="columnheader">Step</span>
          <span role="columnheader">Observation · bears on</span>
          <span role="columnheader">
            Source · tier
            <InfoTip label="About the tier" text="Tier as configured now." align="start" />
          </span>
          <span role="columnheader">Stance</span>
          <span role="columnheader">By</span>
          <span role="columnheader" aria-label="Actions" />
        </div>
        <div className="ev-rows" role="rowgroup">
          {rows.map((row) => (
            <div
              key={row.id}
              ref={row.id === focusId ? focusRef : undefined}
              className={`ev-grid ev-row${row.id === focusId ? ' cite-target' : ''}`}
              role="row"
              data-evidence-id={row.id}
            >
              <span role="cell" className="ev-step">{row.step}</span>
              <div role="cell" className="ev-obs">
                <div className="ev-clamp" title={row.observation}>{row.observation}</div>
                {row.bearsOn && <div className="ev-sub ev-line" title={row.bearsOn}>{row.bearsOn}</div>}
              </div>
              <div role="cell" className="ev-src">
                <div className="ev-line" title={row.source}>{row.source}</div>
                <div className="ev-sub ev-line">{row.tier}</div>
              </div>
              <span role="cell" className={`ev-stance ${row.stance ?? ''}`}>{row.stance ? STANCE_WORD[row.stance] : DASH}</span>
              <span role="cell" className="ev-by ev-line" title={row.by}>{row.by}</span>
              <span role="cell">
                <button type="button" className="ev-act" disabled aria-label="Actions for this row" title="Coming in a later release">
                  ···
                </button>
              </span>
            </div>
          ))}
        </div>
      </div>
    </section>
  )
}
