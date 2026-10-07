/* Watch a run (PRD AW-W5): one run replayed step by step from its record. Board:
   docs/design/console/boards/WorkflowRun.dc.html. The page reads the run; it asks
   for nothing new except the investigate replay RunDetail already reads. */
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Icon } from '../../shared/icons'
import { Cost } from '../../shared/cost'
import {
  IN_FLIGHT, callFailure, callLine, fmtDuration, useInvestigateReplay,
  type CallFailure, type HuntView, type InvestigateDecisionView, type RootCauseBudgets, type RootCauseEntry, type WfRunDetail,
} from './runRead'
import { Heading, HuntPanels, InvestigatePanels, OtherPanels, RootCausePanels } from './WatchPanels'

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
type Mark = 'done' | 'failed' | 'running' | 'warn' | 'pending'

export const STEP_MS = 1400
export const UNSUPPORTED = 'Replay isn’t available for this kind of run yet.'
export const PLAYBOOK = 'A playbook runs its phases in order, with no lead agent to watch. Its phases are listed in History.'

const hhmm = (iso?: string | null): string | null => {
  const t = iso ? new Date(iso) : null
  return t && !Number.isNaN(t.getTime()) ? t.toISOString().slice(11, 16) : null
}
const titled = (action: string) => {
  const words = action.replace(/[_-]+/g, ' ').trim().toLowerCase()
  return words.charAt(0).toUpperCase() + words.slice(1)
}

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

/** No status is stored per decision, so the mark comes from where the cursor is. A run
 *  waiting on a person is stopped, not working, and that wins over a failed call. */
function markOf(i: number, cursor: number, step: Step, newest: boolean, live: boolean, waiting: boolean): Mark {
  if (i > cursor) return 'pending'
  if (newest && waiting) return 'warn'
  if (live && newest) return 'running'
  return step.calls?.some((c) => c.failure) ? 'failed' : 'done'
}

const MARK_LABEL: Record<Mark, string> = { done: 'Done', failed: 'Failed', running: 'Working on it', warn: 'Needs you', pending: 'Not started' }

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
function EntryBody({ entry, call, selected }: { entry: RootCauseEntry; call: CallRow | undefined; selected: boolean }) {
  const clamp = selected ? '' : 'line-clamp-2'
  switch (entry.kind) {
    case 'search':
      return (
        <>
          {call && <CallLine call={call} />}
          {entry.args !== '' && <span className={`font-mono text-[12px] leading-[1.4] text-[var(--tx1)] break-words ${clamp}`} title={entry.args}>{entry.args}</span>}
        </>
      )
    case 'step':
      return (
        <>
          <span className={`text-[12px] leading-[1.4] text-[var(--tx1)] break-words ${clamp}`} title={entry.event}>Recorded {entry.step_id} · {entry.event}</span>
          {entry.who !== '' && <span className="text-[11px] text-[var(--tx2)] break-words">{entry.who}</span>}
          {entry.cause_id !== null && <span className="text-[11px] text-[var(--tx2)] break-words">caused by {entry.cause_id}</span>}
        </>
      )
    case 'notice':
      return <span className={`text-[12px] leading-[1.4] text-[var(--tx1)] break-words ${clamp}`} title={entry.text}>{entry.text}</span>
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

function StepCard({ step, i, mark, selected, open, onPick, onToggle, cardRef }: {
  step: Step; i: number; mark: Mark; selected: boolean; open: boolean
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
        {shown && step.entry && <EntryBody entry={step.entry} call={step.calls?.[0]} selected={selected} />}
        {shown && !step.entry && mark === 'running' && step.calls?.length === 0 && (
          <span className="text-[12px] font-semibold text-[var(--tx2)]">Working on it…</span>
        )}
        {shown && !step.entry && <Trace step={step} open={open} onToggle={onToggle} />}
        {shown && !step.entry && (
          <span
            className={`text-[12px] leading-[1.4] text-[var(--tx1)] ${selected ? '' : 'line-clamp-2'}`}
            title={step.rationale || undefined}
          >
            <span className="text-[11px] uppercase tracking-[0.06em] text-[var(--tx2)] mr-1.5">model text</span>
            {step.rationale || '—'}
          </span>
        )}
      </div>
    </div>
  )
}

function Segments({ n, cursor, playing, onJump, labels }: { n: number; cursor: number; playing: boolean; onJump: (i: number) => void; labels: string[] }) {
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
              style={{ width: i < cursor ? '100%' : current ? (playing ? '100%' : '35%') : '0%', animation: current && playing ? `wr-fill ${STEP_MS}ms linear both` : 'none' }}
            />
          </button>
        )
      })}
    </div>
  )
}

