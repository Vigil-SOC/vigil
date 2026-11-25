/* Watch a run: one run replayed step by step from its record. The page reads the run; it asks
   for nothing new except the investigate replay RunDetail already reads. */
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { Icon } from '../../shared/icons'
import { stopCauseOf } from '../../shared/stopCause'
import { StopLine } from '../../shared/StopLine'
import { utcClock as hhmm } from '../../shared/utc'
import {
  IN_FLIGHT, moveWord, useInvestigateReplay,
  type HuntView, type InvestigateDecisionView, type RootCauseBudgets, type RootCauseEntry, type WfRunDetail,
} from './runRead'
import { Heading, HuntPanels, InvestigatePanels, OtherPanels, PlaybookReport, RootCausePanels } from './WatchPanels'
import {
  StepCard, haltCaption, haltOf, huntSteps, investigateSteps, markOf, rootCauseSteps, runKindOf, type Halt, type Step,
} from './runSteps'

export const STEP_MS = 1400
export const UNSUPPORTED = 'Replay isn’t available for this kind of run yet.'
export const AGENT_RUN = 'One agent works this on its own, with its own tools. Its report appears here when it finishes.'
export const PLAYBOOK = 'A playbook runs its phases in order, with no lead agent to watch. Its phases are listed in History.'


/** `threat-hunt` → "Threat hunt": a slug is read as words; a display name with spaces is shown as written. */
const workflowTitle = (name: string) => {
  const words = /\s/.test(name.trim()) ? name.trim() : name.replace(/[_-]+/g, ' ').trim()
  return words.charAt(0).toUpperCase() + words.slice(1)
}
const HALT_CLOCK: Record<Halt['kind'], string> = { needs_you: 'waiting on you', paused: 'paused', failed: 'stopped', cancelled: 'stopped', rejected: 'rejected' }

// A step at full size and the gap after it, and a folded stretch's chip: what the bar spends on each.
const STEP_PX = 16
const FOLD_PX = 72
// Before the bar has been measured, and where nothing measures it.
const ASSUMED_PX = 960

export type BarItem = { kind: 'step'; i: number } | { kind: 'fold'; from: number; to: number }

/** What the bar draws: every step when they fit; else the first and last, a window around the
 *  cursor sized to the room left, and each stretch between folded into one count. */
export function foldSteps(n: number, cursor: number, width: number): BarItem[] {
  const all = (from: number, to: number): BarItem[] => Array.from({ length: to - from + 1 }, (_, k) => ({ kind: 'step', i: from + k }))
  if (n * STEP_PX <= width) return all(0, n - 1)
  const size = Math.max(9, Math.floor((width - 2 * (FOLD_PX + STEP_PX)) / STEP_PX))
  let lo = Math.max(0, cursor - Math.floor(size / 2))
  const hi = Math.min(n - 1, lo + size - 1)
  lo = Math.max(0, hi - size + 1)
  // A stretch of one step is drawn as that step: a chip counting one would say nothing.
  const stretch = (from: number, to: number): BarItem[] => (from === to ? all(from, to) : [{ kind: 'fold', from, to }])
  const items: BarItem[] = []
  if (lo > 0) items.push({ kind: 'step', i: 0 })
  if (lo > 1) items.push(...stretch(1, lo - 1))
  items.push(...all(lo, hi))
  if (hi < n - 2) items.push(...stretch(hi + 1, n - 2))
  if (hi < n - 1) items.push({ kind: 'step', i: n - 1 })
  return items
}

function useWidth(ref: React.RefObject<HTMLElement | null>): number {
  const [width, setWidth] = useState(ASSUMED_PX)
  useEffect(() => {
    const el = ref.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const watch = new ResizeObserver(([entry]) => { if (entry && entry.contentRect.width > 0) setWidth(entry.contentRect.width) })
    watch.observe(el)
    return () => watch.disconnect()
  }, [ref])
  return width
}

