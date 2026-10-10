/* Watch a run (PRD AW-W5): one run replayed step by step from its record. Board:
   docs/design/console/boards/WorkflowRun.dc.html. The page reads the run; it asks
   for nothing new except the investigate replay RunDetail already reads. */
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Icon } from '../../shared/icons'
import { Cost } from '../../shared/cost'
import { utcClock as hhmm } from '../../shared/utc'
import {
  IN_FLIGHT, callFailure, callLine, fmtDuration, useInvestigateReplay,
  type CallFailure, type HuntView, type InvestigateDecisionView, type RootCauseBudgets, type RootCauseEntry, type WfRunDetail,
} from './runRead'
import { Heading, HuntPanels, InvestigatePanels, OtherPanels, Prose, RootCausePanels } from './WatchPanels'

/** What each kind of run is reduced to: one row per decision. `calls` is null when
 *  the record cannot say which calls followed (an older agent service). */
interface Step {
  key: string
  /** The ledger iteration the panels read as of; an investigate step is its own number. */
  iteration: number
  action: string
  worker: string | null
  at: string | null
  cost: number | null
  thoughtMs: number | null
  rationale: string
  calls: CallRow[] | null
  /** A root-cause entry draws its own body in place of the call trace and the model text. */
  entry?: RootCauseEntry
}
interface CallRow {
  tool: string
  length: number | null
  rows?: number
  durationMs: number | null
  failure: CallFailure | null
}
type Mark = 'done' | 'failed' | 'running' | 'warn' | 'paused' | 'stopped' | 'pending'
/** Why the run is not working right now, though its steps may be on screen. */
interface Halt { kind: 'needs_you' | 'paused' | 'failed' | 'cancelled'; reason: string }
const HUNT_LIKE_RUN_KINDS = ['hunt', 'adjudicate']

export const STEP_MS = 1400
export const UNSUPPORTED = 'Replay isn’t available for this kind of run yet.'
export const PLAYBOOK = 'A playbook runs its phases in order, with no lead agent to watch. Its phases are listed in History.'

const titled = (action: string) => {
  const words = action.replace(/[_-]+/g, ' ').trim().toLowerCase()
  return words.charAt(0).toUpperCase() + words.slice(1)
}

/** `threat-hunt` → "Threat hunt". The definition carries no display name, so the id is read as words. */
const workflowTitle = (name: string) => {
  const words = name.replace(/[_-]+/g, ' ').trim()
  return words.charAt(0).toUpperCase() + words.slice(1)
}

/** What the run is stopped on, if it is. A person's question outranks a pause; a dead run outranks both. */
function haltOf(d: WfRunDetail): Halt | null {
  if (d.status === 'failed' || d.status === 'cancelled') return { kind: d.status, reason: d.error || d.hunt?.reason || d.reason || '' }
  if (d.hunt?.open_checkpoint) return { kind: 'needs_you', reason: '' }
  if (d.status === 'paused' || d.hunt?.status === 'parked') return { kind: 'paused', reason: d.hunt?.reason || d.reason || '' }
  return null
}
const haltCaption = (h: Halt) =>
  h.kind === 'needs_you' ? 'Stopped · needs you' : h.kind === 'paused' ? (h.reason ? `Paused · ${h.reason}` : 'Paused') : `Stopped · ${h.reason || h.kind}`
const HALT_CLOCK: Record<Halt['kind'], string> = { needs_you: 'waiting on you', paused: 'paused', failed: 'stopped', cancelled: 'stopped' }
const HALT_MARK: Record<Halt['kind'], Mark> = { needs_you: 'warn', paused: 'paused', failed: 'failed', cancelled: 'stopped' }

