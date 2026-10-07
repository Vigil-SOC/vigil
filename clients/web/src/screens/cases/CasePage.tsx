import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { format } from 'date-fns'
import { approvalsApi, casesApi, orchestratorApi, workflowApi, type CaseRecordRow, type NeedsYouItem } from '../../services/api'
import { IN_FLIGHT, useRunDetail } from '../workflows/runRead'
import { slaLevel } from '../../shared/LevelBadge'
import { NotMeasured } from '../../shared/NotMeasured'
import { SeverityMark } from '../../shared/SeverityMark'
import { StatePill, statePill } from '../../shared/StatePill'
import { TabStrip } from '../../shared/TabStrip'
import { HoldButton } from '../../shared/HoldButton'
import { InfoTip } from '../../shared/InfoTip'
import { Icon } from '../../shared/icons'
import { EmptyState } from '../../shared/ui'
import type { CaseRow } from '../../data/data'
import Chat from '../../shell/Chat'
import { CommentsCard, EvidenceCard, IOCsCard, TasksCard } from './CaseSections'
import {
  addedBy,
  agentRows,
  explanationWord,
  honestLine,
  moveTool,
  readFold,
  recallEntityCalls,
  recordChip,
  stoppedRun,
  strongestRows,
  visibilityGaps,
  wordDisplay,
  type CallRow,
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

const LATER = 'Later. Nothing writes this yet — it is the phase-2 Act contract.'
const CHAINED = 'Only the run’s rows are hash-chained. Case audit rows are not.'

/** Pill tone per explanationWord(); the three non-verdict words stay neutral. */
const EXPL_TONE: Record<string, string> = {
  proven: 'good',
  standing: 'good',
  forming: 'ac',
  weakened: 'poor',
  'ruled out': 'muted',
}

function Mark({ text }: { text: string }) {
  return (
    <span className="case-mark" title={text} aria-label={text}>
      <Icon name="info" size={14} />
    </span>
  )
}

function when(value?: string | null): string {
  if (!value) return '—'
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? value : format(d, 'MMM d, yyyy · HH:mm')
}

/** "Closed <time> by <who>"; a missing or unparseable time is dropped. */
function closedBy(closure: CaseClosureView | null): string {
  if (!closure) return ''
  const at = closure.closed_at ? new Date(closure.closed_at) : null
  const time = at && !Number.isNaN(at.getTime()) ? ` ${format(at, 'MMM d, yyyy · HH:mm')}` : ''
  const who = closure.closed_by ? ` by ${closure.closed_by}` : ''
  return time || who ? `Closed${time}${who}` : ''
}

function money(value: number | null | undefined): string {
  if (value == null) return '—'
  return `$${value.toFixed(4)}`
}

function clock(value: string | null | undefined): string {
  if (!value) return '—'
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? '—' : format(d, 'HH:mm')
}

function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`
}

function latency(ms: number | undefined): string {
  return ms == null ? '—' : `${ms} ms`
}

function detailOf(error: unknown, fallback: string): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  return (error as { message?: string })?.message || fallback
}

/** Resolve-by clock: "7 h left", or "2 d over" once past due. */
function timeLeft(due: string): string {
  const ms = new Date(due).getTime() - Date.now()
  if (Number.isNaN(ms)) return ''
  const min = Math.round(Math.abs(ms) / 60_000)
  const span = min < 60 ? `${min} min` : min < 48 * 60 ? `${Math.round(min / 60)} h` : `${Math.round(min / 1440)} d`
  return `${span} ${ms < 0 ? 'over' : 'left'}`
}

/** How long a decision has waited: "4 min", "7 h", "3 d". Empty when the stamp doesn't parse. */
function waiting(since: string): string {
  const ms = Date.now() - new Date(since).getTime()
  if (Number.isNaN(ms)) return ''
  const min = Math.max(0, Math.round(ms / 60_000))
  return min < 60 ? `${min} min` : min < 48 * 60 ? `${Math.round(min / 60)} h` : `${Math.round(min / 1440)} d`
}

function LinkedFindings({ items }: { items: CaseLinkedFinding[] }) {
  if (items.length === 0) return null
  return (
    <details className="case-fold" open>
      <summary>Alerts ({items.length})</summary>
      <ul className="case-linked">
        {items.map((item) => (
          <li key={item.finding_id}>
            <span>{item.title || item.description || item.finding_id}</span>
            {item.source_link && (
              <a href={item.source_link} target="_blank" rel="noreferrer">Open in source</a>
            )}
          </li>
        ))}
      </ul>
    </details>
  )
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

function CaseNeeds({
  items,
  busy,
  error,
  onApprove,
  onReject,
}: {
  items: NeedsYouItem[]
  busy: boolean
  error: string | null
  onApprove: (id: string) => void
  onReject: (id: string, reason: string) => void
}) {
  if (items.length === 0) return null
  return (
    <section className="case-needs" aria-label="Needs you">
      {error && <p role="alert">{error}</p>}
      {items.map((item) => (
        <CaseNeed key={item.source_id} item={item} busy={busy} onApprove={onApprove} onReject={onReject} />
      ))}
    </section>
  )
}

function CaseNeed({
  item,
  busy,
  onApprove,
  onReject,
}: {
  item: NeedsYouItem
  busy: boolean
  onApprove: (id: string) => void
  onReject: (id: string, reason: string) => void
}) {
  const [rejecting, setRejecting] = useState(false)
  const [reason, setReason] = useState('')
  const waited = waiting(item.created_at)
  const reversible = item.reversibility === 'reversible'

  return (
    <article>
      <span className="needs-head">
        <span className="needs-dot" aria-hidden="true" />
        Needs your decision{waited && ` · waiting ${waited}`}
      </span>
      <h3>{item.title}</h3>
      <dl className="needs-facts">
        {item.reason && (
          <>
            <dt>Why it stopped</dt>
            <dd>{item.reason}</dd>
          </>
        )}
        <dt>Reversibility</dt>
        <dd>{reversible ? 'Reversible' : 'Cannot be undone'}</dd>
      </dl>
      <div className="needs-actions">
        {reversible ? (
          <button type="button" className="btn" disabled={busy} onClick={() => onApprove(item.source_id)}>
            Approve
          </button>
        ) : (
          <HoldButton label="Hold to approve" disabled={busy} onConfirm={() => onApprove(item.source_id)} />
        )}
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
      {rejecting && (
        <form
          className="needs-reject"
          onSubmit={(event) => {
            event.preventDefault()
            const text = reason.trim()
            if (!text) return
            onReject(item.source_id, text)
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
      )}
    </article>
  )
}

/** Rows the Memory tab's two cards show. */
function memoryRows(fold: RunFold | null): number {
  const recall = fold?.recall
  const opening = recall && !recall.unavailable ? recall.sightings.length + recall.verdicts.length : 0
  return opening + recallEntityCalls(fold).length + (recall?.gaps.length ?? 0) + visibilityGaps(fold).length
}

function counts(fold: RunFold | null, record: number): Record<Tab, number> {
  const calls = fold?.calls.length ?? 0
  const memory = memoryRows(fold)
  if (fold?.kind === 'hunt') {
    return {
      Summary: fold.hypotheses.length,
      Explanations: fold.hypotheses.length,
      Evidence: fold.evidenceCount,
      Checked: calls,
      'Memory and blind spots': memory,
      Record: record,
    }
  }
  const findings = fold?.findings.length ?? 0
  return {
    Summary: findings,
    Explanations: 0,
    Evidence: findings,
    Checked: calls,
    'Memory and blind spots': memory,
    Record: record,
  }
}

const DOOR_ORDER = ['proven', 'standing', 'weakened', 'forming', 'ruled out']

/** The line under each door's count, from the fold already loaded. */
function doorLines(fold: RunFold | null, foldPhase: Phase, hasRun: boolean, rows: CaseRecordRow[], recordPhase: Phase): Record<Exclude<Tab, 'Summary'>, string> {
  const none = foldPhase === 'loading' ? 'Loading…' : foldPhase === 'error' ? 'Couldn’t read the run' : hasRun ? 'Run started' : 'No run yet'
  const tally = new Map<string, number>()
  if (fold?.kind === 'hunt') {
    for (const row of fold.hypotheses) {
      const word = explanationWord(row.status, row.supports, row.weakens)
      tally.set(word, (tally.get(word) ?? 0) + 1)
    }
  }
  const words = [...tally.keys()].sort((a, b) => {
    const [i, j] = [DOOR_ORDER.indexOf(a), DOOR_ORDER.indexOf(b)]
    return (i < 0 ? DOOR_ORDER.length : i) - (j < 0 ? DOOR_ORDER.length : j)
  })
  let evidence = none
  if (fold?.kind === 'hunt') {
    const links = fold.evidence.flatMap((row) => row.bears_on.map((link) => link.relation))
    const stance = (relation: string) => fold.evidence.filter((row) => row.bears_on.some((link) => link.relation === relation)).length
    evidence = links.length || fold.evidence.length ? `${stance('supports')} for · ${stance('weakens')} against · shown` : 'None shown'
  } else if (fold) {
    evidence = 'Findings, no for or against'
  }
  const recall = fold?.recall
  const recallRows = (recall ? recall.sightings.length + recall.verdicts.length + recall.gaps.length : 0) + recallEntityCalls(fold).length
  const blind = visibilityGaps(fold).length
  const memory = [recallRows && plural(recallRows, 'recall row'), blind && plural(blind, 'gap')].filter(Boolean).join(' · ')
  const chained = rows.filter((row) => row.chained).length
  return {
    Explanations: fold?.kind === 'hunt' ? (words.length ? words.map((w) => `${tally.get(w)} ${w.replace(/_/g, ' ')}`).join(' · ') : 'None yet') : fold ? 'Does not test explanations yet' : none,
    Evidence: evidence,
    Checked: fold ? `${money(fold.costUsd)} · ${plural(blind, 'gap')}` : none,
    'Memory and blind spots': recall?.unavailable ? 'Recall did not happen' : memory || (recall ? 'Recalled, no rows' : 'Nothing recalled'),
    Record: recordPhase === 'loading' ? 'Loading…' : recordPhase === 'error' ? 'Couldn’t load' : `${plural(rows.length, 'row')} · ${chained} chained`,
  }
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

/** Now · step N: the latest move, who has it, with which tool, since when. */
function NowCard({ fold, phase, hasRun }: { fold: RunFold | null; phase: Phase; hasRun: boolean }) {
  const move = fold?.moves[0]
  const meta = fold ? [fold.worker, moveTool(fold, move), `since ${clock(move?.at)}`].filter(Boolean).join(' · ') : ''
  return (
    <section className="case-now" aria-label="Now">
      <div className="now-head">
        <span className="now-dot" aria-hidden="true" />
        <b>{fold ? `Now · step ${fold.kind === 'hunt' ? fold.iteration : fold.iterations}` : 'Now'}</b>
        {meta && <span className="now-meta">{meta}</span>}
      </div>
      <p className="clamp2" title={phase === 'ready' ? fold?.doing : undefined}>
        {phase === 'loading' && 'Loading the run…'}
        {phase === 'error' && 'The run could not be read.'}
        {phase === 'ready' && (fold ? fold.doing || 'Nothing decided yet' : hasRun ? 'The run has started and has not reported yet.' : 'No run on this case yet.')}
      </p>
      {fold?.outcome && <p className="muted">Run outcome {fold.outcome}{fold.reason ? ` — ${fold.reason}` : ''}</p>}
    </section>
  )
}

/** A run that is not going on: paused or stopped, with the one-line why and the raw text behind the ⓘ. */
function StoppedCard({ stopped }: { stopped: StoppedRun }) {
  return (
    <section className="case-stopped" aria-label="Run state">
      <b>{display(stopped.state)}</b>
      <span className="clamp2" title={stopped.raw || stopped.line}>{stopped.line}</span>
      {stopped.raw && <InfoTip label="What the run reported" text={stopped.raw} align="start" />}
    </section>
  )
}

function AgentsTable({ fold, phase, live, state }: { fold: RunFold | null; phase: Phase; live: boolean; state: string }) {
  const rows = agentRows(fold)
  return (
    <section className="case-agents" aria-label="Agents on this case">
      <h3>Agents on this case, right now</h3>
      {phase === 'loading' && <p className="muted">Loading the run…</p>}
      {phase === 'error' && <p className="muted">The run could not be read.</p>}
      {phase === 'ready' && rows.length === 0 && <p className="muted">{live ? 'No agent has acted yet.' : 'No live investigation.'}</p>}
      {rows.length > 0 && (
        <div className="agent-rows" role="table" aria-label="Agents">
          {rows.map((row, i) => (
            <div key={row.who} className="agent-row" role="row">
              <span className="agent-who" role="cell" title={row.who}>{row.who}</span>
              <span className="agent-doing" role="cell" title={row.doing}>{row.doing || '—'}</span>
              <span className="agent-tool" role="cell" title={row.tool}>{row.tool || '—'}</span>
              <span className="agent-since" role="cell">{clock(row.at)}</span>
              {/* The run's state belongs to the agent holding the latest move. */}
              <span className={`agent-state${live ? ' live' : ''}`} role="cell">{i === 0 && state && (<><span className="state-dot" aria-hidden="true" />{state}</>)}</span>
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
  const [askSeed, setAskSeed] = useState<{ id: string; text: string } | null>(null)
  const [focusEvidence, setFocusEvidence] = useState<string | null>(null)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [needsItems, setNeedsItems] = useState<NeedsYouItem[]>([])
  const [needsCount, setNeedsCount] = useState(0)
  const [decisionBusy, setDecisionBusy] = useState<string | null>(null)
  const [decisionError, setDecisionError] = useState<string | null>(null)
  const decisionBusyRef = useRef<string | null>(null)
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
    setTab('Summary')
    setAskSeed(null)
    setFocusEvidence(null)
    setNote('')
    setChip('all')
  }, [id])

  const latest = investigations[0] ?? null
  const runId = latest?.run_id ?? null
  const live = investigations.filter((item) => item.live)
  // The list maps every status other than open/investigating to closed, so a
  // `new` case must not use that fallback while the detail read is in flight.
  const closed = combinedState === 'closed' || (phase === 'ready' && !combinedState && c?.status === 'closed')
  const pill = combinedState || (phase === 'loading' ? '…' : c?.status || '—')

  // The newest run, re-read on Watch a run's interval while it is in flight.
  const run = useRunDetail(runId ?? '', runId !== null)
  const { load: loadRun, setDphase: setRunPhase } = run
  useEffect(() => {
    if (!runId) return
    setRunPhase('loading')
    void loadRun()
  }, [runId, loadRun, setRunPhase])
  const detail = runId && run.detail?.run_id === runId ? run.detail : null
  const fold = useMemo(() => readFold(detail), [detail])
  const foldPhase: Phase = !runId || detail ? 'ready' : run.dphase === 'error' ? 'error' : 'loading'

  // Each re-read of a run that was in flight re-reads the case and the record too, the last one when it ends.
  const seen = useRef<{ runId: string; status: string } | null>(null)
  useEffect(() => {
    const prev = seen.current
    seen.current = detail && runId ? { runId, status: detail.status } : null
    if (!prev || !detail || prev.runId !== runId || !IN_FLIGHT.includes(prev.status)) return
    quietRecord.current = true
    setRecordKey((k) => k + 1)
    onRefresh?.()
  }, [detail]) // eslint-disable-line react-hooks/exhaustive-deps

  // A live run may have added evidence the answer just cited; re-read without blanking the page.
  const refreshFold = useCallback(() => {
    if (!runId) return
    workflowApi
      .getRun(runId)
      .then((res) => setFold(readFold(res.data)))
      .catch(() => undefined) // keep what is shown
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
        const due = res.data.resolution_due
        const health = res.data.health_status
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

  const tabCounts = counts(fold, rows.length)
  const gaps = visibilityGaps(fold)
  const recalled = recallEntityCalls(fold)
  const shown = chip === 'all' ? rows : rows.filter((row) => recordChip(row.kind) === chip)

  const promptFor = (text?: string) => {
    const base = c
      ? `Investigate case ${c.id}: "${c.title}" — ${c.prio} priority, status ${pill}, ${c.findings} linked findings.`
      : `Investigate case ${id}.`
    return text ? `${base}\n\nWhy?\n${text}` : base
  }

  const replay = async () => {
    if (!runId) return
    setBusy(true)
    setNote('')
    try {
      const res = await workflowApi.replayRun(runId)
      const data = res.data as { decisions?: unknown[] }
      const n = Array.isArray(data.decisions) ? data.decisions.length : 0
      setNote(n ? `Replay returned ${n} decision${n === 1 ? '' : 's'}.` : 'Replay returned.')
    } catch (e) {
      setNote(detailOf(e, 'Replay failed'))
    } finally {
      setBusy(false)
    }
  }

  const verify = async () => {
    if (!runId) return
    setBusy(true)
    setNote('')
    try {
      const res = await workflowApi.verifyRun(runId)
      const data = res.data as { ok?: boolean; events?: number; reason?: string }
      setNote(data.ok ? `Chain verified (${data.events ?? 0} events).` : `Chain break${data.reason ? `: ${data.reason}` : ''}.`)
    } catch (e) {
      setNote(detailOf(e, 'Verify failed'))
    } finally {
      setBusy(false)
    }
  }

  const download = async () => {
    if (!latest?.investigation_id) return
    setBusy(true)
    setNote('')
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
      setNote(detailOf(e, 'Export failed'))
    } finally {
      setBusy(false)
    }
  }

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

  const findings = fold?.kind === 'lead' ? fold.findings : []
  const hypotheses = fold?.kind === 'hunt' ? fold.hypotheses : []
  const left = sla && !closed ? timeLeft(sla.due) : '' // a closed case's clock has stopped
  // Needs you wins; otherwise a run that is paused or stopped says so over the server's combined state.
  const stopped = closed || needsCount > 0 ? null : stoppedRun(fold)
  const pillState = closed ? 'closed' : stopped?.state ?? pill
  // Only what exists: the live investigation's status, else the run's outcome.
  const runState = stopped?.state ?? (live[0]?.status || fold?.outcome || '').replace(/_/g, ' ')
  const doors = <Doors counts={tabCounts} lines={doorLines(fold, foldPhase, runId !== null, rows, recordPhase)} onOpen={setTab} />
  const tone = statePill(pillState, needsCount > 0).tone
  const running = !closed && !stopped && tone !== 'needs'
  // Reason after the pill: the ask, what a live run is doing, or who closed it.
  const reason =
    tone === 'needs' ? needsItems[0]?.title : tone === 'live' ? fold?.doing : closed ? closedBy(closure) : ''
  const needsBlock = (
    <CaseNeeds
      items={needsItems}
      busy={decisionBusy !== null}
      error={decisionError}
      onApprove={(sourceId) => void decide(sourceId, () => approvalsApi.approve(sourceId))}
      onReject={(sourceId, reason) => void decide(sourceId, () => approvalsApi.reject(sourceId, reason))}
    />
  )

  return (
    <div className="detail-pane case-page">
      <div className="detail-head">
        <div className="dh-crumb">
          <button type="button" className="back" onClick={onBack}>Cases</button>
          <span aria-hidden>›</span>
          <span className="mono">Case {id}</span>
          <div className="dh-crumb-end">
            {c && <CaseMenu onEdit={onEdit} onMerge={onMerge} onDelete={canDelete ? onDelete : undefined} />}
            {onExpand && (
              <>
                <button type="button" className="btn ghost icon" aria-label="Expand" title="Expand" onClick={onExpand}>
                  <Icon name="fit" size={15} />
                </button>
                <button type="button" className="btn ghost icon" aria-label="Close" title="Close" onClick={onBack}>
                  <Icon name="close" size={15} />
                </button>
              </>
            )}
          </div>
        </div>
        {phase === 'error' ? (
          <div className="muted" style={{ padding: '6px 0' }}>Couldn’t load this case: {error}</div>
        ) : c ? (
          <>
            <h2>{c.title}</h2>
            <div className="case-state-line">
              <SeverityMark level={c.prio} />
              <StatePill state={pillState} needs={needsCount > 0} />
              {reason && <span className="case-reason clamp2" title={reason}>{reason}</span>}
            </div>
            <div className="dh-meta">
              <span>{latest ? workflowNames[latest.workflow_id] || latest.workflow_id : 'No workflow'}</span>
              <span>{c.findings} alerts combined</span>
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
          tabs={TABS.map((name) => ({ id: name, label: name, count: tabCounts[name] }))}
          active={tab}
          onChange={setTab}
        />
      </div>

      {needsCount > 0 && tab !== 'Summary' && <NeedsStrip ask={needsItems[0]?.title} onDecide={() => setTab('Summary')} />}

      <div className="case-stage">
        <div className="detail-body" key={tab}>
          {tab === 'Summary' && (
            closed ? (
              <>
                {needsBlock}
                <section className="case-closed" aria-label="Closed summary">
                  <span className="case-closed-line">
                    {[closedBy(closure), CLOSURE_CATEGORIES.find((item) => item.value === closure?.closure_category)?.label ?? closure?.closure_category, closure?.closed_by_kind]
                      .filter(Boolean)
                      .join(' · ')}
                  </span>
                  <h3>{closure?.verdict || '—'}</h3>
                  <ClosedRows fold={fold} phase={foldPhase} />
                  <div className="case-assess">
                    <span>Your assessment</span>
                    <button type="button" className="btn" disabled title={LATER}>Agree</button>
                    <button type="button" className="btn" disabled title={LATER}>Disagree</button>
                    <span className="case-assess-later">Coming in a later release</span>
                    <div className="case-actions">
                      <button className="btn" onClick={reopen} disabled={busy}>Reopen</button>
                    </div>
                  </div>
                </section>
                {doors}
              </>
            ) : (
              <>
                {needsBlock}
                {running && <NowCard fold={fold} phase={foldPhase} hasRun={runId !== null} />}
                {stopped && <StoppedCard stopped={stopped} />}
                <section>
                  <h3>Findings so far</h3>
                  <FindingList fold={fold} />
                </section>
                {running && (
                  <>
                    <LaterRow title="What it changed" line="What this phase changed in the estate." />
                    <LaterRow title="Planned next" line="What the run intends to do next." />
                  </>
                )}
                <AgentsTable fold={fold} phase={foldPhase} live={live.length > 0 && !stopped} state={runState} />
                {doors}
              </>
            )
          )}

          {tab === 'Explanations' && (
            foldPhase === 'loading' ? (
              <p className="muted">Loading the run…</p>
            ) : foldPhase === 'error' ? (
              <p>The run could not be read.</p>
            ) : fold?.kind === 'hunt' ? (
              <section className="case-expl-card">
                <div className="case-expl-head">
                  <div>
                    <span className="case-expl-title">Every explanation this case has held</span>
                    <div className="case-expl-sub">Status, the rows for and against, who added it, and why it moved.</div>
                  </div>
                  <div className="case-expl-actions">
                    <button type="button" disabled title={LATER}>+ Add an explanation</button>
                    <button type="button" disabled title={LATER}>Rule one out</button>
                  </div>
                </div>
                {hypotheses.length === 0 ? (
                  <EmptyState compact icon="search" title="No explanations yet" />
                ) : (
                  <ul className="case-expl-rows">
                    {hypotheses.map((row) => {
                      const word = explanationWord(row.status, row.supports, row.weakens)
                      const by = addedBy(row.provenance)
                      return (
                        <li key={row.hypothesis_id}>
                          <div className="case-expl-main">
                            <div className="case-expl-line">
                              <span className={`case-expl-pill ${EXPL_TONE[word] ?? 'neutral'}`}>{wordDisplay(word)}</span>
                              <span className={`case-expl-text${word === 'ruled out' ? ' struck' : ''}`}>{row.statement || row.hypothesis_id}</span>
                            </div>
                            {row.resolution_reason && <div className="case-expl-note">{row.resolution_reason}</div>}
                          </div>
                          <div className="case-expl-side">
                            <div>
                              <span className="for">{row.supports} for</span> · <span className="against">{row.weakens} against</span>
                            </div>
                            {by && <div className="case-expl-by">Added by {by}</div>}
                          </div>
                        </li>
                      )
                    })}
                  </ul>
                )}
              </section>
            ) : (
              <p>
                {honestLine()}
                <InfoTip label="About explanations" text="Hunt, root cause, and adjudicate test explanations. This run does not." align="start" />
              </p>
            )
          )}

          {tab === 'Evidence' && (
            fold?.kind === 'hunt' ? (
              fold.evidence.length === 0 ? (
                <EmptyState compact icon="shield" title="No evidence yet" />
              ) : (
                <EvidenceTable
                  focusId={focusEvidence}
                  rows={fold.evidence.map((row) => ({
                    id: row.evidence_id,
                    step: String(row.iteration),
                    observation: row.is_gap ? `${row.summary} (gap)` : row.summary,
                    source: row.source_system,
                    bears: row.bears_on.map((link) => `${link.relation} ${link.hypothesis_id}`).join(', ') || '—',
                  }))}
                />
              )
            ) : findings.length === 0 ? (
              <EmptyState compact icon="shield" title="No evidence yet" />
            ) : (
              <EvidenceTable
                rows={findings.map((row, i) => ({
                  id: `${row.agent_id}-${i}`,
                  step: String(i + 1),
                  observation: row.answer || '—',
                  source: row.agent_id,
                  bears: '—',
                }))}
              />
            )
          )}

          {tab === 'Checked' && (
            !fold || fold.calls.length === 0 ? (
              <EmptyState compact icon="search" title="No questions asked yet" />
            ) : (
              <div className="table-wrap">
                <table className="tbl">
                  <thead><tr><th>Question</th><th>Tool</th><th>Result size</th><th>Cost</th><th>Latency</th></tr></thead>
                  <tbody>
                    {fold.calls.map((call, i) => (
                      <tr key={`${call.tool}-${i}`}>
                        <td>{call.question || '—'}</td>
                        <td>{call.tool || '—'}</td>
                        <td>{call.result_length}</td>
                        <td>{money(call.cost_usd)}</td>
                        <td>{latency(call.duration_ms)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          )}

          {tab === 'Memory and blind spots' && (
            foldPhase === 'loading' ? (
              <EmptyState loading compact icon="brain" title="Loading the run…" />
            ) : foldPhase === 'error' ? (
              <EmptyState error compact icon="alert" title="The run could not be read" />
            ) : !fold?.recall && recalled.length === 0 && gaps.length === 0 ? (
              <EmptyState compact icon="brain" title="No memory recorded" body="Recall is what the run journaled, not a fresh read." />
            ) : (
              <MemoryCards fold={fold} recalled={recalled} gaps={gaps} />
            )
          )}

          {tab === 'Record' && (
            <>
              <p>
                Newest first.
                <Mark text={CHAINED} />
              </p>
              <div className="case-chips" role="group" aria-label="Record source">
                {(['all', 'agent', 'human', 'memory', 'system'] as const).map((name) => (
                  <button key={name} className={`btn ghost${chip === name ? ' active' : ''}`} onClick={() => setChip(name)}>
                    {name}
                  </button>
                ))}
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
              {recordPhase === 'ready' && shown.map((row) => (
                <div key={row.id} className="case-record">
                  <span className="tag">{recordChip(row.kind)}</span>
                  <div>
                    <div>{row.text || row.kind}</div>
                    <div className="muted">{when(row.at)}{row.chained ? ' · chained' : ''}</div>
                  </div>
                  <button type="button" className="btn ghost" onClick={() => setAskSeed({ id, text: promptFor(row.text) })}>Why?</button>
                </div>
              ))}
              <div className="case-actions">
                {runId && <button className="btn" onClick={replay} disabled={busy}>Replay</button>}
                {runId && <button className="btn" onClick={verify} disabled={busy}>Verify</button>}
                {latest?.investigation_id && <button className="btn" onClick={download} disabled={busy}>Export</button>}
              </div>
            </>
          )}
          {note && <p className="muted">{note}</p>}
        </div>

        <aside className="case-side" aria-label="Case details">
          <LinkedFindings items={linkedFindings} />
          <div><span className="k">Workflow</span><div>{latest?.workflow_id || '—'}</div></div>
          <div>
            <span className="k">Budget</span>
            <div>
              {latest ? `${money(latest.cost_usd)} / ${money(latest.max_cost_usd)} · ${latest.budget_health}` : '—'}
            </div>
          </div>
          <div>
            <span className="k">Resolve by</span>
            <div>{sla ? `${when(sla.due)}${sla.health ? ` · ${sla.health}` : ''}` : '—'}</div>
          </div>
          <div>
            <span className="k">Entities</span>
            <div>{fold?.recall && !fold.recall.unavailable && fold.recall.keys.length ? fold.recall.keys.join(', ') : '—'}</div>
          </div>
          <div><span className="k">Cost</span><div>{money(fold?.costUsd ?? latest?.cost_usd)}</div></div>
          <details className="case-fold">
            <summary>People</summary>
            <p>Owner {c?.ownerName || '—'}</p>
            <CommentsCard caseId={id} />
            <TasksCard caseId={id} />
            <Tickets caseId={id} />
          </details>
          <details className="case-fold">
            <summary>Known about these entities</summary>
            <p className="muted">
              {fold?.recall
                ? fold.recall.unavailable
                  ? `Recall did not happen: ${fold.recall.unavailable}`
                  : `${fold.recall.keys.join(', ') || 'No entities'}${fold.recall.verdicts.length ? `. Verdicts: ${fold.recall.verdicts.map((v) => [v.outcome, v.statement].filter(Boolean).join(' — ')).join('; ')}` : ''}${fold.recall.gaps.length ? `. Gaps: ${fold.recall.gaps.map((g) => g.statement).join('; ')}` : ''}`
                : 'The run did not journal a recall.'}
            </p>
          </details>
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
        seed={askSeed?.id === id ? askSeed.text : null}
        onSeedConsumed={() => setAskSeed(null)}
        onTurnDone={refreshFold}
        evidenceIds={fold?.kind === 'hunt' ? fold.evidence.map((row) => row.evidence_id) : []}
        onCite={(evidenceId) => {
          setTab('Evidence')
          setFocusEvidence(evidenceId)
        }}
      />
    </div>
  )
}

const KIND_LABEL: Record<string, string> = { hunt: 'Hunt', case: 'Case', analyst: 'Analyst' }
const LATER_TIP = 'Coming in a later release'

/** "Hunt h-12 · Jun 15, 2026": the investigation a recalled row concluded in. */
function provenance(p: RecallProvenance): string {
  const d = new Date(p.concludedAt)
  const day = p.concludedAt && !Number.isNaN(d.getTime()) ? format(d, 'MMM d, yyyy') : ''
  return [[KIND_LABEL[p.kind] ?? p.kind, p.id].filter(Boolean).join(' '), day].filter(Boolean).join(' · ')
}

function MemoryRow({ text, meta, tone, tag, action }: { text: string; meta?: string; tone?: 'poor'; tag?: string; action?: ReactNode }) {
  return (
    <div className={`case-mem-row${tone ? ` ${tone}` : ''}`}>
      <span className="case-mem-text">{tag && <span className="tag">{tag}</span>}{text}</span>
      {(meta || action) && <span className="case-mem-meta">{meta}{action}</span>}
    </div>
  )
}

/** Recalled and Blind spots. Withdraw and Record a blind spot wait on Written back, so they are shown disabled. */
function MemoryCards({ fold, recalled, gaps }: { fold: RunFold | null; recalled: CallRow[]; gaps: { id: string; text: string }[] }) {
  const recall = fold?.recall
  const opened = recall && !recall.unavailable ? recall : null
  return (
    <>
      <section className="case-mem-card" aria-label="Recalled">
        <div className="case-mem-head"><h3>Recalled</h3></div>
        {recall?.unavailable ? (
          <p className="muted">Recall did not happen: {recall.unavailable}{recall.keys.length ? ` (${recall.keys.join(', ')})` : ''}</p>
        ) : opened ? (
          <p className="muted">{opened.keys.length ? `Asked about ${opened.keys.join(', ')}` : 'No entities recalled.'}</p>
        ) : (
          <p className="muted">The run did not journal an opening recall.</p>
        )}
        {(opened?.sightings.length || opened?.verdicts.length || recalled.length) ? (
          <div className="case-mem-rows">
            {opened?.sightings.map((row, i) => (
              <MemoryRow
                key={`s${i}`}
                text={[row.entity, row.source, row.hits == null ? '' : `${row.hits} ${row.hits === 1 ? 'hit' : 'hits'}`].filter(Boolean).join(' · ')}
                meta={provenance(row)}
              />
            ))}
            {opened?.verdicts.map((row, i) => (
              <MemoryRow
                key={`v${i}`}
                text={[row.outcome, row.statement].filter(Boolean).join(' — ')}
                meta={provenance(row)}
                action={<button type="button" className="btn ghost" disabled title={LATER_TIP}>Withdraw</button>}
              />
            ))}
            {recalled.map((call, i) => (
              <MemoryRow key={`c${i}`} text={`recall_entity · ${call.question || '—'}`} meta={`${call.result_length} bytes`} />
            ))}
          </div>
        ) : null}
      </section>
      <section className="case-mem-card" aria-label="Blind spots">
        <div className="case-mem-head">
          <h3>Blind spots that touched this case</h3>
          <button type="button" className="btn ghost" disabled title={LATER_TIP}>Record a blind spot</button>
        </div>
        {gaps.length === 0 && !recall?.gaps.length ? (
          <p className="muted">None recorded.</p>
        ) : (
          <div className="case-mem-rows">
            {recall?.gaps.map((gap, i) => (
              <MemoryRow
                key={`d${i}`}
                tone="poor"
                tag="Declared"
                text={[gap.disposition.replace(/_/g, ' '), gap.statement].filter(Boolean).join(' — ')}
                meta={provenance(gap)}
              />
            ))}
            {gaps.map((gap) => <MemoryRow key={gap.id} tone="poor" tag="Visibility" text={gap.text} />)}
          </div>
        )}
      </section>
    </>
  )
}

function LaterRow({ title, line }: { title: string; line: string }) {
  return (
    <div className="case-later" aria-disabled="true">
      <b>{title}</b>
      <span>{line}</span>
      <Mark text={LATER} />
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

function FindingList({ fold }: { fold: RunFold | null }) {
  if (!fold) return <p className="muted">No findings yet.</p>
  if (fold.kind === 'hunt') {
    if (fold.hypotheses.length === 0) return <p className="muted">No findings yet.</p>
    const ranked = [...fold.hypotheses].sort((a, b) => b.supports - a.supports || b.weakens - a.weakens)
    return (
      <ul>
        {ranked.slice(0, 6).map((row) => (
          <li key={row.hypothesis_id}>
            <Clamped text={`${explanationWord(row.status, row.supports, row.weakens).replace(/_/g, ' ')} — ${row.statement || row.hypothesis_id}`} />
          </li>
        ))}
      </ul>
    )
  }
  if (fold.findings.length === 0) return <p className="muted">No findings yet.</p>
  return (
    <ul>
      {fold.findings.slice(0, 6).map((row, i) => (
        <li key={i}><Clamped text={`${row.agent_id}: ${row.answer || '—'}`} /></li>
      ))}
    </ul>
  )
}

function EvidenceTable({
  rows,
  focusId,
}: {
  rows: { id: string; step: string; observation: string; source: string; bears: string }[]
  focusId?: string | null
}) {
  const focusRef = useRef<HTMLTableRowElement>(null)
  useEffect(() => {
    const node = focusRef.current
    if (node && typeof node.scrollIntoView === 'function') node.scrollIntoView({ block: 'nearest' })
  }, [focusId, rows])
  return (
    <div className="table-wrap">
      <table className="tbl">
        <thead><tr><th>Step</th><th>Observation</th><th>Source</th><th>Bears on</th></tr></thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={row.id}
              ref={row.id === focusId ? focusRef : undefined}
              className={row.id === focusId ? 'cite-target' : undefined}
              data-evidence-id={row.id}
            >
              <td>{row.step}</td>
              <td>{row.observation || '—'}</td>
              <td>{row.source || '—'}</td>
              <td>{row.bears}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Tickets({ caseId }: { caseId: string }) {
  const [rows, setRows] = useState<{ id: string; label: string }[]>([])
  const [phase, setPhase] = useState<Phase>('loading')

  useEffect(() => {
    let cancelled = false
    casesApi
      .getEscalations(caseId)
      .then((res) => {
        if (cancelled) return
        setRows((res.data.escalations || []).map((row) => ({
          id: String(row.escalation_id ?? row.escalated_to),
          label: [row.escalated_to, row.reason].filter(Boolean).join(' — ') || 'Ticket',
        })))
        setPhase('ready')
      })
      .catch(() => {
        if (!cancelled) setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [caseId])

  return (
    <section>
      <h3>Linked tickets</h3>
      {phase === 'loading' && <p className="muted">Loading tickets…</p>}
      {phase === 'error' && <p className="muted">Couldn’t load tickets.</p>}
      {phase === 'ready' && rows.length === 0 && <p className="muted">No linked tickets.</p>}
      {rows.map((row) => <p key={row.id}>{row.label}</p>)}
    </section>
  )
}
