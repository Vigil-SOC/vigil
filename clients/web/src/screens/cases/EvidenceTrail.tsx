import { useEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import { InfoTip } from '../../shared/InfoTip'
import { EmptyState } from '../../shared/ui'
import { LATER_RELEASE } from '../../shared/later'
import { EVIDENCE_PAGE, STANCE_WORD, TIER_WORD, evidenceSource, foldRecord, huntHypothesis, relationWord, stanceOn, stanceTotals, workerAt, type RunFold, type RunRecord, type Stance } from './caseFold'
import { RunNotes, RunTag } from './RunTag'
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
  /** When it was captured, ms since the epoch; null when the view carries no usable time. */
  at: number | null
  /** Rows shown as this one: the same critic argument filed again by a later pass. */
  also: string[]
}

const timeOf = (iso: string): number | null => {
  const at = Date.parse(iso)
  return Number.isNaN(at) ? null : at
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
        at: timeOf(row.captured_at),
        also: [],
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
      at: null,
      also: [],
    }))
  }
  const against = huntHypothesis(fold)?.hypothesis_id
  // Each validate pass files the critic's argument again; the same argument on the same
  // explanation is shown once, the newest, and answers for the others (#2564).
  const argued = new Map<string, TrailRow>()
  return fold.evidence.flatMap((row): TrailRow[] => {
    const key = row.provenance === 'null_check' ? `${row.summary}|${row.bears_on.map((link) => `${link.hypothesis_id}:${link.relation}`).join(',')}` : null
    const same = key === null ? undefined : argued.get(key)
    if (same) {
      same.also.push(row.evidence_id)
      return []
    }
    const shown: TrailRow = {
      id: row.evidence_id,
      step: String(row.iteration),
      observation: row.is_gap ? `${row.summary} (gap)` : row.summary || DASH,
      bearsOn: bearsOnLine(row, fold),
      links: row.bears_on.map((link) => link.relation),
      source: evidenceSource(row) || DASH,
      tier: (row.source_tier && TIER_WORD[row.source_tier]) || DASH,
      stance: stanceOn(row, against),
      by: workerAt(fold, row.iteration) ?? DASH,
      at: timeOf(row.captured_at),
      also: [],
    }
    if (key !== null) argued.set(key, shown)
    return [shown]
  })
}

/** The row count the record holds for a run, listed or not. */
function totalOf(fold: RunFold): number {
  return foldRecord(fold)?.evidenceCount ?? (fold.kind === 'lead' ? fold.findings.length : 0)
}