/** Ledger order: the projection sends moves newest-first. */
function huntSteps(hunt: HuntView): Step[] {
  const calls = hunt.calls ?? []
  // calls only carry an iteration once the agent service stamps it
  const tied = calls.length === 0 || calls.some((c) => c.iteration !== undefined)
  return [...(hunt.moves ?? [])].reverse().map((m) => ({
    key: m.decision_id,
    iteration: m.iteration,
    action: m.action,
    worker: m.worker_agent_id ?? null,
    at: m.created_at ?? null,
    cost: m.cost_usd ?? null,
    thoughtMs: m.duration_ms ?? null,
    rationale: m.rationale,
    calls: tied
      ? calls.filter((c) => c.iteration === m.iteration)
          .map((c) => ({ tool: c.tool, length: c.result_length, durationMs: c.duration_ms ?? null, failure: c.failed ?? null }))
      : null,
  }))
}

function investigateSteps(decisions: InvestigateDecisionView[]): Step[] {
  return decisions.map((d) => ({
    key: String(d.iteration),
    iteration: d.iteration,
    action: d.action,
    worker: d.worker ?? null,
    at: d.at ?? null,
    cost: d.cost_usd,
    thoughtMs: d.duration_ms ?? null,
    rationale: d.rationale,
    calls: d.calls.map((call) => {
      const rec = (typeof call === 'object' && call !== null ? call : {}) as { result?: unknown; duration_ms?: unknown }
      return {
        tool: callLine(call).tool,
        length: typeof rec.result === 'string' ? rec.result.length : null,
        durationMs: typeof rec.duration_ms === 'number' ? rec.duration_ms : null,
        failure: callFailure(call),
      }
    }),
  }))
}

/** One step per ledger entry. A failed search carries its failure on its one call, so markOf marks it. */
function rootCauseSteps(entries: RootCauseEntry[]): Step[] {
  return entries.map((entry, i) => ({
    key: String(i),
    iteration: i,
    action: entry.kind === 'step' ? 'record' : entry.kind,
    worker: null,
    at: entry.recorded_at || null,
    cost: null,
    thoughtMs: null,
    rationale: '',
    // a failed search with no kind recorded is the store answering with an error row
    calls: entry.kind === 'search'
      ? [{ tool: entry.tool, length: null, rows: entry.rows, durationMs: null, failure: entry.failed ? (entry.failure ?? 'backend_error') : null }]
      : [],
    entry,
  }))
}

/** No status is stored per decision, so the mark comes from where the cursor is. A run that
 *  has stopped marks its newest step, and that wins. Calls that partly failed leave a step
 *  done (the call rows show them); only a search entry, which is one call, fails with it. */
function markOf(i: number, cursor: number, step: Step, newest: boolean, live: boolean, halt: Halt | null): Mark {
  if (i > cursor) return 'pending'
  if (newest && halt) return HALT_MARK[halt.kind]
  if (live && newest) return 'running'
  return step.entry && step.calls?.some((c) => c.failure) ? 'failed' : 'done'
}

const MARK_LABEL: Record<Mark, string> = { done: 'Done', failed: 'Failed', running: 'Working on it', warn: 'Needs you', paused: 'Paused', stopped: 'Stopped', pending: 'Not started' }

