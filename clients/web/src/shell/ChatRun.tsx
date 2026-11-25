/* The run on the dock's case, followed live in one column. Everything comes from the run's record
   (useRunDetail, and the replay Watch run reads); the step cards and plan panels are Watch run's own. */
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { approvalsApi, casesApi } from '../services/api'
import type { Schema } from '../services/apiTypes'
import { Icon } from '../shared/icons'
import { StopLine } from '../shared/StopLine'
import { stopCauseOf } from '../shared/stopCause'
import { caseLabel } from '../shared/caseLabel'
import { utcClock } from '../shared/utc'
import { HuntDetections } from '../screens/workflows/HuntDetections'
import { OpenCheckpoint } from '../screens/workflows/huntParts'
import {
  IN_FLIGHT, PHASE_WORD, errMsg, moveWord, runStatusColor, shownStatus, useInvestigateReplay, useRunDetail, verdictOf,
  type HuntView, type WfRunDetail,
} from '../screens/workflows/runRead'
import {
  StepCard, haltOf, huntSteps, investigateSteps, markOf, rootCauseSteps, runKindOf, type Halt, type RunKind, type Step,
} from '../screens/workflows/runSteps'
import { HuntExplanations, Heading, Limit, PlaybookReport, Prose, Spots, huntRows, huntSpots } from '../screens/workflows/WatchPanels'
import { AGENT_RUN, NoSteps, OneLine, PLAYBOOK, WaitingOnPerson } from '../screens/workflows/WatchRun'
import { Steer, useHuntDetections } from '../screens/workflows/WorkflowsScreen'

type CaseRun = Schema<'CaseInvestigationRef'>
type CaseRead = { kind: 'loading' } | { kind: 'refused'; message: string } | { kind: 'ready'; runs: CaseRun[] }

const KIND_WORD: Record<RunKind, string> = { hunt: 'Threat hunt', investigate: 'Investigation', root_cause: 'Root cause', playbook: 'Run' }
const CASE_POLL_MS = 15_000
const runHref = (runId: string) => `/workflows?run=${encodeURIComponent(runId)}`
const caseHref = (caseId: string) => `/cases?case=${encodeURIComponent(caseId)}`
const LINK = 'text-[12px] font-semibold text-[var(--ac)] hover:underline'
const statusWord = (status: string) => PHASE_WORD[status] ?? moveWord(status)

/** The case's runs, newest first, as /replay finds them; re-read so a run started elsewhere shows up. */
function useCaseRuns(caseId: string, refresh: number): CaseRead {
  const [read, setRead] = useState<CaseRead>({ kind: 'loading' })
  // a new case starts blank; a refresh of the same one keeps the block, and what is typed in it, on screen
  useEffect(() => { setRead({ kind: 'loading' }) }, [caseId])
  useEffect(() => {
    let live = true
    const pull = () => casesApi.getById(caseId)
      .then((res) => { if (live) setRead({ kind: 'ready', runs: res.data.investigations ?? [] }) })
      // a case this person cannot read shows no run, whatever the conversation says
      .catch((e: unknown) => { if (live) setRead({ kind: 'refused', message: errMsg(e) }) })
    void pull()
    const timer = setInterval(() => { void pull() }, CASE_POLL_MS)
    return () => { live = false; clearInterval(timer) }
  }, [caseId, refresh])
  return read
}

/** The block under the conversation: the case's newest run, and its other runs as one line each. */
export function CaseRunBlock({ caseId, refresh }: { caseId: string; refresh: number }) {
  const read = useCaseRuns(caseId, refresh)
  const [folded, setFolded] = useState(false)
  if (read.kind === 'loading') return null
  if (read.kind === 'refused') return <div className="chat-run" role="note"><OneLine>{caseLabel(caseId)} can’t be read: {read.message}</OneLine></div>
  const [newest, ...older] = read.runs
  if (!newest) return null
  return (
    <section className="chat-run flex flex-col gap-2.5 min-w-0" aria-label="Run on this case">
      <RunBlock key={newest.run_id} runId={newest.run_id} caseId={caseId} folded={folded} onFold={() => setFolded(!folded)} />
      {!folded && older.length > 0 && (
        <div className="flex flex-col gap-1 min-w-0">
          <Heading>Earlier runs on this case</Heading>
          {older.map((r) => (
            <Link key={r.run_id} to={runHref(r.run_id)} className="flex items-baseline gap-2 min-w-0 text-[12px] text-[var(--tx1)] hover:text-[var(--tx0)]">
              <span className="truncate min-w-0">{r.workflow_name || r.workflow_id}</span>
              <span className="shrink-0 text-[var(--tx2)]">{statusWord(r.status)}</span>
              {r.created_at && <span className="ml-auto shrink-0 font-mono text-[11px] text-[var(--tx3)]">{utcClock(r.created_at)}</span>}
            </Link>
          ))}
        </div>
      )}
    </section>
  )
}

