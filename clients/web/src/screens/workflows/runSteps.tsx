/* A run's steps, as Watch run and the Ask Vigil dock both draw them: the step model read off the
   run's record, and the cards that show one step each. */
import { Cost } from '../../shared/cost'
import { Icon } from '../../shared/icons'
import { STOP_CAUSES, stopCauseOf } from '../../shared/stopCause'
import { utcClock as hhmm } from '../../shared/utc'
import {
  callFailure, callLine, fmtDuration, moveWord, shownStatus,
  type CallFailure, type HuntView, type InvestigateDecisionView, type RootCauseEntry, type WfRunDetail,
} from './runRead'
import { Prose } from './WatchPanels'

/** What each kind of run is reduced to: one row per decision. `calls` is null when
 *  the record cannot say which calls followed (an older agent service). */
export interface Step {
  key: string
  /** The ledger iteration the panels read as of; an investigate step is its own number. */
  iteration: number
  /** The number the player shows, in the unit the run's step limit counts; its position when absent. */
  number?: number
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
export interface CallRow {
  tool: string
  length: number | null
  rows?: number
  durationMs: number | null
  failure: CallFailure | null
}
export type Mark = 'done' | 'failed' | 'running' | 'warn' | 'paused' | 'stopped' | 'pending'
/** Why the run is not working right now, though its steps may be on screen. */
export interface Halt { kind: 'needs_you' | 'paused' | 'failed' | 'cancelled' | 'rejected'; reason: string }
export const HUNT_LIKE_RUN_KINDS = ['hunt', 'adjudicate']

/** What the run is stopped on, if it is. A person's question outranks a pause; a dead run outranks both. */
export function haltOf(d: WfRunDetail): Halt | null {
  const cause = stopCauseOf(d.stop_cause)
  if (shownStatus(d) === 'rejected') return { kind: 'rejected', reason: d.error || d.reason || '' }
  if (d.status === 'failed' || d.status === 'cancelled') return { kind: d.status, reason: cause ? STOP_CAUSES[cause].badge : d.error || d.hunt?.reason || d.reason || '' }
  if (d.hunt?.open_checkpoint) return { kind: 'needs_you', reason: '' }
  if (d.status === 'paused' || d.hunt?.status === 'parked') return { kind: 'paused', reason: d.hunt?.reason || d.reason || '' }
  return null
}
export const haltCaption = (h: Halt) =>
  h.kind === 'needs_you' ? 'Stopped · needs you'
    : h.kind === 'paused' ? (h.reason ? `Paused · ${h.reason}` : 'Paused')
    : h.kind === 'rejected' ? h.reason || 'A person rejected a step.'
    : `Stopped · ${h.reason || h.kind}`
export const HALT_MARK: Record<Halt['kind'], Mark> = { needs_you: 'warn', paused: 'paused', failed: 'failed', cancelled: 'stopped', rejected: 'stopped' }

/** Ledger order: the projection sends moves newest-first. */
export function huntSteps(hunt: HuntView): Step[] {
  const calls = hunt.calls ?? []
  // calls only carry an iteration once the agent service stamps it
  const tied = calls.length === 0 || calls.some((c) => c.iteration !== undefined)
  return [...(hunt.moves ?? [])].reverse().map((m) => ({
    key: m.decision_id,
    iteration: m.iteration,
    // a hunt's step is a turn of its lead, which is what Limits counts against max_iterations
    number: m.iteration,
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

export function investigateSteps(decisions: InvestigateDecisionView[]): Step[] {
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
export function rootCauseSteps(entries: RootCauseEntry[]): Step[] {
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
export function markOf(i: number, cursor: number, step: Step, newest: boolean, live: boolean, halt: Halt | null): Mark {
  if (i > cursor) return 'pending'
  if (newest && halt) return HALT_MARK[halt.kind]
  if (live && newest) return 'running'
  return step.entry && step.calls?.some((c) => c.failure) ? 'failed' : 'done'
}

const MARK_LABEL: Record<Mark, string> = { done: 'Done', failed: 'Failed', running: 'Working on it', warn: 'Needs you', paused: 'Paused', stopped: 'Stopped', pending: 'Not started' }

export function StepMark({ mark }: { mark: Mark }) {
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

export function CallLine({ call }: { call: CallRow }) {
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
export function EntryBody({ entry, call }: { entry: RootCauseEntry; call: CallRow | undefined }) {
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

export function Trace({ step, open, onToggle }: { step: Step; open: boolean; onToggle: () => void }) {
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

export function Words({ label, text }: { label: string; text: string }) {
  return <Prose label={label} text={text} className="text-[12px] leading-[1.4] text-[var(--tx1)]" />
}

export function StepCard({ step, i, mark, error, selected, open, onPick, onToggle, cardRef }: {
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
          type="button" onClick={onPick} title={`Go to step ${step.number ?? i + 1}`}
          className="flex items-baseline gap-2 w-full p-0 bg-transparent text-left cursor-pointer"
          style={{ opacity: mark === 'pending' ? 0.5 : 1 }}
        >
          <span className="text-[12px] font-bold text-[var(--tx0)] shrink-0">{moveWord(step.action)}</span>
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
            {/* a step whose model said nothing before acting shows no empty label */}
            {step.rationale !== '' && !(error && step.rationale === error) && <Words label="model text" text={step.rationale} />}
            {error && <Words label="error" text={error} />}
          </>
        )}
      </div>
    </div>
  )
}

export type RunKind = 'hunt' | 'investigate' | 'root_cause' | 'playbook'

/** The kind the definition declared, which the run row records at start, before any projection exists. */
export function runKindOf(d: WfRunDetail): RunKind {
  const recorded = d.trigger_context?.run_kind ?? (d.projection as { run_kind?: unknown } | null | undefined)?.run_kind
  return typeof recorded === 'string' && HUNT_LIKE_RUN_KINDS.includes(recorded) ? 'hunt'
    : recorded === 'investigate' ? 'investigate' : recorded === 'root_cause' ? 'root_cause' : d.hunt ? 'hunt' : 'playbook'
}