function StepMark({ mark }: { mark: Mark }) {
  return (
    <span role="img" aria-label={MARK_LABEL[mark]} title={MARK_LABEL[mark]} className="inline-flex shrink-0 w-[18px] h-[18px] pt-px">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true" className="block">
        {mark === 'pending' && <circle cx="12" cy="12" r="9" stroke="var(--tx3)" strokeWidth="2" strokeDasharray="2.6 3.4" />}
        {mark === 'running' && (
          <>
            <circle cx="12" cy="12" r="9" stroke="var(--ln2)" strokeWidth="2" />
            <circle
              cx="12" cy="12" r="9" stroke="var(--ac)" strokeWidth="2.2" strokeLinecap="round" strokeDasharray="36 60"
              style={{ transformBox: 'fill-box', transformOrigin: 'center', animation: 'vg-spin 1.1s linear infinite' }}
            />
          </>
        )}
        {mark === 'done' && (
          <>
            <circle cx="12" cy="12" r="9" fill="var(--good)" fillOpacity="0.14" stroke="var(--good)" strokeWidth="2" />
            <path d="M7.8 12.4l2.9 2.9 5.6-5.8" stroke="var(--good)" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
          </>
        )}
        {mark === 'warn' && (
          <>
            <circle cx="12" cy="12" r="9" fill="var(--fair)" fillOpacity="0.14" stroke="var(--fair)" strokeWidth="2" />
            <path d="M12 7.6v5.4M12 16.3v.1" stroke="var(--fair)" strokeWidth="2.4" strokeLinecap="round" />
          </>
        )}
        {mark === 'paused' && (
          <>
            <circle cx="12" cy="12" r="9" fill="var(--fair)" fillOpacity="0.14" stroke="var(--fair)" strokeWidth="2" />
            <path d="M10 8.6v6.8M14 8.6v6.8" stroke="var(--fair)" strokeWidth="2.2" strokeLinecap="round" />
          </>
        )}
        {mark === 'stopped' && (
          <>
            <circle cx="12" cy="12" r="9" fill="var(--tx3)" fillOpacity="0.14" stroke="var(--tx3)" strokeWidth="2" />
            <rect x="9" y="9" width="6" height="6" rx="1" fill="var(--tx3)" />
          </>
        )}
        {mark === 'failed' && (
          <>
            <circle cx="12" cy="12" r="9" fill="var(--poor)" fillOpacity="0.14" stroke="var(--poor)" strokeWidth="2" />
            <path d="M9.2 9.2l5.6 5.6M14.8 9.2l-5.6 5.6" stroke="var(--poor)" strokeWidth="2.2" strokeLinecap="round" />
          </>
        )}
      </svg>
    </span>
  )
}

const FAILURE_TEXT: Record<CallFailure, string> = {
  timeout: 'timed out',
  unavailable: 'failed · unavailable',
  backend_error: 'failed · backend error',
  refused: 'failed · refused',
  invalid_args: 'failed · invalid arguments',
}

function CallLine({ call }: { call: CallRow }) {
  // a timeout is a gap in what could be answered, not a defect in the call
  const color = call.failure === null ? 'var(--good)' : call.failure === 'timeout' ? 'var(--fair)' : 'var(--poor)'
  const rows = call.rows === undefined || call.failure !== null ? null : `${call.rows.toLocaleString()} row${call.rows === 1 ? '' : 's'}`
  const meta = [rows, call.length === null ? null : `${call.length.toLocaleString()} chars`, call.durationMs === null ? null : fmtDuration(call.durationMs)]
  return (
    <span className="flex items-start gap-2 text-[12px] leading-[1.45] text-[var(--tx1)]">
      <span className="inline-flex shrink-0 pt-0.5" style={{ color }}><Icon name={call.failure === null ? 'check' : 'close'} size={12} /></span>
      <span className="min-w-0 break-words">
        <span className="font-mono">{call.tool}</span>
        {meta.filter(Boolean).map((m) => <span key={m} className="text-[var(--tx2)]"> · {m}</span>)}
        {call.failure !== null && <span style={{ color }}> · {FAILURE_TEXT[call.failure]}</span>}
      </span>
    </span>
  )
}

/** What a root-cause entry says for itself. Nothing here is a rationale: the trace journals none. */
function EntryBody({ entry, call }: { entry: RootCauseEntry; call: CallRow | undefined }) {
  const prose = 'text-[12px] leading-[1.4] text-[var(--tx1)]'
  switch (entry.kind) {
    case 'search':
      return (
        <>
          {call && <CallLine call={call} />}
          {entry.args !== '' && <Prose text={entry.args} className={`font-mono ${prose}`} />}
        </>
      )
    case 'step':
      return (
        <>
          <Prose text={`Recorded ${entry.step_id} · ${entry.event}`} className={prose} />
          {entry.who !== '' && <span className="text-[11px] text-[var(--tx2)] break-words">{entry.who}</span>}
          {entry.cause_id !== null && <span className="text-[11px] text-[var(--tx2)] break-words">caused by {entry.cause_id}</span>}
        </>
      )
    case 'notice':
      return <Prose text={entry.text} className={prose} />
    default: {
      const never: never = entry
      return never
    }
  }
}