function header(fold: RunFold, listed = fold.kind === 'hunt' ? fold.evidence.length : 0): string {
  const count = totalOf(fold)
  const head = `Evidence trail · ${count} ${count === 1 ? 'row' : 'rows'}`
  if (fold.kind !== 'hunt') return head
  const shown = listed < count ? ` · showing ${listed} of ${count}` : ''
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

type TableRow = TrailRow & { key: string; tag?: string; aliases: string[] }

/** The row a citation lands on: its own, or the one a repeat of it is shown as. */
const isFocus = (row: TableRow, focusKey: string | null) => focusKey !== null && (row.key === focusKey || row.aliases.includes(focusKey))

/** Records a row stands for: itself and the repeats it is shown as. */
const listedOf = (rows: readonly TrailRow[]) => rows.reduce((n, row) => n + 1 + row.also.length, 0)

/** The card and table both shapes of the trail draw; `footer` is what sits under the rows. */
function TrailCard({ title, note, rows, focusKey, focusRef, footer }: {
  title: string
  note: string | null
  rows: TableRow[]
  focusKey: string | null
  focusRef: RefObject<HTMLDivElement>
  footer: ReactNode
}) {
  return (
    <section className="ev-trail" aria-label="Evidence trail">
      <div>
        <div className="ev-title">
          <h3>{title}</h3>
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
              key={row.key}
              ref={isFocus(row, focusKey) ? focusRef : undefined}
              className={`ev-grid ev-row${isFocus(row, focusKey) ? ' cite-target' : ''}`}
              role="row"
              data-evidence-id={row.id}
            >
              <span role="cell" className="ev-step">{row.step}</span>
              <div role="cell" className="ev-obs">
                <div className="ev-clamp" title={row.observation}>{row.observation}</div>
                {row.bearsOn && <div className="ev-sub ev-line" title={row.bearsOn}>{row.bearsOn}</div>}
                {row.tag && <div className="ev-sub ev-line"><RunTag label={row.tag} /></div>}
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
        {footer}
      </div>
    </section>
  )
}

export function EvidenceTrail({ fold, phase, focusId, onShowOlder }: { fold: RunFold | null; phase: Phase; focusId: string | null; onShowOlder?: (shown: number) => Promise<void> }) {
  // The row count asked for, until a read answers it, with rows or without.
  const [asked, setAsked] = useState<number | null>(null)
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

  return (
    <TrailCard
      title={header(fold)}
      note={totalsNote(fold)}
      rows={rows.map((row) => ({ ...row, key: row.id, aliases: row.also }))}
      focusKey={focusId}
      focusRef={focusRef}
      footer={
        fold.kind === 'hunt' && fold.evidence.length < fold.evidenceCount && onShowOlder && (
          <button
            type="button"
            className="btn neutral"
            disabled={asked === fold.evidence.length}
            onClick={() => {
              const shown = fold.evidence.length
              setAsked(shown)
              void onShowOlder(shown).finally(() => setAsked((held) => (held === shown ? null : held)))
            }}
          >
            {asked === fold.evidence.length ? 'Loading older rows…' : 'Show older'}
          </button>
        )
      }
    />
  )
}

export interface TrailRun {
  id: string
  label: string
  fold: RunFold | null
  phase: Phase
}

/** Newest first by capture time; rows with no usable time follow, in the order given (the sort is stable). */
function newestFirst<T extends { at: number | null }>(rows: T[]): T[] {
  return [...rows].sort((a, b) => (a.at === null ? (b.at === null ? 0 : 1) : b.at === null ? -1 : b.at - a.at))
}

/**
 * One trail across several runs, each row tagged with its run and the list paged as one. A hunt lists
 * only its newest rows, so "Show older" asks the hunts that hold more for a larger page and keeps what is shown.
 */
export function MergedEvidenceTrail({ runs, primaryId, focusId, onShowOlder }: {
  runs: TrailRun[]
  primaryId: string | null
  focusId: string | null
  /** Settles when the read it asked for answers, with rows or without. */
  onShowOlder: (runId: string, shown: number) => Promise<void>
}) {
  const [visible, setVisible] = useState(EVIDENCE_PAGE)
  // The folds on screen when older rows were asked for; the ask stands until a read replaces one of them, or every read asked for answers.
  const [asked, setAsked] = useState<(RunFold | null)[] | null>(null)
  const focusRef = useRef<HTMLDivElement>(null)
  const folds = runs.map((run) => run.fold)
  const read = runs.filter((run) => run.phase === 'ready')
  const found = newestFirst(read.flatMap((run) => (run.fold ? rowsOf(run.fold) : []).map((row) => ({ ...row, key: `${run.id}:${row.id}`, tag: run.label, aliases: row.also.map((id) => `${run.id}:${id}`) }))))
  const unread = read.flatMap((run) => (run.fold?.kind === 'hunt' && run.fold.evidence.length < run.fold.evidenceCount ? [{ id: run.id, listed: run.fold.evidence.length, since: Math.min(...rowsOf(run.fold).flatMap((row) => row.at ?? [])) }] : []))
  // A hunt lists only its newest rows, so what it holds past its oldest listed row is newer than anything older another run shows: list nothing past it until it is read.
  const floor = Math.max(-Infinity, ...unread.map((run) => run.since).filter(Number.isFinite))
  const focusKey = focusId && primaryId ? `${primaryId}:${focusId}` : null
  const all = found.filter((row) => floor === -Infinity || isFocus(row, focusKey) || (row.at !== null && row.at >= floor))
  // a cited row is always listed, however old
  const shownCount = Math.max(visible, focusKey ? all.findIndex((row) => isFocus(row, focusKey)) + 1 : 0)
  const rows = all.slice(0, shownCount)
  useEffect(() => {
    const node = focusRef.current
    if (node && typeof node.scrollIntoView === 'function') node.scrollIntoView({ block: 'nearest' })
  }, [focusKey, all.length])

  const waiting = asked !== null && asked.length === folds.length && asked.every((fold, i) => fold === folds[i])
  const total = read.reduce((n, run) => n + (run.fold ? totalOf(run.fold) : 0), 0)
  // One hunt on its own keeps its stance totals and the note that explains them; they are not added across runs.
  const solo = runs.length === 1 && read[0]?.fold?.kind === 'hunt' ? read[0].fold : null
  const listed = listedOf(rows)
  const title = solo ? header(solo, listed) : `Evidence trail · ${total} ${total === 1 ? 'row' : 'rows'}${listed < total ? ` · showing ${listed} of ${total}` : ''}`

  return (
    <>
      {rows.length > 0 ? (
        <TrailCard
          title={title}
          note={solo ? totalsNote(solo) : null}
          rows={rows}
          focusKey={focusKey}
          focusRef={focusRef}
          footer={
            (rows.length < all.length || unread.length > 0) && (
              <button
                type="button"
                className="btn neutral"
                disabled={waiting}
                onClick={() => {
                  setVisible(shownCount + EVIDENCE_PAGE)
                  if (shownCount + EVIDENCE_PAGE <= all.length) return
                  setAsked(folds)
                  void Promise.allSettled(unread.map((run) => onShowOlder(run.id, run.listed))).then(() => setAsked((held) => (held === folds ? null : held)))
                }}
              >
                {waiting ? 'Loading older rows…' : 'Show older'}
              </button>
            )
          }
        />
      ) : (
        read.length === runs.length && <EmptyState compact icon="shield" title="No evidence yet" />
      )}
      <RunNotes runs={runs} />
    </>
  )
}
