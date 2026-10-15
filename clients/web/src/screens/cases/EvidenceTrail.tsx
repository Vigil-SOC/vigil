import { useEffect, useRef, useState } from 'react'
import { InfoTip } from '../../shared/InfoTip'
import { EmptyState } from '../../shared/ui'
import { LATER_RELEASE } from '../../shared/later'
import { STANCE_WORD, TIER_WORD, evidenceSource, huntHypothesis, relationWord, stanceOn, stanceTotals, workerAt, type EvidenceRow, type RunFold, type Stance, type ValueCheck } from './caseFold'
import { CallDetail } from './RunChecks'
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
  /** The hunt record behind the row, opened in place; absent on a lead's answer. */
  record?: EvidenceRow
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
  const against = huntHypothesis(fold)?.hypothesis_id
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
    source: evidenceSource(row) || DASH,
    tier: (row.source_tier && TIER_WORD[row.source_tier]) || DASH,
    stance: stanceOn(row, against),
    by: workerAt(fold, row.iteration) ?? DASH,
    record: row,
  }))
}

const AUTHORED_WORD: Record<string, string> = { sensor: 'sensor', adversary: 'not attested', third_party: 'third party' }

const CHECK_WORD: Record<ValueCheck['status'], string> = { match: '✓ match', differs: '≠ differs', absent: '— not in the query' }

function value(held: unknown): string {
  if (held === null || held === undefined) return DASH
  if (Array.isArray(held)) return held.map(value).join(', ')
  if (typeof held === 'object') return JSON.stringify(held)
  if (typeof held === 'number') return held.toLocaleString()
  if (typeof held === 'string' && /^-?\d{4,}$/.test(held)) return Number(held).toLocaleString()
  return String(held)
}

/** The words of a field name, a plural read as its singular: what lets conn_count meet count. */
function words(name: string): string[] {
  return name.toLowerCase().split(/[^a-z0-9]+/).filter(Boolean).map((w) => (w.length > 3 && w.endsWith('s') ? w.slice(0, -1) : w))
}

function sameField(a: string, b: string): boolean {
  const [long, short] = words(a).length >= words(b).length ? [words(a), words(b)] : [words(b), words(a)]
  if (short.length === 0) return false
  for (let i = 0; i + short.length <= long.length; i += 1) if (short.every((w, j) => long[i + j] === w)) return true
  return false
}

/** The record whole, as a receipt: each value it was written over beside what its
 *  query returned, then what it bears on, and the query itself on demand. */
