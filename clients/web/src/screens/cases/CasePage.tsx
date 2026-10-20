import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { utcClock, utcDay, utcDayClock } from '../../shared/utc'
import { Link } from 'react-router-dom'
import { approvalsApi, casesApi, orchestratorApi, workflowApi, type CaseRecordRow, type NeedsYouItem } from '../../services/api'
import { FilterChip } from '../../shared/FilterChip'
import { InfoTip } from '../../shared/InfoTip'
import { IN_FLIGHT, extendRun, useRunDetail, type Grant } from '../workflows/runRead'
import { KeepGoing, provenanceTag } from '../workflows/huntParts'
import { LevelBadge, slaLevel, type Level } from '../../shared/LevelBadge'
import { NotMeasured } from '../../shared/NotMeasured'
import { LATER_RELEASE } from '../../shared/later'
import { SeverityMark } from '../../shared/SeverityMark'
import { StatePill, statePill } from '../../shared/StatePill'
import { TabStrip } from '../../shared/TabStrip'
import { HoldButton } from '../../shared/HoldButton'
import { Icon } from '../../shared/icons'
import { useToast } from '../../shell/toast'
import { useFusedDecision } from '../../shell/useFusedDecision'
import { EmptyState } from '../../shared/ui'
import type { CaseRow } from '../../data/data'
import Chat from '../../shell/Chat'
import { EvidenceTrail, MergedEvidenceTrail } from './EvidenceTrail'
import { RunNotes, RunTag } from './RunTag'
import { BlindSpots, CheckedList, blindSpotsOf } from './RunChecks'
import { EvidenceCard, IOCsCard } from './CaseSections'
import { CaseSide } from './CaseSide'
import { caseCitations, evidenceInNote } from './caseCitations'
import type { Citation } from '../../shell/citations'
import { fmtCost } from '../../shared/cost'
import { runLabel, timeLeft, when } from './caseFormat'
import {
  addedBy,
  agentRows,
  caseVerdict,
  closureFlag,
  chainOldestFirst,
  endedRun,
  EVIDENCE_PAGE,
  explanationWord,
  foldRecord,
  heldLine,
  honestLine,
  moveTool,
  plainLine,
  plural,
  readFold,
  recallEntityCalls,
  recordChip,
  runSentence,
  stanceTotals,
  stoppedRun,
  strongestRows,
  visibilityGaps,
  wordDisplay,
  type CaseVerdict,
  type EndedRun,
  type HypothesisRow,
  type ChainStep,
  type OpenCheckpointRow,
  type OpenQuestionRow,
  type RecallProvenance,
  type RecordChip,
  type RunFold,
  type StoppedRun,
} from './caseFold'
import './cases.css'
import { CLOSURE_CATEGORIES, type CaseClosureView, type CaseInvestigationRef, type CaseLinkedFinding, type Phase } from './useCases'

const TABS = ['Summary', 'Explanations', 'Evidence', 'Checked', 'Memory and blind spots', 'Record'] as const
type Tab = (typeof TABS)[number]
const NEEDS_POLL_MS = 20_000

const RECORD_CHIPS = ['agent', 'human', 'memory', 'system'] as const
/** The tabs the run filter narrows. */
const RUN_TABS: readonly Tab[] = ['Explanations', 'Evidence', 'Checked', 'Memory and blind spots']

/** Pill tone per explanationWord(); the three non-verdict words stay neutral. */
const EXPL_TONE: Record<string, string> = {
  proven: 'good',
  standing: 'good',
  forming: 'ac',
  weakened: 'poor',
  'ruled out': 'muted',
}

const ROOT_CAUSE_WORKFLOW = 'root-cause-analysis'
// What the Summary's disagreement button asks the composer, on a case with several runs.
const DISAGREE_ASK = "Where do this case's runs disagree, and what did each one say?"

function Mark({ text }: { text: string }) {
  return (
    <span className="case-mark" title={text} aria-label={text}>
      <Icon name="info" size={14} />
    </span>
  )
}

/** "Closed <time> by <who>"; a missing or unparseable time is dropped. */
function closedBy(closure: CaseClosureView | null): string {
  if (!closure) return ''
  const at = utcDayClock(closure.closed_at)
  const time = at ? ` ${at}` : ''
  const who = closure.closed_by ? ` by ${closure.closed_by}` : ''
  return time || who ? `Closed${time}${who}` : ''
}

/** The whole Closed line, shared by the header reason and the Summary: time, closer, category, closer kind. */
function closedLine(closure: CaseClosureView | null): string {
  return [closedBy(closure), CLOSURE_CATEGORIES.find((item) => item.value === closure?.closure_category)?.label ?? closure?.closure_category, closure?.closed_by_kind]
    .filter(Boolean)
    .join(' · ')
}

function clock(value: string | null | undefined): string {
  return utcClock(value) ?? '—'
}

function detailOf(error: unknown, fallback: string): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  return (error as { message?: string })?.message || fallback
}

const decisionFailText = (error: unknown) => detailOf(error, 'Could not update that decision')

/** How long a decision has waited: "4 min", "7 h", "3 d". Empty when the stamp doesn't parse. */
function waiting(since: string): string {
  const ms = Date.now() - new Date(since).getTime()
  if (Number.isNaN(ms)) return ''
  const min = Math.max(0, Math.round(ms / 60_000))
  return min < 60 ? `${min} min` : min < 48 * 60 ? `${Math.round(min / 60)} h` : `${Math.round(min / 1440)} d`
}

/** The rows a note cites, in words while the row is shut and as chips that open them once it is opened (#2459). */
function NoteCites({ ids, open, cites, onCite }: { ids: string[]; open: boolean; cites: ReadonlyMap<string, Citation>; onCite: (cite: Citation) => void }) {
  const known = ids.flatMap((id) => cites.get(id) ?? [])
  const rest = ids.length - known.length
  if (!open || known.length === 0) return <> · {plural(ids.length, 'evidence row')}</>
  return (
    <span className="case-expl-cites">
      {known.map((cite) => (
        <button
          key={cite.id}
          type="button"
          className="cite-chip evidence"
          title={cite.text}
          onClick={(e) => {
            e.stopPropagation()
            onCite(cite)
          }}
        >
          {cite.label}
        </button>
      ))}
      {rest > 0 && <span>and {plural(rest, 'older row')}</span>}
    </span>
  )
}

/** One explanation: the title stops at a line and the justification at two; the full text is on hover and when the row is opened. */
function ExplanationRow({ row, rootCauseRunId, cites, onCite, tag }: { row: HypothesisRow; rootCauseRunId: string | null; cites: ReadonlyMap<string, Citation>; onCite: (cite: Citation) => void; tag?: string }) {
  const [open, setOpen] = useState(false)
  const word = explanationWord(row.status, row.supports, row.weakens)
  const by = addedBy(row.provenance)
  const title = row.statement || row.hypothesis_id
  const note = row.resolution_reason ? evidenceInNote(row.resolution_reason) : null
  return (
    <li>
      <div className={`case-expl-main${open ? ' open' : ''}`} role="button" tabIndex={0} aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        onKeyDown={(e) => { if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); setOpen((v) => !v) } }}>
        <div className="case-expl-line">
          <span className={`case-expl-pill ${EXPL_TONE[word] ?? 'neutral'}`}>{wordDisplay(word)}</span>
          <span className={`case-expl-text${word === 'ruled out' ? ' struck' : ''}`} title={title}>{title}</span>
        </div>
        {tag && <div className="case-expl-run"><RunTag label={tag} /></div>}
        {note && (
          <div className="case-expl-note" title={note.text}>
            {note.text}
            {note.ids.length > 0 && <NoteCites ids={note.ids} open={open} cites={cites} onCite={onCite} />}
          </div>
        )}
      </div>
      <div className="case-expl-side">
        <div>
          <span className="for">{row.supports} for</span> · <span className="against">{row.weakens} against</span>
        </div>
        {by && <div className="case-expl-by">Added by {by}</div>}
        {row.status === 'handed_off' && rootCauseRunId && (
          <div className="case-expl-by">
            <Link className="text-accent-2 hover:underline" aria-label={`Open run ${rootCauseRunId}`} to={`/workflows?run=${encodeURIComponent(rootCauseRunId)}`}>
              Root-cause run
            </Link>
          </div>
        )}
      </div>
    </li>
  )
}

/** A paused run's question, or a hunt stopped at a limit: each gets the full block. */
const blocks = (it: NeedsYouItem) => it.kind === 'checkpoint' || it.kind === 'parked'

/** Paused runs first, then proposed approvals, each kind in the order served. */
function byWeight(items: NeedsYouItem[]): NeedsYouItem[] {
  return [...items.filter(blocks), ...items.filter((it) => !blocks(it))]
}

/** What's waiting, in a line: a paused run's ask, else the one proposal, else how many there are. */
function needsAsk(items: NeedsYouItem[]): string | undefined {
  const paused = items.find(blocks)
  if (paused || items.length < 2) return (paused ?? items[0])?.title
  return `${items.length} proposed actions`
}

/** Stands in for a run's checkpoint until the approvals queue serves it; answered through the queue. */
const FROM_RUN = 'run-checkpoint:'

/** What the server says under each kind of question, for the moment before it has said it. */
const CHECKPOINT_REASON: Record<string, string> = {
  hypothesis_approval: 'Vigil wants your go-ahead on the explanations it will test.',
}

/** The run's own checkpoint as a decision, before the queue has caught up with it. */
function fromRun(cp: OpenCheckpointRow, caseId: string, runId: string | null): NeedsYouItem {
  return {
    kind: 'checkpoint',
    source_id: `${FROM_RUN}${cp.checkpoint_id}`,
    title: cp.question,
    reason: CHECKPOINT_REASON[cp.checkpoint_class] ?? '',
    created_at: cp.raised_at,
    reversibility: 'reversible',
    case_id: caseId,
    run_id: runId,
    checkpoint_id: cp.checkpoint_id,
    checkpoint_class: cp.checkpoint_class,
    hypotheses: cp.hypotheses,
  }
}

/** One explanation a hypothesis approval would start testing, and what it is to the hunt. */
interface ApprovedRow { statement: string; benign: boolean; tag: string | null }

/** Each explanation in full, the benign one last and labelled as what the others are tested against. */
function approvedRows(item: NeedsYouItem, board: HypothesisRow[]): ApprovedRow[] {
  const provenanceOf = new Map(board.map((h) => [h.hypothesis_id, h.provenance]))
  const listed = (item.hypotheses ?? []).map((h) => {
    const provenance = h.provenance || provenanceOf.get(h.hypothesis_id) || ''
    return { statement: h.statement, benign: provenance === 'base_rate', tag: provenanceTag(provenance)?.text ?? null }
  })
  return [...listed.filter((row) => !row.benign), ...listed.filter((row) => row.benign)]
}

/** Under the tabs on every tab but Summary while a decision waits; the decision itself is on Summary. */
function NeedsStrip({ ask, onDecide }: { ask?: string; onDecide: () => void }) {
  return (
    <div className="case-needs-strip">
      <span className="strip-dot" aria-hidden="true" />
      <b>Needs you</b>
      {ask && <span className="strip-ask">{ask}</span>}
      <button type="button" onClick={onDecide}>Decide on Summary</button>
    </div>
  )
}

/** Compact rows shown before "+N more proposed actions". */
const ROWS_SHOWN = 3