function Trace({ step, open, onToggle }: { step: Step; open: boolean; onToggle: () => void }) {
  const n = step.calls?.length ?? 0
  // "Thought for Ns" when the record has the lead's time, else the call count
  const label = step.thoughtMs !== null ? `Thought for ${fmtDuration(step.thoughtMs)}` : n > 0 ? `${n} call${n === 1 ? '' : 's'}` : null
  if (label === null) return null
  return (
    <>
      <button
        type="button" aria-expanded={open} onClick={onToggle}
        className="self-start inline-flex items-center gap-1.5 h-[22px] pr-1 rounded-md text-[12px] font-semibold text-[var(--tx2)] hover:text-[var(--tx0)] bg-transparent cursor-pointer"
      >
        <span className="inline-flex text-[var(--tx3)]"><Icon name="sparkle" size={12} /></span>
        {label}
        <span className="inline-flex transition-transform" style={{ transform: open ? 'rotate(90deg)' : 'none' }}><Icon name="chevR" size={12} /></span>
      </button>
      {open && (
        <div className="flex flex-col gap-1.5 ml-1.5 py-0.5 pl-3 border-l border-[var(--ln1)]">
          {step.calls === null && <span className="text-[12px] text-[var(--tx2)]">Which calls followed isn’t recorded for this run.</span>}
          {step.calls?.length === 0 && <span className="text-[12px] text-[var(--tx2)]">No calls followed.</span>}
          {step.calls?.map((call, at) => <CallLine key={at} call={call} />)}
        </div>
      )}
    </>
  )
}

function Words({ label, text }: { label: string; text: string }) {
  return <Prose label={label} text={text} className="text-[12px] leading-[1.4] text-[var(--tx1)]" />
}

function StepCard({ step, i, mark, error, selected, open, onPick, onToggle, cardRef }: {
  step: Step; i: number; mark: Mark; error: string | null; selected: boolean; open: boolean
  onPick: () => void; onToggle: () => void; cardRef: (el: HTMLDivElement | null) => void
}) {
  const time = hhmm(step.at)
  const shown = mark !== 'pending'
  return (
    <div
      ref={cardRef}
      className="flex gap-2.5 items-start px-3 py-2.5 rounded-[12px] border transition-colors"
      style={{ borderColor: selected ? 'var(--ac)' : 'var(--ln0)', background: selected ? 'var(--ac-bg)' : 'var(--bg1)' }}
    >
      <StepMark mark={mark} />
      <div className="flex flex-col gap-[5px] min-w-0 grow">
        <button
          type="button" onClick={onPick} title={`Go to step ${i + 1}`}
          className="flex items-baseline gap-2 w-full p-0 bg-transparent text-left cursor-pointer"
          style={{ opacity: mark === 'pending' ? 0.5 : 1 }}
        >
          <span className="text-[12px] font-bold text-[var(--tx0)] shrink-0">{titled(step.action)}</span>
          {step.worker && <span className="font-mono text-[11px] text-[var(--tx2)] truncate min-w-0">{step.worker}</span>}
          <span className="ml-auto flex items-baseline gap-2 shrink-0 text-[11px] text-[var(--tx3)]">
            {step.cost !== null && <Cost usd={step.cost} digits={3} />}
            {time && <span className="font-mono">{time}</span>}
          </span>
        </button>
        {shown && step.entry && <EntryBody entry={step.entry} call={step.calls?.[0]} />}
        {shown && !step.entry && mark === 'running' && step.calls?.length === 0 && (
          <span className="text-[12px] font-semibold text-[var(--tx2)]">Working on it…</span>
        )}
        {shown && !step.entry && <Trace step={step} open={open} onToggle={onToggle} />}
        {shown && !step.entry && (
          <>
            {/* the run's error can stand in for the rationale; it is never the model's words */}
            {!(error && (step.rationale === '' || step.rationale === error)) && <Words label="model text" text={step.rationale || '—'} />}
            {error && <Words label="error" text={error} />}
          </>
        )}
      </div>
    </div>
  )
}

