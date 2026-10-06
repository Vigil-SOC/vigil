/* Watch a run (PRD AW-W5): one run replayed step by step from its record. Board:
   docs/design/console/boards/WorkflowRun.dc.html. The page reads the run; it asks
   for nothing new except the investigate replay RunDetail already reads. */
import { useEffect, useRef, useState } from 'react'
import { Icon } from '../../shared/icons'
import { Cost } from '../../shared/cost'
import {
  IN_FLIGHT, callFailure, callLine, fmtDuration, useInvestigateReplay,
  type CallFailure, type HuntView, type InvestigateDecisionView, type WfRunDetail,
} from './runRead'

/** What each kind of run is reduced to: one row per decision. `calls` is null when
 *  the record cannot say which calls followed (an older agent service). */
interface Step {
  key: string
  action: string
  worker: string | null
  at: string | null
  cost: number | null
  thoughtMs: number | null
  rationale: string
  calls: CallRow[] | null
}
interface CallRow {
  tool: string
  length: number | null
  durationMs: number | null
  failure: CallFailure | null
}
type Mark = 'done' | 'failed' | 'running' | 'pending'

export const STEP_MS = 1400
export const UNSUPPORTED = 'Replay isn’t available for this kind of run yet.'

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
    action: m.action,
    worker: m.worker_agent_id ?? null,
    at: m.created_at ?? null,
    cost: m.cost_usd ?? null,
    thoughtMs: m.duration_ms ?? null,
    rationale: m.rationale,
    // a hunt call's result is only a length, so a failure mark is not known here
    calls: tied
      ? calls.filter((c) => c.iteration === m.iteration)
          .map((c) => ({ tool: c.tool, length: c.result_length, durationMs: c.duration_ms ?? null, failure: null }))
      : null,
  }))
}