function RunBlock({ runId, caseId, folded, onFold }: { runId: string; caseId: string; folded: boolean; onFold: () => void }) {
  const { detail: d, dphase, load } = useRunDetail(runId, true)
  useEffect(() => { void load() }, [load])
  if (!d) return dphase === 'error' ? <OneLine>Couldn’t read the run on this case.</OneLine> : null
  const live = IN_FLIGHT.includes(d.status)
  const kind = runKindOf(d)
  const status = shownStatus(d)
  return (
    <div className="flex flex-col gap-2.5 min-w-0 px-3 py-2.5 rounded-[12px] bg-[var(--bg1)] border border-[var(--ln0)]">
      <div className="flex items-center gap-2 min-w-0">
        <button
          type="button" aria-expanded={!folded} onClick={onFold}
          className="inline-flex items-center gap-1.5 min-w-0 p-0 bg-transparent text-left cursor-pointer"
        >
          <span className="inline-flex shrink-0 text-[var(--tx3)] transition-transform" style={{ transform: folded ? 'none' : 'rotate(90deg)' }}><Icon name="chevR" size={12} /></span>
          <span className="text-[13px] font-[650] text-[var(--tx0)] truncate">{KIND_WORD[kind]}</span>
        </button>
        <span className="shrink-0 text-[12px] font-semibold" style={{ color: runStatusColor(status) }}>{statusWord(status)}{live && status === 'running' ? ' · live' : ''}</span>
        <span className="ml-auto shrink-0 font-mono text-[11px] text-[var(--tx3)]" title={d.run_id}>{d.run_id.slice(0, 8)}</span>
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-1">
        <Link className={LINK} to={runHref(d.run_id)}>Watch run</Link>
        <Link className={LINK} to={caseHref(caseId)}>{caseLabel(caseId)}</Link>
        {kind === 'hunt' && <Link className={LINK} to="/detections">Detections</Link>}
      </div>
      {!folded && <RunBody d={d} kind={kind} live={live} caseId={caseId} reload={() => void load()} />}
    </div>
  )
}

function RunBody({ d, kind, live, caseId, reload }: { d: WfRunDetail; kind: RunKind; live: boolean; caseId: string; reload: () => void }) {
  const halt = haltOf(d)
  const cause = stopCauseOf(d.stop_cause)
  return (
    <>
      {cause && <StopLine cause={cause} raw={d.error || d.reason} />}
      {kind === 'hunt' && <HuntBody d={d} hunt={d.hunt ?? null} live={live} halt={halt} />}
      {kind === 'investigate' && <ReplayBody d={d} live={live} halt={halt} kind="investigate" />}
      {kind === 'root_cause' && <ReplayBody d={d} live={live} halt={halt} kind="root_cause" />}
      {kind === 'playbook' && (
        <>
          <OneLine>{d.workflow_id?.startsWith('agent-run-') ? AGENT_RUN : PLAYBOOK}</OneLine>
          <WaitingOnPerson d={d} />
          <PlaybookReport d={d} plain />
        </>
      )}
      {d.hunt?.open_checkpoint && <OpenCheckpoint hunt={{ ...d.hunt, run_id: d.hunt.run_id ?? d.run_id }} />}
      {/* a hunt's checkpoint is also a pending approval; the card above already answers it */}
      {d.status === 'paused' && !d.hunt?.open_checkpoint && <PendingApprovals runId={d.run_id} caseId={caseId} />}
      {!live && <Verdict d={d} />}
      {kind === 'hunt' && d.hunt && <Detections runId={d.run_id} hunt={d.hunt} concluded={!live} />}
      {live && <div className="chat-run-steer"><Steer runId={d.run_id} hunt={kind === 'hunt'} onSteered={reload} /></div>}
    </>
  )
}

function Part({ title, children }: { title: string; children: ReactNode }) {
  return <div className="flex flex-col gap-1.5 min-w-0"><Heading>{title}</Heading>{children}</div>
}