function Segments({ n, cursor, playing, ended, onJump, labels }: { n: number; cursor: number; playing: boolean; ended: boolean; onJump: (i: number) => void; labels: string[] }) {
  return (
    <div role="list" aria-label="Steps" className="grow min-w-0 flex items-center gap-1.5">
      {Array.from({ length: n }, (_, i) => {
        const current = i === cursor
        return (
          <button
            key={i} type="button" role="listitem" title={labels[i]} aria-label={labels[i]} aria-current={current ? 'step' : undefined}
            onClick={() => onJump(i)}
            className="min-w-[4px] h-[10px] p-0 border-0 rounded-full overflow-hidden cursor-pointer"
            style={{
              flex: current ? '6 1 0' : '0 1 10px',
              background: i < cursor ? 'var(--ac)' : current ? 'var(--bg4)' : 'var(--ln2)',
              transition: 'flex .35s cubic-bezier(.2,.8,.2,1)',
            }}
          >
            {/* keyed on the step so each one's fill starts over */}
            <span
              key={`${i}-${cursor}`} className="block h-[10px] rounded-full bg-[var(--ac)]"
              // the newest step of a live run is part-way; the last step of a run that has ended is done
              style={{ width: i < cursor ? '100%' : current ? (playing || (ended && i === n - 1) ? '100%' : '35%') : '0%', animation: current && playing ? `wr-fill ${STEP_MS}ms linear both` : 'none' }}
            />
          </button>
        )
      })}
    </div>
  )
}