function investigateSteps(decisions: InvestigateDecisionView[]): Step[] {
  return decisions.map((d) => ({
    key: String(d.iteration),
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

/** No status is stored per decision, so the mark comes from where the cursor is. */
function markOf(i: number, cursor: number, step: Step, newest: boolean, live: boolean): Mark {
  if (i > cursor) return 'pending'
  if (live && newest) return 'running'
  return step.calls?.some((c) => c.failure) ? 'failed' : 'done'
}

const MARK_LABEL: Record<Mark, string> = { done: 'Done', failed: 'Failed', running: 'Working on it', pending: 'Not started' }

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
  const meta = [call.length === null ? null : `${call.length.toLocaleString()} chars`, call.durationMs === null ? null : fmtDuration(call.durationMs)]
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
        {shown && mark === 'running' && step.calls?.length === 0 && (
          <span className="text-[12px] font-semibold text-[var(--tx2)]">Working on it…</span>
        )}
        {shown && <Trace step={step} open={open} onToggle={onToggle} />}
        {shown && (
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

function Heading({ children }: { children: React.ReactNode }) {
  return <span className="text-[12px] font-semibold leading-[1.3] text-[var(--tx2)]">{children}</span>
}
function NoData({ children }: { children: React.ReactNode }) {
  return <span className="text-[12px] leading-[1.45] text-[var(--tx2)] px-3 py-2.5 rounded-[12px] bg-[var(--bg1)] border border-[var(--ln0)]">{children}</span>
}

/** The columns the sibling child fills (#1622); until then each says it has nothing to show. */
function Explanations() {
  return (
    <div className="flex flex-col gap-2 min-w-0">
      <Heading>Explanations being tested</Heading>
      <NoData>No explanations tested are shown for this run yet.</NoData>
    </div>
  )
}
function Limits() {
  return (
    <div className="flex flex-col gap-2.5 min-w-0">
      <Heading>Limits used</Heading>
      <NoData>No limits are shown for this run yet.</NoData>
      <Heading>Reviewer</Heading>
      <NoData>No reviewer is shown for this run yet.</NoData>
      <Heading>Blind spots hit</Heading>
      <NoData>No blind spots are shown for this run yet.</NoData>
    </div>
  )
}

/** The player and the three columns. Owns the one selected step the panels read. */
function Replay({ steps, live, note }: { steps: Step[]; live: boolean; note: string | null }) {
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
  const clock = live && at === last ? 'in progress' : 'replayed from the record'

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
        <span className="text-[12px] font-semibold text-[var(--tx1)] whitespace-nowrap">Step {at + 1} of {steps.length} · {titled(step.action)}</span>
        <span className="text-[12px] text-[var(--tx2)] whitespace-nowrap">{time ? `${time} · ${clock}` : clock}</span>
      </div>
      <div className="grid grid-cols-[minmax(0,1.25fr)_minmax(0,1.1fr)_minmax(0,0.8fr)] gap-3.5 items-start">
        <div className="flex flex-col gap-2 min-w-0">
          <Heading>What the lead agent did</Heading>
          {note && <span className="text-[12px] text-[var(--tx2)]">{note}</span>}
          {steps.map((s, i) => (
            <StepCard
              key={s.key} step={s} i={i} mark={markOf(i, at, s, i === last, live)} selected={i === at}
              open={Boolean(opened[s.key])} onPick={() => jump(i)}
              onToggle={() => setOpened((o) => ({ ...o, [s.key]: !o[s.key] }))}
              cardRef={(el) => { cards.current[i] = el }}
            />
          ))}
        </div>
        <Explanations />
        <Limits />
      </div>
    </>
  )
}

function OneLine({ children }: { children: React.ReactNode }) {
  return <div className="text-[12px] leading-[1.45] text-[var(--tx2)] px-3.5 py-2.5 rounded-[12px] bg-[var(--bg1)] border border-[var(--ln0)]">{children}</div>
}

function HuntReplay({ d, hunt, live }: { d: WfRunDetail; hunt: HuntView; live: boolean }) {
  const steps = huntSteps(hunt)
  if (steps.length === 0) return <OneLine>{live ? 'No steps yet. The first one shows here when the lead makes it.' : 'No steps were recorded for this run.'}</OneLine>
  // moves are capped at the newest few; a shorter list than the iteration count is a prefix cut off
  const cut = steps.length < hunt.iteration
  return <Replay key={d.run_id} steps={steps} live={live} note={cut ? `Showing the last ${steps.length} steps` : null} />
}

function InvestigateReplay({ d, live }: { d: WfRunDetail; live: boolean }) {
  const read = useInvestigateReplay(d.run_id, live)
  // a failed poll must not tear down a player that already has steps
  const held = useRef<InvestigateDecisionView[] | null>(null)
  if (read.kind === 'investigate') held.current = read.decisions
  if (read.kind === 'failed' && held.current) return <InvestigateSteps decisions={held.current} live={live} runId={d.run_id} />
  if (read.kind === 'pending') return <OneLine>Loading steps…</OneLine>
  if (read.kind === 'absent') return <OneLine>{UNSUPPORTED}</OneLine>
  if (read.kind === 'failed') return <OneLine>Couldn’t read the steps — {read.message}</OneLine>
  return <InvestigateSteps decisions={read.decisions} live={live} runId={d.run_id} />
}

function InvestigateSteps({ decisions, live, runId }: { decisions: InvestigateDecisionView[]; live: boolean; runId: string }) {
  if (decisions.length === 0) return <OneLine>{live ? 'No steps yet. The first one shows here when the lead makes it.' : 'No steps were recorded for this run.'}</OneLine>
  return <Replay key={runId} steps={investigateSteps(decisions)} live={live} note={null} />
}

function versionText(d: WfRunDetail): string {
  const v = d.workflow_version
  return typeof v === 'number' ? `version ${v}` : 'version not recorded'
}

function Header({ d, onBack }: { d: WfRunDetail; onBack: () => void }) {
  const name = d.workflow_name
  const caseId = d.hunt?.scope?.case_id ?? d.trigger_context?.case_id
  const about = [typeof caseId === 'string' && caseId ? `case ${caseId}` : null, d.hunt?.name].filter(Boolean).join(': ')
  const workflow = name ? `${name} ${typeof d.workflow_version === 'number' ? versionText(d) : `(${versionText(d)})`}` : versionText(d)
  const lead = `${workflow}${about ? ` · ${about}` : ''}`
  const subtitle = ['Run ', <span key="id" className="font-mono" title={d.run_id}>{d.run_id.slice(0, 8)}</span>, ` · ${lead}${/[.?!]$/.test(lead) ? ' ' : '. '}Replayed step by step from the record.`]
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
  const kind = d.hunt ? 'hunt' : (d.projection as { run_kind?: unknown } | null | undefined)?.run_kind === 'investigate' ? 'investigate' : 'other'
  return (
    <div className="flex flex-col gap-3.5 px-[22px] py-5 pb-[110px]">
      <Header d={d} onBack={onBack} />
      {kind === 'hunt' && d.hunt && <HuntReplay d={d} hunt={d.hunt} live={live} />}
      {kind === 'investigate' && <InvestigateReplay d={d} live={live} />}
      {kind === 'other' && (
        <>
          <OneLine>{UNSUPPORTED}</OneLine>
          <div className="max-w-[360px]"><Limits /></div>
        </>
      )}
    </div>
  )
}