/** The plan at the newest step: the explanations, the limits and the blind spots, as Watch run's panels read them. */
function HuntBody({ d, hunt, live, halt }: { d: WfRunDetail; hunt: HuntView | null; live: boolean; halt: Halt | null }) {
  if (!hunt) return <OneLine>Starting…</OneLine>
  const steps = huntSteps(hunt)
  const iteration = steps[steps.length - 1]?.iteration ?? hunt.iteration
  const spots = huntSpots(hunt, iteration, null, true)
  return (
    <>
      <Part title="Explanations being tested"><HuntExplanations hunt={hunt} iteration={iteration} at={null} last recorded={undefined} /></Part>
      <Part title="Limits">{huntRows(hunt, iteration, true, undefined).map((r) => <Limit key={r.label} {...r} />)}</Part>
      {spots.length > 0 && <Part title="Blind spots hit"><Spots lines={spots} empty="" /></Part>}
      {steps.length === 0
        ? <NoSteps live={live} halt={halt} />
        : <StepList key={d.run_id} steps={steps} live={live} halt={halt} note={steps.length < hunt.iteration ? `Showing the last ${steps.length} steps` : null} />}
    </>
  )
}

function ReplayBody({ d, live, halt, kind }: { d: WfRunDetail; live: boolean; halt: Halt | null; kind: 'investigate' | 'root_cause' }) {
  const read = useInvestigateReplay(d.run_id, live)
  // a failed poll keeps the steps already on screen
  const held = useRef<Step[] | null>(null)
  if (read.kind === 'investigate' && kind === 'investigate') held.current = investigateSteps(read.decisions)
  if (read.kind === 'root_cause' && kind === 'root_cause') held.current = rootCauseSteps(read.entries)
  const steps = held.current
  if (steps) return steps.length === 0 ? <NoSteps live={live} halt={halt} /> : <StepList key={d.run_id} steps={steps} live={live} halt={halt} note={null} />
  if (read.kind === 'pending') return <OneLine>Loading steps…</OneLine>
  return <OneLine>{read.kind === 'failed' ? `Couldn’t read the steps — ${read.message}` : 'Replay isn’t available for this kind of run yet.'}</OneLine>
}

/** Oldest first, following the newest step while the run works, unless the reader scrolled back. */
function StepList({ steps, live, halt, note }: { steps: Step[]; live: boolean; halt: Halt | null; note: string | null }) {
  const last = steps.length - 1
  const [opened, setOpened] = useState<Record<string, boolean>>({})
  const box = useRef<HTMLDivElement>(null)
  const following = useRef(true)
  // every render: a card's text and its "Show all" settle after the step arrives
  useEffect(() => {
    const el = box.current
    if (el && following.current) el.scrollTop = el.scrollHeight
  })
  const toggle = (key: string) => setOpened((o) => ({ ...o, [key]: !o[key] }))
  const failed = halt?.kind === 'failed' || halt?.kind === 'cancelled'
  return (
    <Part title={`Steps · ${steps.length}`}>
      {note && <span className="text-[11px] text-[var(--tx2)]">{note}</span>}
      <div
        ref={box}
        onScroll={(e) => { const el = e.currentTarget; following.current = el.scrollTop + el.clientHeight >= el.scrollHeight - 8 }}
        className="flex flex-col gap-1.5 overflow-y-auto max-h-[360px] min-w-0"
      >
        {steps.map((s, i) => (
          <StepCard
            key={s.key} step={s} i={i} mark={markOf(i, last, s, i === last, live, halt)}
            error={i === last && failed ? halt.reason || null : null} selected={false}
            open={Boolean(opened[s.key])} onPick={() => toggle(s.key)} onToggle={() => toggle(s.key)} cardRef={() => {}}
          />
        ))}
      </div>
    </Part>
  )
}