/** The player and the three columns. Owns the one selected step the panels read. */
function Replay({ steps, live, halt, note, panels }: { steps: Step[]; live: boolean; halt: Halt | null; note: string | null; panels: (at: number) => ReactNode }) {
  const last = steps.length - 1
  // a run opens on its newest step; a live one follows it until the viewer moves, and Replay restarts from step 1
  const [cursor, setCursor] = useState(last)
  const [follow, setFollow] = useState(live)
  const [playing, setPlaying] = useState(false)
  const [opened, setOpened] = useState<Record<string, boolean>>({})
  const cards = useRef<(HTMLDivElement | null)[]>([])
  const list = useRef<HTMLDivElement>(null)

  useEffect(() => { if (follow && live) setCursor(last) }, [follow, live, last])
  // one timer, cleared on every step, on pause and on unmount
  useEffect(() => {
    if (!playing) return
    // playback reaching the newest step of a live run hands the page back to the run
    if (cursor >= last) { setPlaying(false); if (live) setFollow(true); return }
    const timer = setTimeout(() => setCursor(cursor + 1), STEP_MS)
    return () => clearTimeout(timer)
  }, [playing, cursor, last, live])
  // the steps list follows the active step on its own; scrollIntoView would also move the page
  useEffect(() => {
    const box = list.current
    const card = cards.current[cursor]
    if (!box || !card) return
    const top = card.offsetTop // the box is the card's offset parent
    if (top < box.scrollTop) box.scrollTop = top
    else if (top + card.offsetHeight > box.scrollTop + box.clientHeight) box.scrollTop = top + card.offsetHeight - box.clientHeight
  }, [cursor])

  const at = Math.min(cursor, last)
  const jump = (i: number) => { setFollow(live && i >= last); setCursor(i) }
  const toggle = () => {
    setFollow(false)
    if (playing) return setPlaying(false)
    if (at >= last) setCursor(0)
    setPlaying(true)
  }
  const step = steps[at]
  const time = hhmm(step.at)
  const finished = at >= last && !playing
  const stopped = halt !== null && at === last
  const clock = stopped ? HALT_CLOCK[halt.kind] : live && at === last ? 'in progress' : 'replayed from the record'

  return (
    <>
      <div className="flex items-center gap-4 px-3.5 py-2.5 rounded-[12px] bg-[var(--bg1)] border border-[var(--ln0)]">
        <button
          type="button" onClick={toggle}
          aria-label={playing ? 'Pause the replay' : finished && !live ? 'Replay from the start' : 'Play the replay'}
          className="w-[34px] h-[34px] shrink-0 p-0 rounded-full border-0 bg-[var(--ac)] text-[var(--ac-tx)] inline-flex items-center justify-center cursor-pointer"
        >
          <Icon name={playing ? 'pause' : 'play'} size={15} style={playing ? undefined : { fill: 'currentColor' }} />
        </button>
        <Segments n={steps.length} cursor={at} playing={playing} ended={!live} onJump={jump} labels={steps.map((s, i) => `Step ${i + 1}: ${titled(s.action)}`)} />
        {stopped
          ? <span className="min-w-0 max-w-[45%] truncate text-[12px] font-semibold text-[var(--tx1)]" title={haltCaption(halt)}>{haltCaption(halt)}</span>
          : <span className="text-[12px] font-semibold text-[var(--tx1)] whitespace-nowrap">{`Step ${at + 1} of ${steps.length} · ${titled(step.action)}`}</span>}
        <span className="text-[12px] text-[var(--tx2)] whitespace-nowrap">{time ? `${time} · ${clock}` : clock}</span>
      </div>
      <div className="grid grid-cols-[minmax(0,1.25fr)_minmax(0,1.1fr)_minmax(0,0.8fr)] gap-3.5 items-start">
        <div className="flex flex-col gap-2 min-w-0">
          <Heading>What the lead agent did</Heading>
          {note && <span className="text-[12px] text-[var(--tx2)]">{note}</span>}
          <div ref={list} className="relative flex flex-col gap-2 overflow-y-auto max-h-[max(360px,calc(100vh-300px))]">
            {steps.map((s, i) => (
              <StepCard
                key={s.key} step={s} i={i} mark={markOf(i, at, s, i === last, live, halt)} error={i === last && (halt?.kind === 'failed' || halt?.kind === 'cancelled') ? halt.reason || null : null} selected={i === at}
                open={Boolean(opened[s.key])} onPick={() => jump(i)}
                onToggle={() => setOpened((o) => ({ ...o, [s.key]: !o[s.key] }))}
                cardRef={(el) => { cards.current[i] = el }}
              />
            ))}
          </div>
        </div>
        {panels(at)}
      </div>
    </>
  )
}

function OneLine({ children }: { children: React.ReactNode }) {
  return <div className="text-[12px] leading-[1.45] text-[var(--tx2)] px-3.5 py-2.5 rounded-[12px] bg-[var(--bg1)] border border-[var(--ln0)]">{children}</div>
}

/** The one line where the player would be, with why the run stopped when it did. */
function NoSteps({ live, halt }: { live: boolean; halt: Halt | null }) {
  // a halt reason often ends in its own period
  return <OneLine>{halt ? `${haltCaption(halt).replace(/[.\s]+$/, '')}. No steps were recorded for this run.` : live ? 'No steps yet. The first one shows here when the lead makes it.' : 'No steps were recorded for this run.'}</OneLine>
}

function HuntReplay({ d, hunt, live, halt }: { d: WfRunDetail; hunt: HuntView | null; live: boolean; halt: Halt | null }) {
  // the run row is there before the projection is: a hunt just started is a hunt, not yet with moves
  if (!hunt) return <OneLine>{halt ? haltCaption(halt) : 'Starting…'}</OneLine>
  const steps = huntSteps(hunt)
  if (steps.length === 0) return <NoSteps live={live} halt={halt} />
  // moves are capped at the newest few; a shorter list than the iteration count is a prefix cut off
  const cut = steps.length < hunt.iteration
  const panels = (at: number) => (
    <HuntPanels runId={d.run_id} hunt={hunt} iteration={steps[at].iteration} decisionId={steps[at].key} last={at === steps.length - 1} />
  )
  return <Replay key={d.run_id} steps={steps} live={live} halt={halt} note={cut ? `Showing the last ${steps.length} steps` : null} panels={panels} />
}