/** The player and the three columns. Owns the one selected step the panels read. */
function Replay({ steps, live, waiting, note, panels }: { steps: Step[]; live: boolean; waiting: boolean; note: string | null; panels: (at: number) => ReactNode }) {
  const last = steps.length - 1
  // a run in flight follows its newest step until the viewer moves; a finished one starts at 1
  const [cursor, setCursor] = useState(live ? last : 0)
  const [follow, setFollow] = useState(live)
  const [playing, setPlaying] = useState(false)
  const [opened, setOpened] = useState<Record<string, boolean>>({})
  const cards = useRef<(HTMLDivElement | null)[]>([])

  useEffect(() => { if (follow && live) setCursor(last) }, [follow, live, last])
  // one timer, cleared on every step, on pause and on unmount
  useEffect(() => {
    if (!playing) return
    if (cursor >= last) { setPlaying(false); return }
    const timer = setTimeout(() => setCursor(cursor + 1), STEP_MS)
    return () => clearTimeout(timer)
  }, [playing, cursor, last])
  useEffect(() => { cards.current[cursor]?.scrollIntoView?.({ block: 'nearest' }) }, [cursor])

  const at = Math.min(cursor, last)
  const jump = (i: number) => { setFollow(false); setCursor(i) }
  const toggle = () => {
    setFollow(false)
    if (playing) return setPlaying(false)
    if (at >= last) setCursor(0)
    setPlaying(true)
  }
  const step = steps[at]
  const time = hhmm(step.at)
  const finished = at >= last && !playing
  const stopped = waiting && at === last
  const clock = stopped ? 'waiting on you' : live && at === last ? 'in progress' : 'replayed from the record'

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
        <Segments n={steps.length} cursor={at} playing={playing} onJump={jump} labels={steps.map((s, i) => `Step ${i + 1}: ${titled(s.action)}`)} />
        <span className="text-[12px] font-semibold text-[var(--tx1)] whitespace-nowrap">{stopped ? 'Stopped · needs you' : `Step ${at + 1} of ${steps.length} · ${titled(step.action)}`}</span>
        <span className="text-[12px] text-[var(--tx2)] whitespace-nowrap">{time ? `${time} · ${clock}` : clock}</span>
      </div>
      <div className="grid grid-cols-[minmax(0,1.25fr)_minmax(0,1.1fr)_minmax(0,0.8fr)] gap-3.5 items-start">
        <div className="flex flex-col gap-2 min-w-0">
          <Heading>What the lead agent did</Heading>
          {note && <span className="text-[12px] text-[var(--tx2)]">{note}</span>}
          {steps.map((s, i) => (
            <StepCard
              key={s.key} step={s} i={i} mark={markOf(i, at, s, i === last, live, waiting)} selected={i === at}
              open={Boolean(opened[s.key])} onPick={() => jump(i)}
              onToggle={() => setOpened((o) => ({ ...o, [s.key]: !o[s.key] }))}
              cardRef={(el) => { cards.current[i] = el }}
            />
          ))}
        </div>
        {panels(at)}
      </div>
    </>
  )
}

function OneLine({ children }: { children: React.ReactNode }) {
  return <div className="text-[12px] leading-[1.45] text-[var(--tx2)] px-3.5 py-2.5 rounded-[12px] bg-[var(--bg1)] border border-[var(--ln0)]">{children}</div>
}

function HuntReplay({ d, hunt, live, waiting }: { d: WfRunDetail; hunt: HuntView; live: boolean; waiting: boolean }) {
  const steps = huntSteps(hunt)
  if (steps.length === 0) return <OneLine>{live ? 'No steps yet. The first one shows here when the lead makes it.' : 'No steps were recorded for this run.'}</OneLine>
  // moves are capped at the newest few; a shorter list than the iteration count is a prefix cut off
  const cut = steps.length < hunt.iteration
  const panels = (at: number) => (
    <HuntPanels runId={d.run_id} hunt={hunt} iteration={steps[at].iteration} decisionId={steps[at].key} last={at === steps.length - 1} />
  )
  return <Replay key={d.run_id} steps={steps} live={live} waiting={waiting} note={cut ? `Showing the last ${steps.length} steps` : null} panels={panels} />
}

function InvestigateReplay({ d, live, waiting }: { d: WfRunDetail; live: boolean; waiting: boolean }) {
  const read = useInvestigateReplay(d.run_id, live)
  // a failed poll must not tear down a player that already has steps
  const held = useRef<InvestigateDecisionView[] | null>(null)
  if (read.kind === 'investigate') held.current = read.decisions
  if (read.kind === 'failed' && held.current) return <InvestigateSteps decisions={held.current} live={live} waiting={waiting} d={d} />
  if (read.kind === 'pending') return <OneLine>Loading steps…</OneLine>
  if (read.kind === 'absent' || read.kind === 'root_cause') return <OneLine>{UNSUPPORTED}</OneLine>
  if (read.kind === 'failed') return <OneLine>Couldn’t read the steps — {read.message}</OneLine>
  return <InvestigateSteps decisions={read.decisions} live={live} waiting={waiting} d={d} />
}