function EvidenceDetail({ record, statements, runId, onAsk }: { record: EvidenceRow; statements: Map<string, string>; runId: string | null; onAsk?: (text: string) => void }) {
  const [shown, setShown] = useState<string | null>(null)
  const refs = record.source_refs.length > 0 ? record.source_refs : record.source_ref ? [record.source_ref] : []
  const checks: ValueCheck[] =
    record.checks.length > 0
      ? record.checks
      : Object.entries(record.payload ?? {})
          .filter(([key]) => key !== 'finding_id')
          .map(([field, claimed]) => ({ field, claimed, status: 'absent' as const }))
  const checked = record.checks.length > 0
  const counts = { match: 0, differs: 0, absent: 0 }
  for (const check of checks) counts[check.status] += 1
  const differing = checks.filter((check) => check.status === 'differs')
  const restsOn = (field: string) => record.rests_on.find((basis) => sameField(basis.field, field))
  return (
    <div className="ev-detail" role="region" aria-label="Evidence detail">
      <div className="ev-receipt-head">
        <span className="call-label">{checked ? 'Checked against its query' : 'Values it was written over'}</span>
        {refs.map((ref) => <span key={ref} className="ev-ref mono">{ref}</span>)}
        {checked && (
          <>
            {counts.match > 0 && <span className="ev-pill match">{counts.match} match</span>}
            {counts.differs > 0 && <span className="ev-pill differs">{counts.differs} differ{counts.differs === 1 ? 's' : ''}</span>}
            {counts.absent > 0 && <span className="ev-pill absent">{counts.absent} not in the query</span>}
          </>
        )}
        {!checked && <span className="ev-pill differs">{refs.length > 0 ? 'Not checked: recorded before checks' : 'Not tied to a query'}</span>}
      </div>
      {checks.length === 0 ? (
        <p className="muted">No values recorded.</p>
      ) : (
        <div className="ev-receipt-wrap">
          <table className="ev-receipt">
            <thead>
              <tr>
                <th>Value</th>
                <th>The record says</th>
                <th>The query returned</th>
                <th>Check</th>
                <th>Verdict rests on it</th>
              </tr>
            </thead>
            <tbody>
              {checks.map((check) => {
                const basis = restsOn(check.field)
                return (
                  <tr key={check.field} className={check.status}>
                    <td className="mono field">{check.field}</td>
                    <td className="mono">{value(check.claimed)}</td>
                    <td className="mono found">
                      {check.status === 'absent' ? (checked ? 'not in the cited result' : DASH) : `${check.found_field ? `${check.found_field} ` : ''}${check.found === undefined ? '' : value(check.found)}${refs.length > 1 && check.ref ? ` · ${check.ref}` : ''}`}
                    </td>
                    <td className={`check ${check.status}`}>{checked ? `${CHECK_WORD[check.status]}${check.rounded ? ' · rounded' : ''}` : DASH}</td>
                    <td className={basis ? `rests ${basis.authored}` : 'rests'}>{basis ? `yes · ${AUTHORED_WORD[basis.authored] ?? basis.authored}` : DASH}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
      <div className="ev-detail-grid">
        <div>
          <span className="call-label">Bears on</span>
          {record.bears_on.length === 0 ? (
            <p className="muted">No explanation.</p>
          ) : (
            <ul className="ev-bears">
              {record.bears_on.map((link) => (
                <li key={link.hypothesis_id}>
                  <b className={link.relation}>{relationWord(link.relation)}</b> {statements.get(link.hypothesis_id) || link.hypothesis_id}
                </li>
              ))}
            </ul>
          )}
        </div>
        <div>
          <span className="call-label">Why the hunt flagged it</span>
          <p>
            {record.why_notable || DASH}
            {record.attack_technique ? ` ATT&CK ${record.attack_technique}.` : ''}
          </p>
          <p className={record.sensor_attested ? 'ev-attest good' : 'ev-attest poor'}>
            {record.sensor_attested ? 'Attested by the telemetry' : 'Not attested: it cannot carry a verdict alone'}
          </p>
        </div>
      </div>
      <div className="ev-source">
        <div className="ev-source-actions">
          {runId && refs.map((ref) => (
            <button key={ref} type="button" className="btn neutral" aria-expanded={shown === ref} onClick={() => setShown(shown === ref ? null : ref)}>
              {shown === ref ? 'Hide' : 'Show'} query <span className="mono">{ref}</span> and its rows
            </button>
          ))}
          {onAsk && differing.length > 0 && (
            <button
              type="button"
              className="btn ghost"
              onClick={() =>
                onAsk(
                  `Evidence ${record.evidence_id} says ${differing.map((c) => `${c.field} = ${value(c.claimed)}`).join(', ')}, but its query ${differing[0]!.ref ?? ''} returned ${differing.map((c) => `${c.found_field} = ${value(c.found)}`).join(', ')}. Why does it differ, and does it change the finding?`,
                )
              }
            >
              Ask why it differs
            </button>
          )}
        </div>
        {shown && runId && <CallDetail runId={runId} callRef={shown} />}
      </div>
    </div>
  )
}

function header(fold: RunFold): string {
  const count = fold.kind === 'hunt' ? fold.evidenceCount : fold.findings.length
  const head = `Evidence trail · ${count} ${count === 1 ? 'row' : 'rows'}`
  if (fold.kind !== 'hunt') return head
  const shown = fold.evidence.length < count ? ` · showing ${fold.evidence.length} of ${count}` : ''
  const n = stanceTotals(fold)
  return n ? `${head}${shown} · ${n.supports} support · ${n.weakens} go against · ${n.neither} neither` : `${head}${shown}`
}

/** The ⓘ under the totals: a row is counted once, so which explanation it is read against is named. */
function totalsNote(fold: RunFold): string | null {
  if (fold.kind !== 'hunt') return null
  const against = huntHypothesis(fold)
  if (!against) return null
  const name = against.statement || against.hypothesis_id
  const slice = fold.evidence.length < fold.evidenceCount ? ` The table lists the newest ${fold.evidence.length}; “Show older” loads more, and the totals already cover every row.` : ''
  return `Each row is counted once, by its stance on the explanation this hunt set out to test (${name.length > 70 ? `${name.slice(0, 70)}…` : name}); a row that does not bear on it counts as neither. Totals cover every row the run holds and leave out critic rows, which are listed but not counted.${slice} A row's stance on each explanation is under “bears on”.`
}

export function EvidenceTrail({ fold, phase, focusId, runId = null, onAsk, onShowOlder }: { fold: RunFold | null; phase: Phase; focusId: string | null; runId?: string | null; onAsk?: (text: string) => void; onShowOlder?: (shown: number) => void }) {
  // The row count asked for, until a read answers it.
  const [asked, setAsked] = useState<number | null>(null)
  const [open, setOpen] = useState<string | null>(null)
  useEffect(() => setAsked(null), [fold])
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
  const statements = new Map(fold.kind === 'hunt' ? fold.hypotheses.map((h) => [h.hypothesis_id, h.statement]) : [])

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
            <div key={row.id} className={open === row.id ? 'ev-item open' : 'ev-item'}>
            <div
              ref={row.id === focusId ? focusRef : undefined}
              className={`ev-grid ev-row${row.id === focusId ? ' cite-target' : ''}${row.record ? ' ev-openable' : ''}`}
              role="row"
              data-evidence-id={row.id}
              onClick={row.record ? () => setOpen(open === row.id ? null : row.id) : undefined}
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
                {row.record ? (
                  <button
                    type="button"
                    className="ev-act"
                    aria-expanded={open === row.id}
                    aria-label={open === row.id ? 'Close this evidence' : 'Open this evidence'}
                    onClick={(event) => {
                      event.stopPropagation()
                      setOpen(open === row.id ? null : row.id)
                    }}
                  >
                    {open === row.id ? '▾' : '▸'}
                  </button>
                ) : (
                  <button type="button" className="ev-act" disabled aria-label="Actions for this row" title={LATER_RELEASE}>
                    ···
                  </button>
                )}
              </span>
            </div>
            {open === row.id && row.record && <EvidenceDetail record={row.record} statements={statements} runId={runId} onAsk={onAsk} />}
            </div>
          ))}
        </div>
        {fold.kind === 'hunt' && fold.evidence.length < fold.evidenceCount && onShowOlder && (
          <button
            type="button"
            className="btn neutral"
            disabled={asked === fold.evidence.length}
            onClick={() => {
              setAsked(fold.evidence.length)
              onShowOlder(fold.evidence.length)
            }}
          >
            {asked === fold.evidence.length ? 'Loading older rows…' : 'Show older'}
          </button>
        )}
      </div>
    </section>
  )
}
