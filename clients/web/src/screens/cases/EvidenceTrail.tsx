import { useEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import { InfoTip } from '../../shared/InfoTip'
import { EmptyState } from '../../shared/ui'
import { LATER_RELEASE } from '../../shared/later'
import { EVIDENCE_PAGE, STANCE_WORD, TIER_WORD, evidenceSource, foldRecord, huntHypothesis, relationWord, stanceOn, stanceTotals, workerAt, type RunFold, type RunRecord, type Stance } from './caseFold'
import { RunNote } from './RunChecks'
import { utcStamp } from './caseFormat'
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
  /** When it was captured (ISO); empty for a row that carries no time. */
  at: string
  /** The run it came from, set only where the table merges runs. */
  runId?: string
  tag?: string
}

const DASH = '—'

/**
 * What a row bears on: "Supports X · Goes against Y" over several explanations, the statement alone
 * for one. The explanations it bears on neither way are left out, and a row that bears on none of
 * them says so once (#2458).
 */
function bearsOnLine(row: RunRecord['evidence'][number], record: RunRecord): string {
  if (row.bears_on.length === 0) return ''
  const statements = new Map(record.hypotheses.map((h) => [h.hypothesis_id, h.statement]))
  const bearing = row.bears_on.filter((link) => link.relation !== 'neither')
  if (bearing.length === 0) return record.hypotheses.length === 2 ? 'Bears on neither explanation' : 'Bears on no explanation'
  return bearing
    .map((link) => {
      const statement = statements.get(link.hypothesis_id) || link.hypothesis_id
      return row.bears_on.length > 1 ? `${relationWord(link.relation)} ${statement}` : statement
    })
    .join(' · ')
}

function rowsOf(fold: RunFold): TrailRow[] {
  if (fold.kind === 'head') return [] // a root-cause chain is on Watch a run
  if (fold.kind === 'lead' && fold.record) {
    // An investigation tests no one explanation, so a row's stance is shown only where its links agree.
    const record = fold.record
    return record.evidence.map((row) => {
      const relations = new Set(row.bears_on.map((link) => link.relation))
      const [only] = relations
      return {
        id: row.evidence_id,
        step: String(row.iteration),
        observation: row.summary || DASH,
        bearsOn: bearsOnLine(row, record),
        links: row.bears_on.map((link) => link.relation),
        source: evidenceSource(row) || DASH,
        tier: DASH,
        stance: relations.size === 1 && (only === 'supports' || only === 'weakens') ? only : null,
        by: fold.findings[0]?.agent_id || 'agent',
        at: row.captured_at,
      }
    })
  }
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
      at: '',
    }))
  }
  const against = huntHypothesis(fold)?.hypothesis_id
  return fold.evidence.map((row) => ({
    id: row.evidence_id,
    step: String(row.iteration),
    observation: row.is_gap ? `${row.summary} (gap)` : row.summary || DASH,
    bearsOn: bearsOnLine(row, fold),
    links: row.bears_on.map((link) => link.relation),
    source: evidenceSource(row) || DASH,
    tier: (row.source_tier && TIER_WORD[row.source_tier]) || DASH,
    stance: stanceOn(row, against),
    by: workerAt(fold, row.iteration) ?? DASH,
    at: row.captured_at,
  }))
}

const timeOf = (at: string): number => {
  const ms = at ? Date.parse(utcStamp(at)) : NaN
  return Number.isNaN(ms) ? -Infinity : ms
}

/**
 * Every run's rows as one list, newest first by capture time. Each run keeps its own order (a k-way
 * merge, so a run's rows never swap); a row with no usable time sorts after those with one, in run order.
 */
function mergeRows(runs: { id: string; tag: string; rows: TrailRow[] }[]): TrailRow[] {
  const queues = runs.map((run) => run.rows.map((row) => ({ ...row, runId: run.id, tag: run.tag })))
  const merged: TrailRow[] = []
  for (;;) {
    let best = -1
    queues.forEach((queue, i) => {
      if (queue.length > 0 && (best < 0 || timeOf(queue[0].at) > timeOf(queues[best][0].at))) best = i
    })
    if (best < 0) return merged
    merged.push(queues[best].shift()!)
  }
}