function InvestigateSteps({ decisions, live, waiting, d }: { decisions: InvestigateDecisionView[]; live: boolean; waiting: boolean; d: WfRunDetail }) {
  if (decisions.length === 0) return <OneLine>{live ? 'No steps yet. The first one shows here when the lead makes it.' : 'No steps were recorded for this run.'}</OneLine>
  const costs = decisions.map((x) => x.cost_usd)
  return <Replay key={d.run_id} steps={investigateSteps(decisions)} live={live} waiting={waiting} note={null} panels={(at) => <InvestigatePanels d={d} costs={costs} at={at} />} />
}

function RootCauseReplay({ d, live, waiting }: { d: WfRunDetail; live: boolean; waiting: boolean }) {
  const read = useInvestigateReplay(d.run_id, live)
  const held = useRef<Extract<typeof read, { kind: 'root_cause' }> | null>(null)
  if (read.kind === 'root_cause') held.current = read
  const shown = read.kind === 'root_cause' ? read : read.kind === 'failed' ? held.current : null
  if (shown) return <RootCauseSteps entries={shown.entries} budgets={shown.budgets} live={live} waiting={waiting} d={d} />
  if (read.kind === 'pending') return <OneLine>Loading steps…</OneLine>
  // an agent service that cannot replay this run still gets the run as it stands
  return (
    <>
      <OneLine>{read.kind === 'failed' ? `Couldn’t read the steps — ${read.message}` : UNSUPPORTED}</OneLine>
      <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-3.5 items-start"><OtherPanels d={d} /></div>
    </>
  )
}

function RootCauseSteps({ entries, budgets, live, waiting, d }: { entries: RootCauseEntry[]; budgets: RootCauseBudgets; live: boolean; waiting: boolean; d: WfRunDetail }) {
  if (entries.length === 0) return <OneLine>{live ? 'No steps yet. The first one shows here when the lead makes it.' : 'No steps were recorded for this run.'}</OneLine>
  return <Replay key={d.run_id} steps={rootCauseSteps(entries)} live={live} waiting={waiting} note={null} panels={(at) => <RootCausePanels d={d} entries={entries} budgets={budgets} at={at} />} />
}

function versionText(d: WfRunDetail): string {
  const v = d.workflow_version
  return typeof v === 'number' ? `version ${v}` : 'version not recorded'
}

function Header({ d, replayed, onBack }: { d: WfRunDetail; replayed: boolean; onBack: () => void }) {
  const name = d.workflow_name
  const caseId = d.hunt?.scope?.case_id ?? d.trigger_context?.case_id
  const about = [typeof caseId === 'string' && caseId ? `case ${caseId}` : null, d.hunt?.name].filter(Boolean).join(': ')
  const workflow = name ? `${name} ${typeof d.workflow_version === 'number' ? versionText(d) : `(${versionText(d)})`}` : versionText(d)
  const lead = `${workflow}${about ? ` · ${about}` : ''}`
  const subtitle = ['Run ', <span key="id" className="font-mono" title={d.run_id}>{d.run_id.slice(0, 8)}</span>, ` · ${lead}${replayed ? `${/[.?!]$/.test(lead) ? ' ' : '. '}Replayed step by step from the record.` : ''}`]
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
          Watch it run{name ? ` · ${name}` : ''}
        </h2>
        <span className="text-[12px] leading-[1.45] text-[var(--tx2)]">{subtitle}</span>
      </div>
    </div>
  )
}

/** `d` is the run as getRun last saw it; RunView keeps it fresh while the run is in flight. */
export function WatchRun({ d, onBack }: { d: WfRunDetail; onBack: () => void }) {
  const live = IN_FLIGHT.includes(d.status)
  // parked on a person: stopped, not working, though the run stays in flight and keeps polling
  const waiting = d.status === 'paused' || Boolean(d.hunt?.open_checkpoint)
  const runKind = (d.projection as { run_kind?: unknown } | null | undefined)?.run_kind
  const kind = d.hunt ? 'hunt' : runKind === 'investigate' ? 'investigate' : runKind === 'root_cause' ? 'root_cause' : 'playbook'
  return (
    <div className="flex flex-col gap-3.5 px-[22px] py-5 pb-[110px]">
      <Header d={d} replayed={kind !== 'playbook'} onBack={onBack} />
      {kind === 'hunt' && d.hunt && <HuntReplay d={d} hunt={d.hunt} live={live} waiting={waiting} />}
      {kind === 'investigate' && <InvestigateReplay d={d} live={live} waiting={waiting} />}
      {kind === 'root_cause' && <RootCauseReplay d={d} live={live} waiting={waiting} />}
      {kind === 'playbook' && (
        <>
          <OneLine>{PLAYBOOK}</OneLine>
          <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-3.5 items-start"><OtherPanels d={d} /></div>
        </>
      )}
    </div>
  )
}