function CaseNeeds({
  items,
  busy,
  error,
  board,
  onApprove,
  onReject,
}: {
  items: NeedsYouItem[]
  busy: boolean
  error: string | null
  /** The run's explanations, to say which one an approval tests against when the queue does not. */
  board: HypothesisRow[]
  onApprove: (item: NeedsYouItem) => void
  onReject: (item: NeedsYouItem, reason: string) => void
}) {
  const [showAll, setShowAll] = useState(false)
  if (items.length === 0) return null
  // Every paused run gets the full block; with none, the first proposal does. Other proposals wait without blocking, so they collapse.
  const ordered = byWeight(items)
  const full = Math.max(1, ordered.filter(blocks).length)
  const rows = ordered.slice(full)
  const visible = showAll ? rows : rows.slice(0, ROWS_SHOWN)
  return (
    <section className="case-needs" aria-label="Needs you">
      {error && <p role="alert">{error}</p>}
      {ordered.slice(0, full).map((item) => (
        <CaseNeed key={item.source_id} item={item} busy={busy} board={board} onApprove={onApprove} onReject={onReject} />
      ))}
      {rows.length > 0 && (
        <div className="needs-more">
          <h3>{ordered[0].kind === 'approval' ? 'More proposed actions' : 'Proposed actions'} · {rows.length}</h3>
          <ul aria-label="Proposed actions">
            {visible.map((item) => (
              <CaseNeedRow key={item.source_id} item={item} busy={busy} onApprove={onApprove} onReject={onReject} />
            ))}
          </ul>
          {rows.length > visible.length && (
            <div className="needs-more-foot">
              <span>+{rows.length - visible.length} more proposed actions</span>
              <button type="button" className="btn neutral" onClick={() => setShowAll(true)}>Show all</button>
            </div>
          )}
        </div>
      )}
    </section>
  )
}

type NeedProps = {
  item: NeedsYouItem
  busy: boolean
  board?: HypothesisRow[]
  onApprove: (item: NeedsYouItem) => void
  onReject: (item: NeedsYouItem, reason: string) => void
}

function ApproveButton({ item, busy, onApprove, label }: NeedProps & { label?: string }) {
  return item.reversibility === 'reversible' ? (
    <button type="button" className="btn primary" aria-label={label} disabled={busy} onClick={() => onApprove(item)}>
      Approve
    </button>
  ) : (
    <HoldButton label="Hold to approve" name={label && `Hold to approve ${item.title}`} disabled={busy} onConfirm={() => onApprove(item)} />
  )
}

function RejectForm({ item, busy, onReject }: NeedProps) {
  const [reason, setReason] = useState('')
  return (
    <form
      className="needs-reject"
      onSubmit={(event) => {
        event.preventDefault()
        const text = reason.trim()
        if (!text) return
        onReject(item, text)
      }}
    >
      <label htmlFor={`reject-${item.source_id}`}>Why? The reason goes back to the agents.</label>
      <textarea
        id={`reject-${item.source_id}`}
        className="feedback-box"
        aria-label="Rejection reason"
        value={reason}
        onChange={(event) => setReason(event.target.value)}
        placeholder="Why is this being rejected?"
        autoFocus
      />
      <button type="submit" className="btn neutral" disabled={busy || !reason.trim()}>
        Confirm reject
      </button>
    </form>
  )
}

function CaseNeed(props: NeedProps) {
  const { item, busy, board = [] } = props
  const [rejecting, setRejecting] = useState(false)
  const waited = waiting(item.created_at)
  const approving = approvedRows(item, board)

  if (item.kind === 'parked') return <ParkedNeed item={item} waited={waited} />
  return (
    <article>
      <span className="needs-head">
        <span className="needs-dot" aria-hidden="true" />
        Needs your decision{waited && ` · waiting ${waited}`}
      </span>
      <h3>{item.title}</h3>
      {approving.length > 0 && (
        <ul className="needs-approving" aria-label="What you are approving">
          {approving.map((row, i) => (
            <li key={i} className={row.benign ? 'benign' : undefined}>
              <span className="needs-approving-tag">{row.benign ? 'The benign explanation it is tested against' : row.tag === 'yours' ? 'Yours, to test' : 'To test'}</span>
              <span className="needs-approving-text">{row.statement}</span>
            </li>
          ))}
        </ul>
      )}
      <dl className="needs-facts">
        {item.reason && (
          <>
            <dt>Why it stopped</dt>
            <dd>{item.reason}</dd>
          </>
        )}
        <dt>Reversibility</dt>
        <dd>{item.reversibility === 'reversible' ? 'Reversible' : 'Cannot be undone'}</dd>
      </dl>
      <div className="needs-actions">
        <ApproveButton {...props} />
        <button
          type="button"
          className="btn neutral"
          aria-expanded={rejecting}
          disabled={busy}
          onClick={() => setRejecting((open) => !open)}
        >
          Reject
        </button>
      </div>
      {rejecting && <RejectForm {...props} />}
    </article>
  )
}

/** Keep going on a hunt stopped at a limit: the run page's control, sending the run page's directive. */
function KeepGoingRow({ runId }: { runId: string }) {
  const [busy, setBusy] = useState(false)
  const [said, setSaid] = useState<{ ok: boolean; text: string } | null>(null)
  const grant = (amount: Grant, label: string) => {
    setBusy(true)
    setSaid(null)
    extendRun(runId, '', amount)
      .then(() => setSaid({ ok: true, text: `Keep going sent: ${label}. The hunt carries on from where it stopped at its next turn.` }))
      .catch((e) => setSaid({ ok: false, text: detailOf(e, 'Could not send Keep going') }))
      .finally(() => setBusy(false))
  }
  if (said?.ok) return <p className="needs-sent" role="status">{said.text}</p>
  return (
    <div className="needs-actions needs-keep-going">
      <KeepGoing busy={busy} className="btn neutral" onGrant={grant} />
      {said && <span role="alert" className="needs-error">{said.text}</span>}
    </div>
  )
}

/** A hunt stopped at its turn, time or budget limit: it keeps what it found and waits for Keep going. */
function ParkedNeed({ item, waited }: { item: NeedsYouItem; waited: string }) {
  return (
    <article>
      <span className="needs-head">
        <span className="needs-dot" aria-hidden="true" />
        Needs your decision{waited && ` · waiting ${waited}`}
      </span>
      <h3>{item.title}</h3>
      <dl className="needs-facts">
        <dt>Why it stopped</dt>
        <dd>{item.reason || 'It reached one of its own limits.'}</dd>
        <dt>If you do nothing</dt>
        <dd>It keeps everything it found and waits here.</dd>
      </dl>
      <KeepGoingRow runId={item.run_id || item.source_id} />
    </article>
  )
}

/** One proposed action as a row: title, reversibility, its own Approve and Reject. */
function CaseNeedRow(props: NeedProps) {
  const { item, busy } = props
  const [rejecting, setRejecting] = useState(false)
  const reversible = item.reversibility === 'reversible'
  return (
    <li>
      <div className="needs-row">
        <span className="needs-row-title">{item.title}</span>
        <span className={reversible ? 'needs-row-rev' : 'needs-row-rev irreversible'}>
          {reversible ? 'Reversible' : 'Cannot be undone'}
        </span>
        <div className="needs-actions">
          <ApproveButton {...props} label={`Approve ${item.title}`} />
          <button
            type="button"
            className="btn neutral"
            aria-label={`Reject ${item.title}`}
            aria-expanded={rejecting}
            disabled={busy}
            onClick={() => setRejecting((open) => !open)}
          >
            Reject
          </button>
        </div>
      </div>
      {rejecting && <RejectForm {...props} />}
    </li>
  )
}

/** Rows the Memory tab's two cards show. */
function memoryRows(fold: RunFold | null): number {
  const recall = fold?.recall
  const opening = recall && !recall.unavailable ? recall.sightings.length + recall.verdicts.length : 0
  return opening + recallEntityCalls(fold).length + (recall?.gaps.length ?? 0) + visibilityGaps(fold).length + leftOpen(fold).length
}

/** An investigation's questions still open on its record: blind spots it declared. A hunt's open questions are leads, shown where it stopped on one. */
function leftOpen(fold: RunFold | null): OpenQuestionRow[] {
  return fold?.kind === 'lead' ? fold.record?.openQuestions ?? [] : []
}

function counts(fold: RunFold | null, record: number): Record<Tab, number> {
  const calls = fold?.calls.length ?? 0
  const memory = memoryRows(fold)
  const kept = foldRecord(fold)
  if (kept) {
    return {
      Summary: kept.hypotheses.length,
      Explanations: kept.hypotheses.length,
      Evidence: kept.evidenceCount,
      Checked: calls,
      'Memory and blind spots': memory,
      Record: record,
    }
  }
  const findings = fold?.kind === 'lead' ? fold.findings.length : 0
  return {
    Summary: findings,
    Explanations: 0,
    Evidence: findings,
    Checked: calls,
    'Memory and blind spots': memory,
    Record: record,
  }
}

/** The grouped tabs count every run's rows, so a door's number is what its tab lists; Summary stays the primary run's. */
function caseCounts(primary: RunFold | null, folds: (RunFold | null)[], record: number): Record<Tab, number> {
  const each = folds.map((fold) => counts(fold, record))
  const sum = (tab: Tab) => each.reduce((n, one) => n + one[tab], 0)
  return {
    ...counts(primary, record),
    Explanations: sum('Explanations'),
    Evidence: sum('Evidence'),
    Checked: sum('Checked'),
    'Memory and blind spots': sum('Memory and blind spots'),
  }
}

const DOOR_ORDER = ['proven', 'standing', 'weakened', 'forming', 'ruled out']
/** Where a word sits in DOOR_ORDER; a word outside it comes last. */
const doorRank = (word: string) => (DOOR_ORDER.includes(word) ? DOOR_ORDER.indexOf(word) : DOOR_ORDER.length)