function header(fold: RunFold): string {
  const count = foldRecord(fold)?.evidenceCount ?? (fold.kind === 'lead' ? fold.findings.length : 0)
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

function TrailTable({ rows, focusId, focusRunId, focusRef, children }: {
  rows: TrailRow[]
  focusId: string | null
  /** Where rows of several runs share ids, the run the focus belongs to. */
  focusRunId?: string | null
  focusRef: RefObject<HTMLDivElement>
  children?: ReactNode
}) {
  const focused = (row: TrailRow) => row.id === focusId && (focusRunId === undefined || row.runId === focusRunId)
  return (
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
            key={row.runId ? `${row.runId}:${row.id}` : row.id}
            ref={focused(row) ? focusRef : undefined}
            className={`ev-grid ev-row${focused(row) ? ' cite-target' : ''}`}
            role="row"
            data-evidence-id={row.id}
          >
            <span role="cell" className="ev-step">{row.step}</span>
            <div role="cell" className="ev-obs">
              <div className="ev-clamp" title={row.observation}>{row.observation}</div>
              {(row.tag || row.bearsOn) && (
                <div className="ev-sub ev-line" title={[row.tag, row.bearsOn].filter(Boolean).join(' · ')}>
                  {row.tag && <span className="ev-run">{row.tag}</span>}
                  {row.tag && row.bearsOn && ' · '}
                  {row.bearsOn}
                </div>
              )}
            </div>
            <div role="cell" className="ev-src">
              <div className="ev-line" title={row.source}>{row.source}</div>
              <div className="ev-sub ev-line">{row.tier}</div>
            </div>
            <span role="cell" className={`ev-stance ${row.stance ?? ''}`}>{row.stance ? STANCE_WORD[row.stance] : DASH}</span>
            <span role="cell" className="ev-by ev-line" title={row.by}>{row.by}</span>
            <span role="cell">
              <button type="button" className="ev-act" disabled aria-label="Actions for this row" title={LATER_RELEASE}>
                ···
              </button>
            </span>
          </div>
        ))}
      </div>
      {children}
    </div>
  )
}

/** Scrolls the cited row into view, and re-does it when `read` changes: the run, or the runs, read again. */
function useFocusRow(focusId: string | null, read: unknown) {
  const focusRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const node = focusRef.current
    if (node && typeof node.scrollIntoView === 'function') node.scrollIntoView({ block: 'nearest' })
  }, [focusId, read])
  return focusRef
}

/** A number per fold read, so a list of runs' reads is one string that changes whenever any run is read again. */
const readIds = new WeakMap<RunFold, number>()
let lastReadId = 0
function readsKey(runs: TrailRun[]): string {
  return runs
    .map((run) => {
      if (!run.fold) return `${run.id}:-`
      if (!readIds.has(run.fold)) readIds.set(run.fold, ++lastReadId)
      return `${run.id}:${readIds.get(run.fold)}`
    })
    .join(' ')
}

/** Holds the row count asked for until that ask's read settles, rows or not; a later ask supersedes it. */
function useAsk(setAsked: (shown: number | null) => void) {
  const latest = useRef(0)
  return (shown: number, answered: Promise<unknown>) => {
    const mine = ++latest.current
    setAsked(shown)
    void answered.finally(() => {
      if (latest.current === mine) setAsked(null)
    })
  }
}