function InvestigateReplay({ d, live, halt }: { d: WfRunDetail; live: boolean; halt: Halt | null }) {
  const read = useInvestigateReplay(d.run_id, live)
  // a failed poll must not tear down a player that already has steps
  const held = useRef<InvestigateDecisionView[] | null>(null)
  if (read.kind === 'investigate') held.current = read.decisions
  if (read.kind === 'failed' && held.current) return <InvestigateSteps decisions={held.current} live={live} halt={halt} d={d} />
  if (read.kind === 'pending') return <OneLine>Loading steps…</OneLine>
  if (read.kind === 'absent' || read.kind === 'root_cause') return <OneLine>{UNSUPPORTED}</OneLine>
  if (read.kind === 'failed') return <OneLine>Couldn’t read the steps — {read.message}</OneLine>
  return <InvestigateSteps decisions={read.decisions} live={live} halt={halt} d={d} />
}

function InvestigateSteps({ decisions, live, halt, d }: { decisions: InvestigateDecisionView[]; live: boolean; halt: Halt | null; d: WfRunDetail }) {
  if (decisions.length === 0) return <NoSteps live={live} halt={halt} />
  const costs = decisions.map((x) => x.cost_usd)
  return <Replay key={d.run_id} steps={investigateSteps(decisions)} live={live} halt={halt} note={null} panels={(at) => <InvestigatePanels d={d} costs={costs} at={at} />} />
}

function RootCauseReplay({ d, live, halt }: { d: WfRunDetail; live: boolean; halt: Halt | null }) {
  const read = useInvestigateReplay(d.run_id, live)
  const held = useRef<Extract<typeof read, { kind: 'root_cause' }> | null>(null)
  if (read.kind === 'root_cause') held.current = read
  const shown = read.kind === 'root_cause' ? read : read.kind === 'failed' ? held.current : null
  if (shown) return <RootCauseSteps entries={shown.entries} budgets={shown.budgets} live={live} halt={halt} d={d} />
  if (read.kind === 'pending') return <OneLine>Loading steps…</OneLine>
  // an agent service that cannot replay this run still gets the run as it stands
  return (
    <>
      <OneLine>{read.kind === 'failed' ? `Couldn’t read the steps — ${read.message}` : UNSUPPORTED}</OneLine>
      <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-3.5 items-start"><OtherPanels d={d} /></div>
    </>
  )
}

function RootCauseSteps({ entries, budgets, live, halt, d }: { entries: RootCauseEntry[]; budgets: RootCauseBudgets; live: boolean; halt: Halt | null; d: WfRunDetail }) {
  if (entries.length === 0) return <NoSteps live={live} halt={halt} />
  return <Replay key={d.run_id} steps={rootCauseSteps(entries)} live={live} halt={halt} note={null} panels={(at) => <RootCausePanels d={d} entries={entries} budgets={budgets} at={at} />} />
}

function versionText(d: WfRunDetail): string {
  const v = d.workflow_version
  return typeof v === 'number' ? `version ${v}` : 'version not recorded'
}

/** What the hunt was asked, one claim per line: from the start request, else the beliefs the operator or definition put up. */
function hypothesisOf(d: WfRunDetail): string | null {
  const asked = d.trigger_context?.hypothesis
  const lines = typeof asked === 'string'
    ? asked.split('\n')
    : (d.hunt?.hypotheses ?? []).filter((h) => h.provenance === 'operator' || h.provenance === 'hunt_spec').map((h) => h.statement)
  return lines.map((l) => l.trim()).filter(Boolean).join(' · ') || null
}