/** The line under each door's count, from the folds already loaded: the primary run's alone on a one-run case, every run's otherwise. */
function doorLines(folds: (RunFold | null)[], foldPhase: Phase, hasRun: boolean, rows: CaseRecordRow[], recordPhase: Phase): Record<Exclude<Tab, 'Summary'>, string> {
  const none = foldPhase === 'loading' ? 'Loading…' : foldPhase === 'error' ? 'Couldn’t read the run' : hasRun ? 'Run started' : 'No run yet'
  const read = folds.filter((one): one is RunFold => one !== null)
  const fold = folds.length === 1 ? folds[0] : null
  const tally = new Map<string, number>()
  const records = read.flatMap((one) => foldRecord(one) ?? [])
  const record = records.length ? records[0] : null
  for (const kept of records) {
    for (const row of kept.hypotheses) {
      const word = explanationWord(row.status, row.supports, row.weakens)
      tally.set(word, (tally.get(word) ?? 0) + 1)
    }
  }
  const words = [...tally.keys()].sort((a, b) => doorRank(a) - doorRank(b))
  let evidence = none
  if (folds.length > 1 && read.length) {
    const total = read.reduce((n, one) => n + counts(one, 0).Evidence, 0)
    evidence = `${plural(total, 'row')} across ${plural(folds.length, 'run')}`
  } else if (fold?.kind === 'hunt') {
    const n = stanceTotals(fold)
    evidence = !fold.evidence.length ? 'None shown' : !n ? plural(fold.evidenceCount, 'row') : `${n.supports} support · ${n.weakens} go against · ${n.neither} neither`
  } else if (record) {
    evidence = record.evidenceCount ? `${plural(record.evidenceCount, 'row')} weighed` : 'None linked yet'
  } else if (fold) {
    evidence = 'Findings, no for or against'
  }
  const recalls = read.flatMap((one) => one.recall ?? [])
  const recallRows = recalls.reduce((n, r) => n + r.sightings.length + r.verdicts.length + r.gaps.length, 0) + read.reduce((n, one) => n + recallEntityCalls(one).length, 0)
  const blind = read.reduce((n, one) => n + visibilityGaps(one).length, 0)
  const open = read.reduce((n, one) => n + leftOpen(one).length, 0)
  const memory = [recallRows && plural(recallRows, 'recall row'), blind && plural(blind, 'gap'), open && plural(open, 'open question')].filter(Boolean).join(' · ')
  const priced = read.flatMap((one) => (one.costUsd == null ? [] : [one.costUsd]))
  const cost = priced.length ? fmtCost(priced.reduce((a, b) => a + b, 0)) : '—'
  const chained = rows.filter((row) => row.chained).length
  return {
    Explanations: record ? (words.length ? words.map((w) => `${tally.get(w)} ${w.replace(/_/g, ' ')}`).join(' · ') : 'None yet') : read.length ? 'Does not test explanations yet' : none,
    Evidence: evidence,
    Checked: read.length ? `${cost} · ${plural(blind, 'gap')}` : none,
    'Memory and blind spots': recalls.length && recalls.every((r) => r.unavailable) ? 'Recall did not happen' : memory || (recalls.length ? 'Recalled, no rows' : 'Nothing recalled'),
    Record: recordPhase === 'loading' ? 'Loading…' : recordPhase === 'error' ? 'Couldn’t load' : `${plural(rows.length, 'row')} · ${chained} chained`,
  }
}

/** One run on the case, as the page has read it. */
interface CaseRun {
  ref: CaseInvestigationRef
  fold: RunFold | null
  phase: Phase
}

/** Reads one run on the case on Watch a run's interval while it is in flight, and reports each read up. Renders nothing. */
function RunReader({ runId, evidenceLimit, onRead, onLoader, onSettled, onReread }: {
  runId: string
  evidenceLimit?: number
  onRead: (runId: string, read: { fold: RunFold | null; phase: Phase } | null) => void
  onLoader: (runId: string, load: (() => Promise<void>) | null) => void
  /** A read at this evidence limit has answered, with rows or without. */
  onSettled: (runId: string, evidenceLimit: number) => void
  /** A re-read of a run that was in flight: the case and record follow it. */
  onReread: () => void
}) {
  const run = useRunDetail(runId, true, undefined, evidenceLimit)
  const { load, setDphase } = run
  useEffect(() => {
    setDphase('loading')
    void load().finally(() => onSettled(runId, evidenceLimit ?? 0))
  }, [load, setDphase, onSettled, runId, evidenceLimit])
  useEffect(() => {
    onLoader(runId, load) // a failed read keeps what is shown
    return () => onLoader(runId, null)
  }, [runId, load, onLoader])
  const detail = run.detail?.run_id === runId ? run.detail : null
  const fold = useMemo(() => readFold(detail), [detail])
  const phase: Phase = detail ? 'ready' : run.dphase === 'error' ? 'error' : 'loading'
  useEffect(() => onRead(runId, { fold, phase }), [runId, fold, phase, onRead])
  useEffect(() => () => onRead(runId, null), [runId, onRead])
  const seen = useRef<string | null>(null)
  useEffect(() => {
    const prev = seen.current
    seen.current = detail?.status ?? null
    if (detail && prev && IN_FLIGHT.includes(prev)) onReread()
  }, [detail]) // eslint-disable-line react-hooks/exhaustive-deps
  return null
}

/** "$0.12 of $1.00": what a run has spent against what it was granted, read off its fold once read; a cost nothing priced is a dash, never $0. */
function spentLine(run: CaseRun): string {
  const cost = run.fold ? run.fold.costUsd : null
  const cap = run.fold?.maxCostUsd || (run.ref.max_cost_usd > 0 ? run.ref.max_cost_usd : null)
  const spent = cost == null ? '—' : fmtCost(cost)
  return cap == null ? spent : `${spent} of ${fmtCost(cap)}`
}

/** The other runs on the case, one head line each, under the primary run's cards. */
function OtherRuns({ runs, names, onFollow }: { runs: CaseRun[]; names: Record<string, string>; onFollow: () => void }) {
  if (runs.length === 0) return null
  return (
    <section className="case-other-runs" aria-label="Other runs on this case">
      <h3>Other runs on this case</h3>
      <ul>
        {runs.map((run) => {
          const word = run.fold?.disposition
          const state = run.phase === 'loading' ? 'Loading…' : run.phase === 'error' ? 'Couldn’t read the run' : wordDisplay(run.fold?.outcome || run.fold?.run.status || run.ref.status || '—')
          return (
            <li key={run.ref.run_id}>
              <Link className="case-other-name" to={`/workflows?run=${encodeURIComponent(run.ref.run_id)}`} title={runLabel(run.ref, names)} onClick={onFollow}>{runLabel(run.ref, names)}</Link>
              <span className="case-other-state">
                {state}
                {word && <> · <b>{wordDisplay(word)}</b></>}
              </span>
              <span className="num">{spentLine(run)}</span>
            </li>
          )
        })}
      </ul>
    </section>
  )
}

/** The newest root-cause run on the case that recorded a chain, with its steps oldest first; else why the card is silent. */
function howStarted(runs: CaseRun[]): { run: CaseRun; chain: ChainStep[] } | 'error' | null {
  const newest = [...runs].sort((a, b) => (b.ref.created_at ?? '').localeCompare(a.ref.created_at ?? ''))
  const ready = newest.find((one) => one.fold?.kind === 'head' && one.fold.steps.length > 0)
  if (ready?.fold?.kind === 'head') return { run: ready, chain: chainOldestFirst(ready.fold.steps) }
  return newest.some((one) => one.ref.workflow_id === ROOT_CAUSE_WORKFLOW && one.phase === 'error') ? 'error' : null
}

/** Where the incident started and how far the trace proved it: the root-cause run's chain, first and last links only. */
function HowStarted({ started, names, onFollow }: { started: NonNullable<ReturnType<typeof howStarted>>; names: Record<string, string>; onFollow: () => void }) {
  if (started === 'error') {
    return (
      <section className="case-card" aria-label="How it started">
        <h3>How it started</h3>
        <p className="how-line">The run could not be read.</p>
      </section>
    )
  }
  const { run, chain } = started
  const origin = chain[0]
  const rows = chain.length > 2 ? [origin, chain[chain.length - 1]] : chain
  const proven = run.fold?.kind === 'head' ? run.fold.proven : 0
  const to = `/workflows?run=${encodeURIComponent(run.ref.run_id)}`
  return (
    <section className="case-card how-started" aria-label="How it started">
      <h3>How it started</h3>
      <p className="how-origin">
        <b>{origin.event || '—'}</b>
        <span>{[origin.who, origin.at && when(origin.at)].filter(Boolean).join(' · ')}</span>
      </p>
      <p className="how-line">{plural(chain.length, 'event')} · {proven} proven</p>
      <ul className="how-rows">
        {rows.map((step, i) => (
          <li key={step.step_id || i}>
            <span className="how-event">{step.event || '—'}</span>
            <span className="how-at">{step.at ? when(step.at) : '—'}</span>
            <b className={step.proven ? 'good' : undefined}>{step.proven ? 'proven' : 'unproven'}</b>
          </li>
        ))}
      </ul>
      <Link className="how-all" to={to} title={runLabel(run.ref, names)} onClick={onFollow}>
        {chain.length > 2 ? `Show all ${chain.length}` : 'Open the trace'}
      </Link>
    </section>
  )
}

/** The one word the runs stand behind, its basis when they part ways, and what to do now. Open moods draw it as a card; the closed Summary draws it inside its own. */
function VerdictBlock({ verdict, card }: { verdict: CaseVerdict; card: boolean }) {
  return (
    <div className={`case-verdict${card ? ' card' : ''}`} {...(card ? { role: 'region', 'aria-label': 'Case verdict' } : {})}>
      <span className="case-verdict-label">Runs say</span>
      <div>
        <b className="case-verdict-word">{wordDisplay(verdict.word)}</b>
        {verdict.basis && <p>{verdict.basis}</p>}
      </div>
      {verdict.doNow.length > 0 && (
        <>
          <span className="case-verdict-label">Do now</span>
          <ul>
            {verdict.doNow.map((step, i) => (
              <li key={i}>{step}</li>
            ))}
          </ul>
        </>
      )}
    </div>
  )
}

/** Five doors into the audit tabs, in every mood. */
function Doors({ counts: n, lines, onOpen }: { counts: Record<Tab, number>; lines: Record<Exclude<Tab, 'Summary'>, string>; onOpen: (tab: Tab) => void }) {
  return (
    <section className="case-doors" aria-label="Audit doors">
      {TABS.filter((name) => name !== 'Summary').map((name) => (
        <button key={name} type="button" className="case-door" onClick={() => onOpen(name)}>
          <span className="door-label">{name}<span aria-hidden="true">›</span></span>
          <span className="door-count">{n[name]}</span>
          <span className="door-sub">{lines[name as Exclude<Tab, 'Summary'>]}</span>
        </button>
      ))}
    </section>
  )
}

/** The way into Watch a run for the run this card describes; the drawer closes on follow, as Replay's does. */
function WatchLink({ runId, onFollow }: { runId: string | null; onFollow: () => void }) {
  if (!runId) return null
  return (
    <Link className="rec-btn watch-link" to={`/workflows?run=${encodeURIComponent(runId)}`} onClick={onFollow}>
      Watch it run
    </Link>
  )
}

/** Now · step N: the latest move, who has it, with which tool, since when. */
function NowCard({ fold, phase, watch }: { fold: RunFold | null; phase: Phase; watch: ReactNode }) {
  const move = fold?.moves[0]
  const meta = fold ? [fold.worker, moveTool(fold, move), `since ${clock(move?.at)}`].filter(Boolean).join(' · ') : ''
  return (
    <section className="case-now" aria-label="Now">
      <div className="now-head">
        <span className="now-dot" aria-hidden="true" />
        <b>{fold ? `Now · step ${Math.max(fold.kind === 'hunt' ? fold.iteration : fold.kind === 'lead' ? fold.iterations : 0, move?.iteration ?? 0)}` : 'Now'}</b>
        {meta && <span className="now-meta">{meta}</span>}
      </div>
      <p className="clamp2" title={phase === 'ready' ? fold?.doing : undefined}>
        {phase === 'loading' && 'Loading the run…'}
        {phase === 'error' && 'The run could not be read.'}
        {phase === 'ready' && (fold ? fold.doing || 'Nothing decided yet' : 'The run has started and has not reported yet.')}
      </p>
      {watch}
    </section>
  )
}

/** A run that is not going on: paused or stopped, with the one-line why and the raw text behind the ⓘ.
 *  A hunt stopped at a limit takes Keep going here too, before the queue has listed it. */