export function EvidenceTrail({ fold, phase, focusId, onShowOlder }: {
  fold: RunFold | null
  phase: Phase
  focusId: string | null
  /** Settles when the read it asked for answers, with rows or without. */
  onShowOlder?: (shown: number) => Promise<void>
}) {
  // The row count asked for, until the read answers it, whether it brings rows or fails.
  const [asked, setAsked] = useState<number | null>(null)
  const ask = useAsk(setAsked)
  const focusRef = useFocusRow(focusId, fold)

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
      <TrailTable rows={rows} focusId={focusId} focusRef={focusRef}>
        {fold.kind === 'hunt' && fold.evidence.length < fold.evidenceCount && onShowOlder && (
          <button
            type="button"
            className="btn neutral"
            disabled={asked === fold.evidence.length}
            onClick={() => ask(fold.evidence.length, onShowOlder(fold.evidence.length))}
          >
            {asked === fold.evidence.length ? 'Loading older rows…' : 'Show older'}
          </button>
        )}
      </TrailTable>
    </section>
  )
}

/** One run's place in the merged table. */
export interface TrailRun {
  id: string
  tag: string
  fold: RunFold | null
  phase: Phase
}

/**
 * Every selected run's evidence as one table, newest first. Paged over the merged list; "Show older"
 * shows the next page, and once the merged list is shown, asks each hunt that holds more for more.
 * Rows already shown stay while a run is re-read.
 */
export function MergedEvidence({ runs, focusId, focusRunId, onShowOlder }: {
  runs: TrailRun[]
  focusId: string | null
  /** Only this run's rows take the citation focus. */
  focusRunId: string | null
  /** Settles when that run's read answers, with rows or without. */
  onShowOlder: (runId: string, shown: number) => Promise<void>
}) {
  const [limit, setLimit] = useState(EVIDENCE_PAGE)
  const [asked, setAsked] = useState<number | null>(null)
  const ask = useAsk(setAsked)
  const ready = runs.filter((run) => run.phase === 'ready' && run.fold)
  const held = ready.reduce((n, run) => n + (run.fold && run.fold.kind === 'hunt' ? run.fold.evidence.length : 0), 0)
  const reads = readsKey(ready)
  const focusRef = useFocusRow(focusId, reads)

  const all = mergeRows(ready.map((run) => ({ id: run.id, tag: run.tag, rows: run.fold ? rowsOf(run.fold) : [] })))
  const rows = all.slice(0, limit)
  const count = ready.reduce((n, run) => n + (run.fold ? foldRecord(run.fold)?.evidenceCount ?? (run.fold.kind === 'lead' ? run.fold.findings.length : 0) : 0), 0)
  const more = ready.flatMap((run) => (run.fold?.kind === 'hunt' && run.fold.evidence.length < run.fold.evidenceCount ? [{ id: run.id, shown: run.fold.evidence.length }] : []))
  const waiting = runs.filter((run) => run.phase !== 'ready')

  return (
    <>
      {waiting.map((run) => <RunNote key={run.id} tag={run.tag} phase={run.phase} />)}
      {rows.length === 0 ? (
        waiting.length === 0 && <EmptyState compact icon="shield" title="No evidence yet" />
      ) : (
        <section className="ev-trail" aria-label="Evidence trail">
          <div>
            <div className="ev-title">
              <h3>{`Evidence trail · ${count} ${count === 1 ? 'row' : 'rows'}${rows.length < count ? ` · showing ${rows.length} of ${count}` : ''}`}</h3>
            </div>
            <p className="muted">Each observation, its source and tier, and whether it supports or goes against an explanation. Newest first, across the runs on this case.</p>
          </div>
          <TrailTable rows={rows} focusId={focusId} focusRunId={focusRunId} focusRef={focusRef}>
            {(all.length > limit || more.length > 0) && (
              <button
                type="button"
                className="btn neutral"
                disabled={all.length <= limit && asked === held}
                onClick={() => {
                  setLimit(limit + EVIDENCE_PAGE)
                  if (all.length > limit) return
                  ask(held, Promise.allSettled(more.map((one) => onShowOlder(one.id, one.shown))))
                }}
              >
                {all.length <= limit && asked === held ? 'Loading older rows…' : 'Show older'}
              </button>
            )}
          </TrailTable>
        </section>
      )}
    </>
  )
}