function Segments({ n, cursor, playing, ended, onJump, labels }: { n: number; cursor: number; playing: boolean; ended: boolean; onJump: (i: number) => void; labels: string[] }) {
  const bar = useRef<HTMLDivElement>(null)
  const items = foldSteps(n, cursor, useWidth(bar))
  return (
    <div ref={bar} role="list" aria-label="Steps" className="grow min-w-0 flex items-center gap-1.5 overflow-hidden">
      {items.map((item) => {
        if (item.kind === 'fold') {
          const count = item.to - item.from + 1
          const middle = Math.floor((item.from + item.to) / 2)
          return (
            <button
              key={`fold-${item.from}`} type="button"
              title={`Steps ${item.from + 1}–${item.to + 1}`}
              aria-label={`Steps ${item.from + 1} to ${item.to + 1}, ${count} folded. Open at step ${middle + 1}`}
              onClick={() => onJump(middle)}
              className="shrink-0 h-[20px] px-[9px] rounded-full border border-[var(--ln1)] bg-[var(--bg2)] text-[var(--tx2)] hover:text-[var(--tx0)] hover:border-[var(--ln2)] font-mono text-[11px] leading-[18px] tabular-nums cursor-pointer"
            >
              {`··· ${count}`}
            </button>
          )
        }
        const i = item.i
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
function Replay({ steps, total, live, halt, note, panels }: { steps: Step[]; total?: number; live: boolean; halt: Halt | null; note: string | null; panels: (at: number) => ReactNode }) {
  const last = steps.length - 1
  const numberOf = (i: number) => steps[i]?.number ?? i + 1
  const of = Math.max(total ?? steps.length, numberOf(last))
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
        <Segments n={steps.length} cursor={at} playing={playing} ended={!live} onJump={jump} labels={steps.map((s, i) => `Step ${numberOf(i)}: ${moveWord(s.action)}`)} />
        {stopped
          ? <span className="min-w-0 max-w-[45%] truncate text-[12px] font-semibold text-[var(--tx1)]" title={haltCaption(halt)}>{haltCaption(halt)}</span>
          : <span className="text-[12px] font-semibold text-[var(--tx1)] whitespace-nowrap">{`Step ${numberOf(at)} of ${of} · ${moveWord(step.action)}`}</span>}
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

export function OneLine({ children }: { children: React.ReactNode }) {
  return <div className="text-[12px] leading-[1.45] text-[var(--tx2)] px-3.5 py-2.5 rounded-[12px] bg-[var(--bg1)] border border-[var(--ln0)]">{children}</div>
}

/** The one line where the player would be, with why the run stopped when it did. */
export function NoSteps({ live, halt }: { live: boolean; halt: Halt | null }) {
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
    <HuntPanels runId={d.run_id} hunt={hunt} iteration={steps[at].iteration} at={steps[at].at} decisionId={steps[at].key} last={at === steps.length - 1} />
  )
  return <Replay key={d.run_id} steps={steps} total={hunt.iteration} live={live} halt={halt} note={cut ? `Showing the last ${steps.length} steps` : null} panels={panels} />
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

/** The operator manifest a run ran under: the run event's spec names its plugin, which for an operator agent is
 *  the manifest's id (agent-serve stamps it there, and the projection reads it back as kernel.plugin). */
function manifestIdOf(d: WfRunDetail): string | null {
  const plugin = (d.projection as { kernel?: { plugin?: unknown } } | null | undefined)?.kernel?.plugin
  return typeof plugin === 'string' && plugin.startsWith('custom-') ? plugin : null
}

function Header({ d, hypothesis, tail, onBack }: { d: WfRunDetail; hypothesis: string | null; tail: string | null; onBack: () => void }) {
  const name = d.workflow_name
  const caseId = d.hunt?.scope?.case_id ?? d.trigger_context?.case_id
  // a hunt with no name of its own carries the workflow id as its name; the heading already says which workflow
  const huntName = d.hunt?.name && d.hunt.name !== name && d.hunt.name !== d.workflow_id ? d.hunt.name : null
  const about = [typeof caseId === 'string' && caseId ? `case ${caseId}` : null, huntName].filter(Boolean).join(': ')
  const workflow = name ? `${workflowTitle(name)} ${typeof d.workflow_version === 'number' ? versionText(d) : `(${versionText(d)})`}` : versionText(d)
  const lead = `${workflow}${about ? ` · ${about}` : ''}`
  const manifest = manifestIdOf(d)
  const subtitle = [
    'Run ', <span key="id" className="font-mono" title={d.run_id}>{d.run_id.slice(0, 8)}</span>,
    ...(manifest ? [' · manifest ', <span key="manifest" className="font-mono">{manifest}</span>] : []),
    ` · ${lead}${tail ? `${/[.?!]$/.test(lead) ? ' ' : '. '}${tail}` : ''}`,
  ]
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

/** A playbook run has no replay to raise this in, so its wait for a person is said here. The decision is on the case. */
export function WaitingOnPerson({ d }: { d: WfRunDetail }) {
  const open = (d.projection as { open_checkpoint?: { question?: string } | null } | null | undefined)?.open_checkpoint
  if (!open) return null
  const caseId = d.trigger_context?.case_id
  return (
    <OneLine>
      Stopped · needs you. {open.question}{' '}
      {typeof caseId === 'string' && caseId && <Link className="underline" to={`/cases?case=${encodeURIComponent(caseId)}`}>Decide on the case</Link>}
    </OneLine>
  )
}

/** `d` is the run as getRun last saw it; RunView keeps it fresh while the run is in flight. */
export function WatchRun({ d, onBack }: { d: WfRunDetail; onBack: () => void }) {
  const live = IN_FLIGHT.includes(d.status)
  const halt = haltOf(d)
  const kind = runKindOf(d)
  const tail = kind === 'playbook' ? null : live && !halt ? 'Live.' : 'Replayed step by step from the record.'
  const cause = stopCauseOf(d.stop_cause)
  return (
    <div className="flex flex-col gap-3.5 px-[22px] pt-5 pb-[110px]">
      <Header d={d} hypothesis={kind === 'hunt' ? hypothesisOf(d) : null} tail={tail} onBack={onBack} />
      {cause && <StopLine cause={cause} raw={d.error || d.reason} />}
      {kind === 'hunt' && <HuntReplay d={d} hunt={d.hunt ?? null} live={live} halt={halt} />}
      {kind === 'investigate' && <InvestigateReplay d={d} live={live} halt={halt} />}
      {kind === 'root_cause' && <RootCauseReplay d={d} live={live} halt={halt} />}
      {kind === 'playbook' && (
        <>
          <OneLine>{d.workflow_id?.startsWith('agent-run-') ? AGENT_RUN : PLAYBOOK}</OneLine>
          <WaitingOnPerson d={d} />
          {halt?.kind === 'rejected' && !cause && <OneLine>{haltCaption(halt)}</OneLine>}
          <PlaybookReport d={d} />
          <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-3.5 items-start"><OtherPanels d={d} /></div>
        </>
      )}
    </div>
  )
}