function StoppedCard({ stopped, runId, watch }: { stopped: StoppedRun; runId: string | null; watch: ReactNode }) {
  return (
    <section className="case-stopped" aria-label="Run state">
      <div className="stopped-line">
        <b>{wordDisplay(stopped.state)}</b>
        <span className="clamp2" title={stopped.raw || stopped.line}>{stopped.line}</span>
        {stopped.raw && <InfoTip label="What the run reported" text={stopped.raw} align="start" />}
        {watch}
      </div>
      {stopped.atLimit && runId && <KeepGoingRow runId={runId} />}
    </section>
  )
}

/** A question answered here, while the run has yet to read the answer: said at once, so the page never
 *  flips back to waiting. */
function AnsweredCard({ answer, watch }: { answer: 'approve' | 'reject' | null; watch: ReactNode }) {
  const word = answer === 'approve' ? 'Approved' : answer === 'reject' ? 'Rejected' : 'Answered'
  const next = answer === 'reject' ? 'The run stops at its next turn.' : 'The run carries on at its next turn.'
  return (
    <section className="case-stopped case-answered" aria-label="Run state">
      <div className="stopped-line">
        <b>{word}</b>
        <span className="clamp2">{next}</span>
        {watch}
      </div>
    </section>
  )
}

/** A run that has ended: its verdicts in one statement, the run's own words behind the ⓘ, and the open
 *  question it could not conclude past put to you as a decision rather than buried in prose. */
function OutcomeCard({ ended, watch, onAsk }: { ended: EndedRun; watch: ReactNode; onAsk: (text: string) => void }) {
  return (
    <>
      <section className="case-stopped case-ended" aria-label="Run outcome">
        <div className="stopped-line">
          <b>Ended</b>
          <span className="clamp2" title={ended.line}>{ended.line}</span>
          {ended.raw && <InfoTip label="What the run reported" text={ended.raw} align="start" />}
          {watch}
        </div>
      </section>
      {ended.question && (
        <section className="case-needs" aria-label="Left open">
          <article>
            <span className="needs-head">
              <span className="needs-dot" aria-hidden="true" />
              Left open · the hunt would not conclude without it
            </span>
            <h3>{ended.question.question}</h3>
            <dl className="needs-facts">
              <dt>Why it matters</dt>
              <dd>The hunt stopped with this unanswered, so the verdicts above are where it stopped, not where it would have ended.</dd>
            </dl>
            <div className="needs-actions">
              <button type="button" className="btn primary" onClick={() => onAsk(ended.question!.question)}>Ask about this</button>
            </div>
          </article>
        </section>
      )}
    </>
  )
}