/** What a paused run waits on. Approving happens where Home's Needs your attention sends a person: the case. */
function PendingApprovals({ runId, caseId }: { runId: string; caseId: string }) {
  const [actions, setActions] = useState<{ action_id?: string | null; title?: string | null; restricted?: boolean }[] | null>(null)
  useEffect(() => {
    let live = true
    approvalsApi.list({ status: 'pending', workflow_run_id: runId })
      .then((res) => { if (live) setActions(res.data.actions ?? []) })
      .catch(() => { if (live) setActions(null) })
    return () => { live = false }
  }, [runId])
  if (!actions?.length) return null
  return (
    <div className="flex flex-col gap-1.5 p-3 rounded-[12px] border" style={{ background: 'var(--poor-bg)', borderColor: 'var(--poor-ln)' }}>
      <span className="text-[12px] font-bold text-[var(--poor)]">Waiting on an approval</span>
      {actions.map((a, i) => (
        <span key={a.action_id ?? i} className="text-[12px] leading-[1.45] text-[var(--tx0)] break-words">
          {a.restricted ? 'An action only the case’s readers can see' : a.title || a.action_id}
        </span>
      ))}
      <span className="flex flex-wrap gap-x-3 gap-y-1">
        <Link className={LINK} to={caseHref(caseId)}>Decide on the case</Link>
        <Link className={LINK} to="/home">Needs your attention</Link>
      </span>
    </div>
  )
}

const rec = (v: unknown): Record<string, unknown> => (typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Record<string, unknown>) : {})
const strs = (v: unknown): string[] => (Array.isArray(v) ? v.filter((s): s is string => typeof s === 'string' && s.trim() !== '') : [])

/** The fields get_run reads off an ended run (tools/mcp/vigil.py): conclusion, disposition, citations, declared gaps. */
function verdictRead(d: WfRunDetail) {
  const folded = rec(d.hunt ?? d.projection)
  // plugin views nest the kernel fold; compose returns it bare
  const kernel = rec(folded.kernel ?? folded)
  const conclusion = rec(kernel.conclusion)
  const value = rec(conclusion.value)
  const said = [kernel.summary, value.summary, value.text, value.note].find((s): s is string => typeof s === 'string' && s.trim() !== '')
  const fallback = verdictOf(d, null)
  const disposition = [folded.disposition, value.disposition].find((s): s is string => typeof s === 'string' && s !== '')
  const gaps = rec(conclusion.declared_gaps)
  const visibility = (Array.isArray(gaps.visibility) ? gaps.visibility : []).map(rec)
    .map((v) => ({ main: `${typeof v.tool === 'string' ? v.tool : 'A call'}${typeof v.failure === 'string' ? ` · ${v.failure}` : ''}`, sub: typeof v.ref === 'string' ? [v.ref] : [] }))
  return {
    // a hunt's kernel summary is its whole report; its account's summary is the sentence
    conclusion: (d.hunt ? fallback.headline || said : said || fallback.headline) || d.error || null,
    disposition: disposition ? moveWord(disposition) : null,
    citations: strs(value.citations),
    gaps: [
      ...strs(gaps.declared).map((main) => ({ main, sub: [] as string[] })),
      ...strs(gaps.open_questions).map((main) => ({ main, sub: ['open question'] })),
      ...visibility,
    ],
  }
}

function Verdict({ d }: { d: WfRunDetail }) {
  const v = verdictRead(d)
  if (!v.conclusion && !v.disposition && v.citations.length === 0 && v.gaps.length === 0) return null
  return (
    <Part title="Verdict · model text">
      <div className="flex flex-col gap-1.5 min-w-0 px-3 py-2.5 rounded-[12px] bg-[var(--bg2)] border border-[var(--ln0)] text-[12.5px] leading-[1.5] text-[var(--tx0)]">
        {v.disposition && <span className="text-[12px] font-bold">{v.disposition}</span>}
        {v.conclusion && <Prose text={v.conclusion} />}
        {v.citations.length > 0 && (
          <span className="flex flex-wrap items-baseline gap-1 text-[11px] text-[var(--tx2)]">
            Cites
            {v.citations.map((c) => <span key={c} className="font-mono px-1.5 rounded-[6px] bg-[var(--bg3)] text-[var(--tx1)] break-all">{c}</span>)}
          </span>
        )}
      </div>
      {v.gaps.length > 0 && <><Heading>Declared gaps</Heading><Spots lines={v.gaps} empty="" /></>}
    </Part>
  )
}

/** What the concluded hunt led to for detection, when it led to anything. */
function Detections({ runId, hunt, concluded }: { runId: string; hunt: HuntView; concluded: boolean }) {
  const read = useHuntDetections(runId, concluded)
  if (!read || (read.proposals.length === 0 && read.without.length === 0)) return null
  const statement = (id: string) => hunt.hypotheses.find((h) => h.hypothesis_id === id)?.statement ?? id
  return (
    <Part title="Detection follow-through">
      <div className="chat-run-detections min-w-0"><HuntDetections read={read} hyp={statement} /></div>
    </Part>
  )
}