function Header({ d, hypothesis, tail, onBack }: { d: WfRunDetail; hypothesis: string | null; tail: string | null; onBack: () => void }) {
  const name = d.workflow_name
  const caseId = d.hunt?.scope?.case_id ?? d.trigger_context?.case_id
  // a hunt with no name of its own carries the workflow id as its name; the heading already says which workflow
  const huntName = d.hunt?.name && d.hunt.name !== name ? d.hunt.name : null
  const about = [typeof caseId === 'string' && caseId ? `case ${caseId}` : null, huntName].filter(Boolean).join(': ')
  const workflow = name ? `${workflowTitle(name)} ${typeof d.workflow_version === 'number' ? versionText(d) : `(${versionText(d)})`}` : versionText(d)
  const lead = `${workflow}${about ? ` · ${about}` : ''}`
  const subtitle = ['Run ', <span key="id" className="font-mono" title={d.run_id}>{d.run_id.slice(0, 8)}</span>, ` · ${lead}${tail ? `${/[.?!]$/.test(lead) ? ' ' : '. '}${tail}` : ''}`]
  return (
    <div className="flex items-center gap-3">
      <button
        type="button" onClick={onBack}
        className="inline-flex items-center gap-1.5 h-[30px] px-2.5 rounded-[9px] border border-[var(--ln1)] bg-transparent text-[var(--tx1)] text-[12px] font-[650] cursor-pointer shrink-0"
      >
        <Icon name="chevL" size={13} /> Workflows
      </button>
      <div className="flex flex-col gap-0.5 min-w-0 grow">
        {/* inline weight: the shell's unlayered heading rules would beat a utility class */}
        <h2 className="m-0 text-[20px] leading-[1.25] tracking-[-0.2px] text-[var(--tx0)]" style={{ fontWeight: 700 }}>
          Watch it run{name ? ` · ${workflowTitle(name)}` : ''}
        </h2>
        {hypothesis && <span className="text-[13px] leading-[1.4] text-[var(--tx1)] line-clamp-2 break-words" title={hypothesis}>{hypothesis}</span>}
        <span className="text-[12px] leading-[1.45] text-[var(--tx2)]">{subtitle}</span>
      </div>
    </div>
  )
}

/** `d` is the run as getRun last saw it; RunView keeps it fresh while the run is in flight. */
export function WatchRun({ d, onBack }: { d: WfRunDetail; onBack: () => void }) {
  const live = IN_FLIGHT.includes(d.status)
  const halt = haltOf(d)
  // the kind the definition declared, which the run row records at start, before any projection exists
  const recorded = d.trigger_context?.run_kind ?? (d.projection as { run_kind?: unknown } | null | undefined)?.run_kind
  const kind = typeof recorded === 'string' && HUNT_LIKE_RUN_KINDS.includes(recorded) ? 'hunt'
    : recorded === 'investigate' ? 'investigate' : recorded === 'root_cause' ? 'root_cause' : d.hunt ? 'hunt' : 'playbook'
  const tail = kind === 'playbook' ? null : live && !halt ? 'Live.' : 'Replayed step by step from the record.'
  return (
    <div className="flex flex-col gap-3.5 px-[22px] pt-5 pb-[110px]">
      <Header d={d} hypothesis={kind === 'hunt' ? hypothesisOf(d) : null} tail={tail} onBack={onBack} />
      {kind === 'hunt' && <HuntReplay d={d} hunt={d.hunt ?? null} live={live} halt={halt} />}
      {kind === 'investigate' && <InvestigateReplay d={d} live={live} halt={halt} />}
      {kind === 'root_cause' && <RootCauseReplay d={d} live={live} halt={halt} />}
      {kind === 'playbook' && (
        <>
          <OneLine>{PLAYBOOK}</OneLine>
          <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-3.5 items-start"><OtherPanels d={d} /></div>
        </>
      )}
    </div>
  )
}