function AgentsTable({ fold, phase }: { fold: RunFold | null; phase: Phase }) {
  // A paused or stopped run is already said by the card above; its rows would be stale.
  if (stoppedRun(fold)) return null
  const inFlight = !!fold && IN_FLIGHT.includes(fold.run.status)
  const rows = inFlight ? agentRows(fold) : []
  return (
    <section className="case-agents" aria-label="Agents on this case">
      <h3>Agents on this case, right now</h3>
      {phase === 'loading' && <p className="muted">Loading the run…</p>}
      {phase === 'error' && <p className="muted">The run could not be read.</p>}
      {phase === 'ready' && rows.length === 0 && <p className="muted">{inFlight ? 'No agent has acted yet.' : 'No live investigation.'}</p>}
      {rows.length > 0 && (
        <div className="agent-rows" role="table" aria-label="Agents">
          {rows.map((row, i) => (
            <div key={row.who} className="agent-row" role="row">
              <span className="agent-who" role="cell" title={row.who}>{row.who}</span>
              <span className="agent-doing" role="cell" title={row.doing}>{row.doing || '—'}</span>
              <span className="agent-tool" role="cell" title={row.tool}>{row.tool || '—'}</span>
              <span className="agent-since" role="cell">{clock(row.at)}</span>
              {/* The agent holding the latest move is working; the others are between moves. */}
              <span className={`agent-state${i === 0 ? ' live' : ''}`} role="cell"><span className="state-dot" aria-hidden="true" />{i === 0 ? 'Working' : 'Idle'}</span>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

/** ⋯ menu in the head row. Same pattern as the console's More menu: closes on outside click and Escape. */
function CaseMenu({ onEdit, onMerge, onDelete }: { onEdit: () => void; onMerge: () => void; onDelete?: () => void }) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      e.preventDefault() // the drawer's own Escape handler skips handled keys
      setOpen(false)
    }
    // capture: the drawer stops mousedown from bubbling to the document
    document.addEventListener('mousedown', onDoc, true)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc, true)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const pick = (fn: () => void) => () => {
    setOpen(false)
    triggerRef.current?.focus() // the dialog returns focus here when it closes
    fn()
  }

  return (
    <div className="vg-more dh-more" ref={ref}>
      <button
        type="button"
        ref={triggerRef}
        className="btn ghost icon"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Case actions"
        title="Case actions"
        onClick={() => setOpen((v) => !v)}
      >
        <Icon name="more" size={15} />
      </button>
      {open && (
        <div className="vg-more-menu dh-menu" role="menu" aria-label="Case actions">
          <button type="button" role="menuitem" onClick={pick(onEdit)}><Icon name="edit" size={14} /> Edit</button>
          <button type="button" role="menuitem" onClick={pick(onMerge)}><Icon name="link" size={14} /> Merge</button>
          {onDelete && (
            <button type="button" role="menuitem" className="danger" onClick={pick(onDelete)}>
              <Icon name="trash" size={14} /> Delete case
            </button>
          )}
        </div>
      )}
    </div>
  )
}

export function CasePage({
  id,
  c,
  created,
  combinedState,
  investigations,
  closure,
  linkedFindings,
  phase,
  error,
  pageKey,
  seed,
  onSeedConsumed,
  onBack,
  onExpand,
  onEdit,
  onMerge,
  onDelete,
  canDelete,
  onChanged,
  onRefresh,
}: {
  id: string
  c: CaseRow | null
  created: string
  combinedState: string
  investigations: CaseInvestigationRef[]
  closure: CaseClosureView | null
  linkedFindings: CaseLinkedFinding[]
  phase: Phase
  error: string | null
  /** Route key stored as page_context. Cases passes `cases`; the drawer passes SocConsole's current. */
  pageKey: string
  /** Text typed in the command bar; handed to the composer, then cleared. */
  seed?: string | null
  onSeedConsumed?: () => void
  /** The "Cases" crumb; in the drawer it also backs the Close icon. */
  onBack: () => void
  /** Set only in the drawer: shows the Expand and Close icons. */
  onExpand?: () => void
  onEdit: () => void
  onMerge: () => void
  onDelete: () => void
  canDelete: boolean
  onChanged: () => void
  /** Re-read the case in place, without blanking it. Used while the run is live. */
  onRefresh?: () => void
}) {
  const [tab, setTab] = useState<Tab>('Summary')
  const [sla, setSla] = useState<{ due: string; health: string } | null>(null)
  const [workflowNames, setWorkflowNames] = useState<Record<string, string>>({})
  const [rows, setRows] = useState<CaseRecordRow[]>([])
  const [recordPhase, setRecordPhase] = useState<Phase>('loading')
  const [recordError, setRecordError] = useState<string | null>(null)
  const [recordKey, setRecordKey] = useState(0)
  const quietRecord = useRef(false) // a poll's re-read keeps the rows and tab as they are
  const recordReady = useRef(false)
  recordReady.current = recordPhase === 'ready'
  const [chip, setChip] = useState<RecordChip | 'all'>('all')
  const [runFilter, setRunFilter] = useState<string | null>(null)
  const [askSeed, setAskSeed] = useState<{ id: string; text: string } | null>(null)
  const [focusEvidence, setFocusEvidence] = useState<string | null>(null)
  const [note, setNote] = useState('') // Reopen's failure, on Summary
  // Verify chain / Export audit outcome, shown in the Record header; level null while in flight
  const [recStatus, setRecStatus] = useState<{ level: Level; text: string } | null>(null)
  const recStatusTicket = useRef(0)
  const [busy, setBusy] = useState(false)
  const [needsItems, setNeedsItems] = useState<NeedsYouItem[]>([])
  const [needsCount, setNeedsCount] = useState(0)
  const [decisionBusy, setDecisionBusy] = useState<string | null>(null)
  const [decisionError, setDecisionError] = useState<string | null>(null)
  const decisionBusyRef = useRef<string | null>(null)
  // Checkpoints the queue has served for this case, and how each one answered here was answered: a
  // checkpoint that has left the queue while the run still holds it open has been answered.
  const [seenCheckpoints, setSeenCheckpoints] = useState<ReadonlySet<string>>(() => new Set())
  const [answers, setAnswers] = useState<Record<string, 'approve' | 'reject'>>({})
  const { pending, settled } = useToast()
  const needsTicket = useRef(0)
  const seenCase = useRef(id)
  if (seenCase.current !== id) {
    seenCase.current = id
    needsTicket.current += 1
    decisionBusyRef.current = null
    setNeedsItems([])
    setNeedsCount(0)
    setDecisionBusy(null)
    setDecisionError(null)
    setSeenCheckpoints(new Set())
    setAnswers({})
  }

  const loadNeeds = useCallback(async () => {
    const ticket = ++needsTicket.current
    const forCase = id
    try {
      const res = await approvalsApi.needsYou(forCase)
      if (ticket !== needsTicket.current || forCase !== seenCase.current) return
      setNeedsItems(res.data.items)
      setNeedsCount(res.data.count)
    } catch {
      if (ticket !== needsTicket.current || forCase !== seenCase.current) return
    }
  }, [id])

  useEffect(() => {
    void loadNeeds()
    const timer = window.setInterval(() => {
      void loadNeeds()
    }, NEEDS_POLL_MS)
    return () => window.clearInterval(timer)
  }, [loadNeeds])

  useEffect(() => {
    const served = needsItems.flatMap((it) => (it.checkpoint_id ? [it.checkpoint_id] : []))
    if (served.every((cp) => seenCheckpoints.has(cp))) return
    setSeenCheckpoints((held) => new Set([...held, ...served]))
  }, [needsItems, seenCheckpoints])

  useEffect(() => {
    setTab('Summary')
    setAskSeed(null)
    setFocusEvidence(null)
    setNote('')
    setChip('all')
    setRunFilter(null)
    setEvidenceLimits({})
  }, [id])

  // leaving the Record tab (or the case) drops its status and ignores a call still in flight
  useEffect(() => {
    recStatusTicket.current += 1
    setRecStatus(null)
  }, [id, tab])

  // the command bar's text rides the composer's own seed path
  useEffect(() => {
    if (!seed) return
    setAskSeed({ id, text: seed })
    onSeedConsumed?.()
  }, [seed, id, onSeedConsumed])

  // The run the page reads, header and record included: a hunt's handoff queues a
  // root-cause run onto the same case, and that follow-up is not the case's run.
  const latest = investigations.find((item) => item.primary) ?? null
  const runId = latest?.run_id ?? null
  const rootCauseRunId = investigations.find((item) => item.workflow_id === ROOT_CAUSE_WORKFLOW)?.run_id ?? null
  // The list maps every status other than open/investigating to closed, so a
  // `new` case must not use that fallback while the detail read is in flight.
  const closed = combinedState === 'closed' || (phase === 'ready' && !combinedState && c?.status === 'closed')
  const pill = combinedState || (phase === 'loading' ? '…' : c?.status || '—')

  // Every run on the case, the primary first, then the rest newest first. Each is read by its own
  // RunReader, re-read on Watch a run's interval while it is in flight, and pages its evidence alone.
  const caseRuns = useMemo(() => {
    const rest = investigations
      .filter((item, i) => item.run_id && investigations.findIndex((one) => one.run_id === item.run_id) === i && item.run_id !== latest?.run_id)
      .sort((a, b) => (b.created_at ?? '').localeCompare(a.created_at ?? ''))
    return latest ? [latest, ...rest] : rest
  }, [investigations, latest])
  const [evidenceLimits, setEvidenceLimits] = useState<Record<string, number>>({})
  const [reads, setReads] = useState<Record<string, { fold: RunFold | null; phase: Phase }>>({})
  const onRead = useCallback((key: string, read: { fold: RunFold | null; phase: Phase } | null) => {
    setReads((held) => {
      if (read === null) {
        if (!(key in held)) return held
        const { [key]: _gone, ...rest } = held // eslint-disable-line @typescript-eslint/no-unused-vars
        return rest
      }
      return held[key]?.fold === read.fold && held[key]?.phase === read.phase ? held : { ...held, [key]: read }
    })
  }, [])
  const loaders = useRef(new Map<string, () => Promise<void>>())
  const onLoader = useCallback((key: string, load: (() => Promise<void>) | null) => {
    if (load) loaders.current.set(key, load)
    else loaders.current.delete(key)
  }, [])
  // "Show older" asks a run for a larger page; the ask settles once a read at that size answers, with rows or without.
  const heldLimits = useRef(evidenceLimits)
  heldLimits.current = evidenceLimits
  const olderAsks = useRef(new Map<string, { limit: number; settle: () => void }[]>())
  const showOlder = useCallback(
    (key: string, shown: number) =>
      new Promise<void>((settle) => {
        const limit = shown + EVIDENCE_PAGE
        if ((heldLimits.current[key] ?? 0) >= limit) {
          // already asked for, and that read failed: read again at the same size
          const load = loaders.current.get(key)
          if (load) void load().finally(settle)
          else settle()
          return
        }
        olderAsks.current.set(key, [...(olderAsks.current.get(key) ?? []), { limit, settle }])
        setEvidenceLimits((held) => ({ ...held, [key]: limit }))
      }),
    [],
  )
  const onSettled = useCallback((key: string, limit: number) => {
    const asks = olderAsks.current.get(key) ?? []
    asks.filter((ask) => ask.limit <= limit).forEach((ask) => ask.settle())
    const left = asks.filter((ask) => ask.limit > limit)
    if (left.length) olderAsks.current.set(key, left)
    else olderAsks.current.delete(key)
  }, [])
  // Each re-read of any run that was in flight re-reads the case and the record too, the last one when it ends.
  const onRefreshRef = useRef(onRefresh)
  onRefreshRef.current = onRefresh
  const onReread = useCallback(() => {
    quietRecord.current = true
    setRecordKey((k) => k + 1)
    onRefreshRef.current?.()
  }, [])
  const runs: CaseRun[] = caseRuns.map((ref) => ({ ref, fold: reads[ref.run_id]?.fold ?? null, phase: reads[ref.run_id]?.phase ?? 'loading' }))
  const fold = runId ? reads[runId]?.fold ?? null : null
  // The run id is unknown until the case read returns, so the run is still loading, not absent.
  const foldPhase: Phase = phase === 'loading' && !runId ? 'loading' : !runId ? 'ready' : reads[runId]?.phase ?? 'loading'
  // The run the four record tabs are narrowed to; All once that run is no longer on the case.
  const filter = runs.some((one) => one.ref.run_id === runFilter) ? runFilter : null
  const shownRuns = filter ? runs.filter((one) => one.ref.run_id === filter) : runs
  useEffect(() => {
    if (runFilter && !filter) setRunFilter(null)
  }, [runFilter, filter])

  // A case refresh that moves another run on the case (a hunt's root-cause follow-up) re-reads the record in place.
  const otherRuns = investigations.filter((item) => !item.primary).map((item) => `${item.run_id}:${item.status}`).join(' ')
  const seenOthers = useRef<{ id: string; runs: string } | null>(null)
  useEffect(() => {
    if (phase !== 'ready') return // the first read of the case is not a change
    const prev = seenOthers.current
    seenOthers.current = { id, runs: otherRuns }
    if (!prev || prev.id !== id || prev.runs === otherRuns) return
    quietRecord.current = true
    setRecordKey((k) => k + 1)
  }, [id, otherRuns, phase])

  // Each run is named once, on its tag, the filter, the cost lines and the Record's rows.
  const runLabels = useMemo(() => Object.fromEntries(investigations.map((item) => [item.run_id, runLabel(item, workflowNames)])), [investigations, workflowNames])

  // A live run may have added evidence the answer just cited; re-read without blanking the page.
  const refreshFold = useCallback(() => {
    if (runId) void loaders.current.get(runId)?.()
  }, [runId])

  useEffect(() => {
    let cancelled = false
    workflowApi
      .listAll()
      .then((res) => {
        if (cancelled) return
        const list = (res.data?.workflows ?? []) as { id: string; name?: string }[]
        setWorkflowNames(Object.fromEntries(list.filter((w) => w.name).map((w) => [w.id, w.name as string])))
      })
      .catch(() => undefined) // the header falls back to the id
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    casesApi
      .getSLA(id)
      .then((res) => {
        if (cancelled) return
        // null: the case has no SLA
        const due = res.data?.resolution_due
        const health = res.data?.health_status
        setSla(due ? { due, health: health || '' } : null)
      })
      .catch(() => {
        if (!cancelled) setSla(null)
      })
    return () => {
      cancelled = true
    }
  }, [id])

  useEffect(() => {
    let cancelled = false
    const quiet = quietRecord.current && recordReady.current // only a record on screen is re-read in place
    quietRecord.current = false
    if (!quiet) {
      setRecordPhase('loading')
      setRecordError(null)
    }
    casesApi
      .getRecord(id)
      .then((res) => {
        if (cancelled) return
        setRows(res.data.rows || [])
        setRecordPhase('ready')
      })
      .catch((e) => {
        if (cancelled || quiet) return
        setRows([])
        setRecordError(detailOf(e, 'Couldn’t load the record'))
        setRecordPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [id, recordKey])

  const tabCounts = runs.length > 1 ? caseCounts(fold, runs.map((one) => one.fold), rows.length) : counts(fold, rows.length)
  const shown = chip === 'all' ? rows : rows.filter((row) => recordChip(row.kind, row.source) === chip)
  const chipCounts = useMemo(() => {
    const counts: Record<RecordChip, number> = { agent: 0, human: 0, memory: 0, system: 0 }
    for (const row of rows) counts[recordChip(row.kind, row.source)] += 1
    return counts
  }, [rows])

  const promptFor = (text?: string) => {
    const base = c
      ? `Investigate case ${c.id}: "${c.title}" — ${c.prio} priority, status ${pill}, ${c.findings} linked findings.`
      : `Investigate case ${id}.`
    return text ? `${base}\n\nWhy?\n${text}` : base
  }

  const verify = async () => {
    if (!runId) return
    const ticket = ++recStatusTicket.current
    const report = (status: { level: Level; text: string }) => {
      if (ticket === recStatusTicket.current) setRecStatus(status)
    }
    setBusy(true)
    report({ level: null, text: 'Verifying…' })
    try {
      const res = await workflowApi.verifyRun(runId)
      const data = res.data as { ok?: boolean; events?: number; reason?: string }
      report(
        data.ok
          ? { level: 'good', text: `Chain verified · ${data.events ?? 0} events` }
          : { level: 'poor', text: `Chain break${data.reason ? `: ${data.reason}` : ''}` },
      )
    } catch (e) {
      report({ level: 'poor', text: detailOf(e, 'Verify failed') })
    } finally {
      setBusy(false)
    }
  }

  const download = async () => {
    if (!latest?.investigation_id) return
    const ticket = ++recStatusTicket.current
    setBusy(true)
    setRecStatus(null)
    try {
      const res = await orchestratorApi.exportInvestigation(latest.investigation_id)
      const blob = res.data instanceof Blob ? res.data : new Blob([JSON.stringify(res.data)])
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${latest.investigation_id}-export`
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      if (ticket === recStatusTicket.current) setRecStatus({ level: 'poor', text: detailOf(e, 'Export failed') })
    } finally {
      setBusy(false)
    }
  }

  // a fused commit just landed or failed (maybe after this page was closed and reopened): refresh.
  // onChanged is a fresh closure each render, so it is read through a ref, not an effect dependency.
  const onChangedRef = useRef(onChanged)
  onChangedRef.current = onChanged
  const seenSettled = useRef(settled)
  const lastPending = useRef(pending)
  useEffect(() => {
    const before = lastPending.current
    lastPending.current = pending
    // A fuse started or was undone: nothing was sent.
    if (seenSettled.current === settled) return
    seenSettled.current = settled
    // What was just sent leaves the block now, not when the re-read lands; a failed one comes back with it.
    const sent = before.filter((key) => !pending.includes(key))
    if (sent.length > 0) setNeedsItems((items) => items.filter((it) => !sent.includes(it.source_id)))
    void loadNeeds()
    onChangedRef.current()
  }, [settled, pending, loadNeeds])

  // Reversible approve and every reject commit through the toast's undo fuse, the same path as
  // Home's card, and survive closing the case. A held (irreversible) approve commits at once via decide().
  const fused = useFusedDecision(decisionFailText)

  /** A decision drawn from the run before the queue served it is answered through the queue's own item. */
  const throughQueue = async (item: NeedsYouItem, act: (served: NeedsYouItem) => void) => {
    if (!item.source_id.startsWith(FROM_RUN)) return act(item)
    const forCase = id
    try {
      const res = await approvalsApi.needsYou(forCase)
      if (seenCase.current !== forCase) return
      const served = res.data.items.find((it) => it.checkpoint_id === item.checkpoint_id)
      if (served) {
        setNeedsItems(res.data.items)
        setNeedsCount(res.data.count)
        act(served)
      } else setDecisionError('This decision has not reached the approvals queue yet. Try again in a moment.')
    } catch (e) {
      if (seenCase.current === forCase) setDecisionError(detailOf(e, 'Could not update that decision'))
    }
  }

  const answered = (item: NeedsYouItem, answer: 'approve' | 'reject') => {
    const cp = item.checkpoint_id
    if (cp) setAnswers((held) => ({ ...held, [cp]: answer }))
  }

  const approve = (item: NeedsYouItem) =>
    void throughQueue(item, (served) => {
      answered(served, 'approve')
      if (served.reversibility === 'reversible') fused.approve(served)
      else void decide(served.source_id, () => approvalsApi.approve(served.source_id))
    })

  const reject = (item: NeedsYouItem, reason: string) =>
    void throughQueue(item, (served) => {
      answered(served, 'reject')
      fused.reject(served, reason)
    })

  const decide = async (sourceId: string, act: () => Promise<unknown>) => {
    if (decisionBusyRef.current) return
    const forCase = id
    decisionBusyRef.current = sourceId
    setDecisionBusy(sourceId)
    setDecisionError(null)
    try {
      await act()
      if (seenCase.current === forCase) {
        await loadNeeds()
        onChanged()
      }
    } catch (e) {
      if (seenCase.current === forCase) setDecisionError(detailOf(e, 'Could not update that decision'))
    } finally {
      if (seenCase.current === forCase && decisionBusyRef.current === sourceId) {
        decisionBusyRef.current = null
        setDecisionBusy(null)
      }
    }
  }

  const reopen = async () => {
    setBusy(true)
    setNote('')
    try {
      await casesApi.update(id, { status: 'open' })
      onChanged()
    } catch (e) {
      setNote(detailOf(e, 'Couldn’t reopen the case'))
    } finally {
      setBusy(false)
    }
  }

  // The question the run itself is stopped on. The queue mirrors it a moment later, and polls slower
  // than the run is read, so until it serves it the decision is drawn from the run; once the queue has
  // served it and it has gone (here, or from Home), it was answered and the run has yet to read that.
  const runCheckpoint = fold?.kind === 'hunt' && !closed ? fold.openCheckpoint : null
  const queued = runCheckpoint ? needsItems.find((it) => it.checkpoint_id === runCheckpoint.checkpoint_id) : undefined
  const awaitingQueue = runCheckpoint !== null && !queued && !seenCheckpoints.has(runCheckpoint.checkpoint_id)
  const answeredHere = runCheckpoint !== null && !awaitingQueue && (!queued || pending.includes(queued.source_id))
  const answer = runCheckpoint ? answers[runCheckpoint.checkpoint_id] ?? null : null
  // Each read of the run asks the queue again until it serves the run's question.
  useEffect(() => {
    if (awaitingQueue) void loadNeeds()
  }, [awaitingQueue, fold, loadNeeds])
  // A run that moves on (resumes, parks, ends) changes what waits on a person.
  const runMood = `${fold?.run.status}|${fold?.run.huntStatus}|${runCheckpoint?.checkpoint_id ?? ''}`
  const lastMood = useRef(runMood)
  useEffect(() => {
    if (lastMood.current === runMood) return
    lastMood.current = runMood
    void loadNeeds()
  }, [runMood, loadNeeds])

  // items whose fuse is running stay out of the block, the ask and the count, so a poll can't bring them back
  const served = needsItems.filter((it) => !pending.includes(it.source_id))
  const shownNeeds = awaitingQueue && runCheckpoint ? [fromRun(runCheckpoint, id, runId), ...served] : served
  const needsShown = Math.max(0, needsCount - (needsItems.length - served.length)) + (shownNeeds.length - served.length)
  const hypotheses = foldRecord(fold)?.hypotheses ?? []
  const citations = caseCitations(fold, linkedFindings)
  const citeById = new Map(citations.map((cite) => [cite.id, cite]))
  const openCite = (cite: Citation) => {
    setTab(cite.kind === 'explanation' ? 'Explanations' : 'Evidence')
    setRunFilter((held) => (held === runId ? held : null)) // citations point at the primary run
    if (cite.kind === 'evidence') setFocusEvidence(cite.id)
  }
  const left = sla && !closed ? timeLeft(sla.due) : '' // a closed case's clock has stopped
  // Needs you wins; otherwise a run that is paused or stopped says so over the server's combined state.
  const stopped = closed || needsShown > 0 || answeredHere ? null : stoppedRun(fold)
  const ended = closed || needsShown > 0 ? null : endedRun(fold)
  const pillState = closed ? 'closed' : answeredHere ? (answer === 'reject' ? 'stopped' : 'executing') : stopped?.state ?? pill
  // Full page only: the drawer has its own Escape handler. Escape leaves the page unless something
  // else took the key (a menu or dialog is showing, or a text field has focus). A closed dialog that
  // stays mounted, like the Ask Vigil dock, is aria-hidden and doesn't count.
  useEffect(() => {
    if (onExpand) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.defaultPrevented) return
      const t = e.target as HTMLElement | null
      if (t?.closest('input, textarea, select, [contenteditable="true"]')) return
      const showing = [...document.querySelectorAll('[role="dialog"], [role="menu"]')].some(
        (el) => !el.closest('[aria-hidden="true"], [hidden]'),
      )
      if (showing) return
      onBack()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onExpand, onBack])

  const watch = <WatchLink runId={runId} onFollow={() => onExpand && onBack()} />
  const doors = <Doors counts={tabCounts} lines={doorLines(runs.length > 1 ? runs.map((one) => one.fold) : [fold], foldPhase, runId !== null, rows, recordPhase)} onOpen={setTab} />
  const tone = statePill(pillState, needsShown > 0).tone
  // Running furniture needs a run: a run-less case, or one whose needs item sits in the undo fuse, shows none.
  const running = runId !== null && !closed && !stopped && !ended && !answeredHere && tone !== 'needs'
  // Reason after the pill: the ask, where a live run stands (its directive is in the Now card), or who closed it.
  const reason =
    tone === 'needs' ? needsAsk(shownNeeds) : answeredHere ? '' : tone === 'live' ? runSentence(fold) : closed ? closedLine(closure) : ''
  // Newest run first, which is not `runs` order (primary first).
  const verdict = caseVerdict(
    [...runs]
      .sort((a, b) => (b.ref.created_at ?? '').localeCompare(a.ref.created_at ?? ''))
      .map((one) => ({ fold: one.fold, label: runLabel(one.ref, workflowNames), phase: one.phase })),
  )
  // A case with several runs can ask where they part ways; the brief carries every run.
  const started = howStarted(runs)
  const startedCard = started && <HowStarted started={started} names={workflowNames} onFollow={() => onExpand && onBack()} />
  const disagree = investigations.length > 1 && (
    <div className="case-disagree">
      <button type="button" className="btn" onClick={() => setAskSeed({ id, text: DISAGREE_ASK })}>
        Where do the runs disagree?
      </button>
    </div>
  )
  const needsBlock = (
    <CaseNeeds
      key={id} // Show all is per case
      items={shownNeeds}
      busy={decisionBusy !== null}
      error={decisionError}
      board={hypotheses}
      onApprove={approve}
      onReject={reject}
    />
  )

  return (
    <div className="detail-pane case-page">
      {caseRuns.map((ref) => (
        <RunReader key={ref.run_id} runId={ref.run_id} evidenceLimit={evidenceLimits[ref.run_id]} onRead={onRead} onLoader={onLoader} onSettled={onSettled} onReread={onReread} />
      ))}
      <div className="detail-head">
        <div className="dh-crumb">
          <button type="button" className="back" onClick={onBack}>Cases</button>
          <span aria-hidden>›</span>
          <span className="mono">{id.startsWith('case-') ? id : `Case ${id}`}</span>
          <div className="dh-crumb-end">
            {c && <CaseMenu onEdit={onEdit} onMerge={onMerge} onDelete={canDelete ? onDelete : undefined} />}
            {onExpand && (
              <button type="button" className="btn ghost icon" aria-label="Expand" title="Expand" onClick={onExpand}>
                <Icon name="fit" size={15} />
              </button>
            )}
            <button type="button" className="btn ghost icon" aria-label="Close" title="Close" onClick={onBack}>
              <Icon name="close" size={15} />
            </button>
          </div>
        </div>
        {phase === 'error' ? (
          <div className="muted" style={{ padding: '6px 0' }}>Couldn’t load this case: {error}</div>
        ) : c ? (
          <>
            <h2>{c.title}</h2>
            <div className="case-state-line">
              <SeverityMark level={c.prio} />
              <StatePill state={pillState} needs={needsShown > 0} />
              {reason && <span className="case-reason clamp2" title={reason}>{reason}</span>}
            </div>
            <div className="dh-meta">
              {latest && <span>{workflowNames[latest.workflow_id] || latest.workflow_id}</span>}
              {c.findings > 0 && <span>Combined from {c.findings} {c.findings === 1 ? 'alert' : 'alerts'}</span>}
              <span>Opened {created}</span>
              <span>
                <Icon name="clock" size={13} /> Resolve by {sla ? when(sla.due) : '—'}
                {left && (
                  <>
                    {' · '}
                    <span className={`case-sla ${slaLevel(sla?.health) ?? ''}`} title={sla?.health ? `SLA ${sla.health}` : undefined}>{left}</span>
                  </>
                )}
              </span>
              <NotMeasured className="case-trust" />
            </div>
          </>
        ) : (
          <div className="muted" style={{ padding: '6px 0' }}>Loading case…</div>
        )}
        <TabStrip
          label="Case sections"
          tabs={TABS.map((name) => ({ id: name, label: name, count: name === 'Summary' ? undefined : tabCounts[name] }))}
          active={tab}
          onChange={setTab}
        />
      </div>

      {needsShown > 0 && tab !== 'Summary' && <NeedsStrip ask={needsAsk(shownNeeds)} onDecide={() => setTab('Summary')} />}

      <div className="case-stage">
        <div className="detail-body" key={tab}>
          {tab === 'Summary' && (
            closed ? (
              <>
                {needsBlock}
                <section className="case-closed" aria-label="Closed summary">
                  <span className="case-closed-line">
                    {closedLine(closure)}
                  </span>
                  <h3>{closure?.verdict || '—'}</h3>
                  {verdict && closureFlag(closure, verdict.word) && <VerdictBlock verdict={verdict} card={false} />}
                  <ClosedRows fold={fold} phase={foldPhase} />
                  <div className="case-assess">
                    <span>Your assessment</span>
                    <button type="button" className="btn" disabled title={LATER_RELEASE}>Agree</button>
                    <button type="button" className="btn" disabled title={LATER_RELEASE}>Disagree</button>
                    <span className="case-assess-later">{LATER_RELEASE}</span>
                    <div className="case-actions">
                      <button className="btn" onClick={reopen} disabled={busy}>Reopen</button>
                    </div>
                  </div>
                </section>
                {startedCard}
                {disagree}
                {doors}
              </>
            ) : (
              <>
                {needsBlock}
                {verdict && <VerdictBlock verdict={verdict} card />}
                {startedCard}
                {running && <NowCard fold={fold} phase={foldPhase} watch={watch} />}
                {answeredHere && needsShown === 0 && <AnsweredCard answer={answer} watch={watch} />}
                {stopped && <StoppedCard stopped={stopped} runId={runId} watch={watch} />}
                {ended && <OutcomeCard ended={ended} watch={watch} onAsk={(text) => setAskSeed({ id, text })} />}
                <OtherRuns runs={runs.filter((one) => one.ref !== latest)} names={workflowNames} onFollow={() => onExpand && onBack()} />
                {runId !== null && (
                  <section className="case-card" aria-label="Findings so far">
                    <h3>Findings so far</h3>
                    <FindingList fold={fold} />
                  </section>
                )}
                {disagree}
                {running && (
                  <>
                    <LaterRow title="What it changed" line="What this phase changed in the estate." />
                    <LaterRow title="Planned next" line="What the run intends to do next." />
                  </>
                )}
                <AgentsTable fold={fold} phase={foldPhase} />
                {doors}
              </>
            )
          )}

          {runs.length > 1 && RUN_TABS.includes(tab) && (
            <RunFilter runs={runs} labels={runLabels} value={filter} onChange={setRunFilter} />
          )}

          {tab === 'Explanations' && (
            runs.length > 1 ? (
              <RunExplanations runs={shownRuns} labels={runLabels} primaryId={runId} rootCauseRunId={rootCauseRunId} cites={citeById} onCite={openCite} />
            ) : (
              <Explanations fold={fold} phase={foldPhase} rootCauseRunId={rootCauseRunId} cites={citeById} onCite={openCite} />
            )
          )}

          {tab === 'Evidence' && (
            runs.length > 1 ? (
              <MergedEvidenceTrail
                runs={shownRuns.map((one) => ({ id: one.ref.run_id, label: runLabels[one.ref.run_id], fold: one.fold, phase: one.phase }))}
                primaryId={runId}
                focusId={focusEvidence}
                onShowOlder={showOlder}
              />
            ) : (
              <EvidenceTrail
                fold={fold}
                phase={foldPhase}
                focusId={focusEvidence}
                onShowOlder={runId ? (shown) => showOlder(runId, shown) : undefined}
              />
            )
          )}

          {tab === 'Checked' && (runs.length > 1 ? <MergedChecked runs={shownRuns} labels={runLabels} /> : <Checked fold={fold} phase={foldPhase} runId={runId} />)}

          {tab === 'Memory and blind spots' && (runs.length > 1 ? <MergedMemory runs={shownRuns} labels={runLabels} /> : <Memory fold={fold} phase={foldPhase} runId={runId} />)}

          {tab === 'Record' && (
            <section className="rec-card">
              <div className="rec-bar">
                <div className="rec-chips" role="group" aria-label="Record source">
                  <FilterChip label={`All ${rows.length}`} active={chip === 'all'} onClick={() => setChip('all')} />
                  {RECORD_CHIPS.map((name) => (
                    <FilterChip key={name} label={`${name[0].toUpperCase() + name.slice(1)} ${chipCounts[name]}`} active={chip === name} onClick={() => setChip(name)} />
                  ))}
                  <InfoTip
                    label="About the record"
                    align="start"
                    source="Each run’s journal, the case audit log and the case’s activity history, newest first."
                    calculation="Each row is typed Agent, Human, Memory or System from what wrote it."
                    limit="Only run rows are hash-chained. Case audit and activity rows are not."
                  />
                </div>
                <div className="rec-actions">
                  <span className="rec-status" role="status" aria-live="polite">
                    {recStatus && (
                      <>
                        <LevelBadge level={recStatus.level} variant="pill" />
                        {recStatus.text}
                      </>
                    )}
                  </span>
                  {runId ? (
                    <Link className="rec-btn" to={`/workflows?run=${encodeURIComponent(runId)}`} onClick={() => onExpand && onBack()}>
                      Replay
                    </Link>
                  ) : (
                    <>
                      <button type="button" className="rec-btn" disabled>Replay</button>
                      <InfoTip label="Why Replay is unavailable" text="This case has no run to replay." />
                    </>
                  )}
                  {runId && <button type="button" className="rec-btn" onClick={verify} disabled={busy}>Verify chain</button>}
                  {latest && (latest.investigation_id ? (
                    <button type="button" className="rec-btn" onClick={download} disabled={busy}>Export audit</button>
                  ) : (
                    <>
                      <button type="button" className="rec-btn" disabled>Export audit</button>
                      <InfoTip label="Why Export audit is unavailable" text="This run has no investigation record to export." />
                    </>
                  ))}
                </div>
              </div>
              {recordPhase === 'loading' && <EmptyState loading compact icon="clock" title="Loading the record…" />}
              {recordPhase === 'error' && (
                <EmptyState
                  error
                  compact
                  icon="alert"
                  title="Couldn’t load the record"
                  body={recordError || undefined}
                  primary={{ label: 'Retry', onClick: () => setRecordKey((k) => k + 1), icon: 'refresh' }}
                />
              )}
              {recordPhase === 'ready' && shown.length === 0 && (
                <EmptyState compact icon="clock" title="No record yet" />
              )}
              {recordPhase === 'ready' && shown.length > 0 && (
                <div className="rec-rows">
                  {shown.map((row) => {
                    const type = recordChip(row.kind, row.source)
                    const runLabel = row.run_id ? runLabels[row.run_id] || row.run_id.slice(0, 8) : null
                    return (
                      <div key={row.id} className="rec-row">
                        <span className="rec-time" title={when(row.at)}>{clock(row.at)}</span>
                        <span className={`rec-type ${type}`}>{type[0].toUpperCase() + type.slice(1)}</span>
                        <span className="rec-text" title={row.text}>
                          {runLabel && <span className="rec-run">{runLabel}</span>}
                          {row.text || row.kind}
                          {row.chained && <span className="rec-chained">chained</span>}
                        </span>
                        <button type="button" className="rec-why" onClick={() => setAskSeed({ id, text: promptFor(row.text) })}>Why?</button>
                      </div>
                    )
                  })}
                </div>
              )}
            </section>
          )}
          {tab === 'Summary' && note && <p className="muted">{note}</p>}
        </div>

        <aside className="case-side" aria-label="Case details">
          <CaseSide key={id} caseId={id} linked={linkedFindings} owner={c?.ownerName || '—'} latest={latest} runs={runs} workflowNames={workflowNames} sla={sla} closed={closed} fold={fold} latestFold={runId === latest?.run_id ? fold : null} />
          <details className="case-fold">
            <summary>Files</summary>
            <EvidenceCard caseId={id} title="Files" />
          </details>
          <details className="case-fold">
            <summary>IOCs</summary>
            <IOCsCard caseId={id} />
          </details>
        </aside>
      </div>

      <Chat
        pinned
        open
        onClose={() => undefined}
        pageKey={pageKey}
        lockedCaseId={id}
        collapseKey={tab}
        seed={askSeed?.id === id ? askSeed.text : null}
        onSeedConsumed={() => setAskSeed(null)}
        onTurnDone={refreshFold}
        citations={citations}
        onCite={openCite}
      />
    </div>
  )
}

const NO_CITES: ReadonlyMap<string, Citation> = new Map()

/** All, then one entry per run in `investigations[]` order; narrows the four record tabs together. */
function RunFilter({ runs, labels, value, onChange }: { runs: CaseRun[]; labels: Record<string, string>; value: string | null; onChange: (runId: string | null) => void }) {
  return (
    <div className="rec-chips run-filter" role="group" aria-label="Run">
      <FilterChip label="All runs" active={value === null} onClick={() => onChange(null)} />
      {runs.map((one) => (
        <FilterChip key={one.ref.run_id} label={labels[one.ref.run_id]} title={labels[one.ref.run_id]} active={value === one.ref.run_id} onClick={() => onChange(one.ref.run_id)} />
      ))}
    </div>
  )
}

/** The honest line for runs that keep no record. */
function NoRecord({ head }: { head: boolean }) {
  return (
    <p>
      {honestLine()}
      <InfoTip
        label="About explanations"
        text={head ? 'A root-cause run traces a chain of steps, shown on Watch a run.' : 'Hunt, root cause, and adjudicate test explanations. This run does not.'}
        align="start"
      />
    </p>
  )
}

function ExplanationsCard({ children }: { children: ReactNode }) {
  return (
    <section className="case-expl-card">
      <div className="case-expl-head">
        <div>
          <span className="case-expl-title">Every explanation this case has held</span>
          <div className="case-expl-sub">Status, the rows for and against, who added it, and why it moved.</div>
        </div>
        <div className="case-expl-actions">
          <button type="button" disabled title={LATER_RELEASE}>+ Add an explanation</button>
          <button type="button" disabled title={LATER_RELEASE}>Rule one out</button>
        </div>
      </div>
      {children}
    </section>
  )
}

/** One run's explanations; the honest line for a run that keeps no record. */
function Explanations({ fold, phase, rootCauseRunId, cites, onCite }: {
  fold: RunFold | null
  phase: Phase
  rootCauseRunId: string | null
  cites: ReadonlyMap<string, Citation>
  onCite: (cite: Citation) => void
}) {
  if (phase === 'loading') return <p className="muted">Loading the run…</p>
  if (phase === 'error') return <p>The run could not be read.</p>
  const record = foldRecord(fold)
  if (!record) return <NoRecord head={fold?.kind === 'head'} />
  return (
    <ExplanationsCard>
      {record.hypotheses.length === 0 ? (
        <EmptyState compact icon="search" title="No explanations yet" />
      ) : (
        <ul className="case-expl-rows">
          {record.hypotheses.map((row) => (
            <ExplanationRow key={row.hypothesis_id} row={row} rootCauseRunId={rootCauseRunId} cites={cites} onCite={onCite} />
          ))}
        </ul>
      )}
    </ExplanationsCard>
  )
}

const runsOf = (runs: CaseRun[], labels: Record<string, string>) => runs.map((one) => ({ id: one.ref.run_id, label: labels[one.ref.run_id], phase: one.phase }))

/**
 * Every selected run's explanations in one list: by standing word, then newest run first. Rows are
 * never matched or paired across runs, so a run's rows stay keyed by the run.
 */
function RunExplanations({ runs, labels, primaryId, rootCauseRunId, cites, onCite }: {
  runs: CaseRun[]
  labels: Record<string, string>
  primaryId: string | null
  rootCauseRunId: string | null
  cites: ReadonlyMap<string, Citation>
  onCite: (cite: Citation) => void
}) {
  const read = runs.filter((one) => one.phase === 'ready')
  const kept = [...read]
    .sort((a, b) => (b.ref.created_at ?? '').localeCompare(a.ref.created_at ?? ''))
    .flatMap((one) => foldRecord(one.fold)?.hypotheses.map((row) => ({ run: one, row })) ?? [])
  const keeps = read.some((one) => foldRecord(one.fold))
  const rows = kept.sort((a, b) => doorRank(explanationWord(a.row.status, a.row.supports, a.row.weakens)) - doorRank(explanationWord(b.row.status, b.row.supports, b.row.weakens)))
  return (
    <>
      {keeps && (
        <ExplanationsCard>
          {rows.length === 0 ? (
            <EmptyState compact icon="search" title="No explanations yet" />
          ) : (
            <ul className="case-expl-rows">
              {rows.map(({ run, row }) => (
                <ExplanationRow
                  key={`${run.ref.run_id}:${row.hypothesis_id}`}
                  row={row}
                  rootCauseRunId={rootCauseRunId}
                  cites={run.ref.run_id === primaryId ? cites : NO_CITES}
                  onCite={onCite}
                  tag={labels[run.ref.run_id]}
                />
              ))}
            </ul>
          )}
        </ExplanationsCard>
      )}
      {!keeps && read.length === runs.length && <NoRecord head={read.every((one) => one.fold?.kind === 'head')} />}
      <RunNotes runs={runsOf(runs, labels)} />
    </>
  )
}

/** What one run asked, at what cost, and where it had no source. */
function Checked({ fold, phase, runId }: { fold: RunFold | null; phase: Phase; runId: string | null }) {
  const gaps = visibilityGaps(fold)
  if (phase === 'loading') return <EmptyState loading compact icon="search" title="Loading the run…" />
  if (phase === 'error') return <EmptyState error compact icon="search" title="The run could not be read." />
  if (!fold || (fold.calls.length === 0 && gaps.length === 0)) return <EmptyState compact icon="search" title="No questions asked yet" />
  return (
    <section className="case-card case-checked">
      <h3>
        What Vigil checked · {fold.calls.length} {fold.calls.length === 1 ? 'query' : 'queries'} · {fold.costUsd == null ? '—' : fmtCost(fold.costUsd)}
        <InfoTip
          label="About the cost"
          source="The run's total spend."
          calculation="Each row shows its turn's cost, so rows from one turn repeat the same figure and do not add up to the total."
        />
        {gaps.length > 0 && ` · ${gaps.length} with no source`}
      </h3>
      <CheckedList runId={runId} calls={fold.calls} />
    </section>
  )
}

/** What every selected run asked, under one header: each turn tagged with its run, a call opening its own run's ledger. */
function MergedChecked({ runs, labels }: { runs: CaseRun[]; labels: Record<string, string> }) {
  const read = runs.filter((one) => one.phase === 'ready' && one.fold)
  const calls = read.reduce((n, one) => n + one.fold!.calls.length, 0)
  const gaps = read.reduce((n, one) => n + visibilityGaps(one.fold).length, 0)
  const priced = read.flatMap((one) => (one.fold!.costUsd == null ? [] : [one.fold!.costUsd]))
  return (
    <>
      {calls > 0 || gaps > 0 ? (
        <section className="case-card case-checked">
          <h3>
            What Vigil checked · {calls} {calls === 1 ? 'query' : 'queries'} · {priced.length ? fmtCost(priced.reduce((a, b) => a + b, 0)) : '—'}
            <InfoTip
              label="About the cost"
              source="The selected runs' total spend, added."
              calculation="Each row shows its turn's cost, so rows from one turn repeat the same figure and do not add up to a run's total."
            />
            {gaps > 0 && ` · ${gaps} with no source`}
          </h3>
          {read.map((one) => <CheckedList key={one.ref.run_id} runId={one.ref.run_id} calls={one.fold!.calls} tag={labels[one.ref.run_id]} />)}
        </section>
      ) : (
        runs.every((one) => one.phase === 'ready') && <EmptyState compact icon="search" title="No questions asked yet" />
      )}
      <RunNotes runs={runsOf(runs, labels)} />
    </>
  )
}

/** Nothing for the Memory tab to show of this run. */
const memoryEmpty = (fold: RunFold | null) =>
  !fold?.recall && recallEntityCalls(fold).length === 0 && visibilityGaps(fold).length === 0 && leftOpen(fold).length === 0 && blindSpotsOf(fold).groups.length === 0

/** One run's recall and blind spots. */
function Memory({ fold, phase, runId }: { fold: RunFold | null; phase: Phase; runId: string | null }) {
  if (phase === 'loading') return <EmptyState loading compact icon="brain" title="Loading the run…" />
  if (phase === 'error') return <EmptyState error compact icon="alert" title="The run could not be read" />
  if (memoryEmpty(fold)) return <EmptyState compact icon="brain" title="No memory recorded" body="Recall is what the run journaled, not a fresh read." />
  return <MemoryCards parts={[{ fold, runId }]} />
}

/** Recalled and Blind spots across every selected run that has any, each row tagged with its run. */
function MergedMemory({ runs, labels }: { runs: CaseRun[]; labels: Record<string, string> }) {
  const parts = runs.filter((one) => one.phase === 'ready' && !memoryEmpty(one.fold)).map((one) => ({ fold: one.fold, runId: one.ref.run_id, tag: labels[one.ref.run_id] }))
  const settled = runs.every((one) => one.phase === 'ready')
  return (
    <>
      {parts.length > 0 ? (
        <MemoryCards parts={parts} />
      ) : (
        settled && <EmptyState compact icon="brain" title="No memory recorded" body="Recall is what the run journaled, not a fresh read." />
      )}
      <RunNotes runs={runsOf(runs, labels)} error="The run could not be read" />
    </>
  )
}

const KIND_LABEL: Record<string, string> = { hunt: 'Hunt', case: 'Case', analyst: 'Analyst' }

/** "Hunt h-12 · Jun 15, 2026": the investigation a recalled row concluded in. */
function provenance(p: RecallProvenance): string {
  const day = utcDay(p.concludedAt) ?? ''
  return [[KIND_LABEL[p.kind] ?? p.kind, p.id].filter(Boolean).join(' '), day].filter(Boolean).join(' · ')
}

function MemoryRow({ text, meta, tone, tag, run, action }: { text: string; meta?: string; tone?: 'poor'; tag?: string; run?: string; action?: ReactNode }) {
  return (
    <div className={`case-mem-row${tone ? ` ${tone}` : ''}`}>
      <span className="case-mem-text">{run && <RunTag label={run} />}{tag && <span className="tag">{tag}</span>}{text}</span>
      {(meta || action) && <span className="case-mem-meta">{meta}{action}</span>}
    </div>
  )
}

/** A run's share of the Memory tab; `tag` names the run when several are listed. */
interface MemoryPart { fold: RunFold | null; runId: string | null; tag?: string }

/** Recalled and Blind spots. Withdraw and Record a blind spot wait on Written back, so they are shown disabled. */
function MemoryCards({ parts }: { parts: MemoryPart[] }) {
  const each = parts.map((part) => {
    const recall = part.fold?.recall
    return { ...part, recall, opened: recall && !recall.unavailable ? recall : null, recalled: recallEntityCalls(part.fold), gaps: visibilityGaps(part.fold), open: leftOpen(part.fold) }
  })
  const none = each.every((part) => part.gaps.length === 0 && !part.recall?.gaps.length && part.open.length === 0)
  return (
    <>
      <section className="case-mem-card" aria-label="Recalled">
        <div className="case-mem-head"><h3>Recalled</h3></div>
        {each.map(({ runId, tag, recall, opened, recalled }) => (
          <Fragment key={runId ?? 'run'}>
            {recall?.unavailable ? (
              <p className="muted">{tag && <RunTag label={tag} />}Recall did not happen: {recall.unavailable}{recall.keys.length ? ` (${recall.keys.join(', ')})` : ''}</p>
            ) : opened ? (
              <p className="muted">{tag && <RunTag label={tag} />}{opened.keys.length ? `Asked about ${opened.keys.join(', ')}` : 'No entities recalled.'}</p>
            ) : (
              <p className="muted">{tag && <RunTag label={tag} />}The run did not journal an opening recall.</p>
            )}
            {(opened?.sightings.length || opened?.verdicts.length || recalled.length) ? (
              <div className="case-mem-rows">
                {opened?.sightings.map((row, i) => (
                  <MemoryRow
                    key={`s${i}`}
                    run={tag}
                    text={[row.entity, row.source, row.hits == null ? '' : `${row.hits} ${row.hits === 1 ? 'hit' : 'hits'}`].filter(Boolean).join(' · ')}
                    meta={provenance(row)}
                  />
                ))}
                {opened?.verdicts.map((row, i) => (
                  <MemoryRow
                    key={`v${i}`}
                    run={tag}
                    text={[row.outcome, row.statement].filter(Boolean).join(' — ')}
                    meta={provenance(row)}
                    action={<button type="button" className="btn ghost" disabled title={LATER_RELEASE}>Withdraw</button>}
                  />
                ))}
                {recalled.map((call, i) => (
                  <MemoryRow key={`c${i}`} run={tag} text={`recall_entity · ${call.question || '—'}`} meta={`${call.result_length} bytes`} />
                ))}
              </div>
            ) : null}
          </Fragment>
        ))}
      </section>
      <section className="case-mem-card" aria-label="Blind spots">
        <div className="case-mem-head">
          <h3>Blind spots that touched this case</h3>
          <button type="button" className="btn ghost" disabled title={LATER_RELEASE}>Record a blind spot</button>
        </div>
        {none && <p className="muted">None recorded.</p>}
        {each.map(({ fold, runId, tag, recall, gaps, open }) => (
          <Fragment key={runId ?? 'run'}>
            {(recall?.gaps.length || open.length > 0) ? (
              <div className="case-mem-rows">
                {recall?.gaps.map((gap, i) => (
                  <MemoryRow
                    key={`d${i}`}
                    tone="poor"
                    run={tag}
                    tag="Declared"
                    text={[gap.disposition.replace(/_/g, ' '), gap.statement].filter(Boolean).join(' — ')}
                    meta={provenance(gap)}
                  />
                ))}
                {open.map((q) => <MemoryRow key={q.question_id} tone="poor" run={tag} tag="Open question" text={q.question} />)}
              </div>
            ) : null}
            {blindSpotsOf(fold).groups.length > 0 ? (
              <BlindSpots fold={fold} runId={runId} tag={tag} />
            ) : gaps.length > 0 && (
              // A run whose calls do not say which failed: its gaps as it wrote them.
              <div className="case-mem-rows">
                {gaps.map((gap) => <MemoryRow key={gap.id} tone="poor" run={tag} tag="Visibility" text={gap.text} />)}
              </div>
            )}
          </Fragment>
        ))}
      </section>
    </>
  )
}

function LaterRow({ title, line }: { title: string; line: string }) {
  return (
    <div className="case-later" aria-disabled="true">
      <b>{title}</b>
      <span>{line}</span>
      <Mark text={LATER_RELEASE} />
    </div>
  )
}

function ClosedRows({ fold, phase }: { fold: RunFold | null; phase: Phase }) {
  if (phase === 'loading') return <p className="muted">Loading the run…</p>
  if (phase === 'error') return <p className="muted">The run could not be read.</p>
  const rows = strongestRows(fold)
  if (rows.length === 0) return <p className="muted">No findings yet.</p>
  return (
    <ul className="case-closed-rows">
      {rows.map((row, i) => (
        <li key={i}>
          <span className="mono">{row.step === '—' ? '—' : `Step ${row.step}`}</span>
          <span title={row.text}>{row.text}</span>
          <b className={row.stance === 'For' ? 'good' : row.stance === 'Against' ? 'poor' : undefined}>{row.stance ?? '—'}</b>
        </li>
      ))}
    </ul>
  )
}

/** Two lines, then an ellipsis; the full text is on hover. */
function Clamped({ text }: { text: string }) {
  return <span className="clamp2" title={text}>{text}</span>
}

/** Findings so far as rows, never as raw list markup: a hunt's explanations by their word, a lead's answers as one plain line each. */
function FindingList({ fold }: { fold: RunFold | null }) {
  if (!fold) return <p className="muted">No findings yet.</p>
  const ranked = [...(foldRecord(fold)?.hypotheses ?? [])].sort((a, b) => b.supports - a.supports || b.weakens - a.weakens)
  // A hunt's findings are its explanations; an investigation's are its answer, a held one first, then what it weighed.
  const lead = fold.kind === 'lead' ? fold : null
  const held = lead?.held ?? null
  const answers = lead?.findings ?? []
  if (!held && answers.length === 0 && ranked.length === 0) return <p className="muted">No findings yet.</p>
  return (
    <ul className="case-findings">
      {held && (
        <li>
          <span className="case-expl-pill poor">Held</span>
          <Clamped text={`${heldLine(held)}: ${held.reason}`} />
        </li>
      )}
      {answers.slice(0, 6).map((row, i) => (
        <li key={i}>
          <span className="case-findings-who">{row.agent_id}</span>
          <Clamped text={plainLine(row.answer) || '—'} />
        </li>
      ))}
      {ranked.slice(0, 6).map((row) => {
        const word = explanationWord(row.status, row.supports, row.weakens)
        return (
          <li key={row.hypothesis_id}>
            <span className={`case-expl-pill ${EXPL_TONE[word] ?? 'neutral'}`}>{wordDisplay(word)}</span>
            <Clamped text={row.statement || row.hypothesis_id} />
          </li>
        )
      })}
    </ul>
  )
}
