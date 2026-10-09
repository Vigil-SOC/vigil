import { Fragment, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { formatDistanceToNowStrict } from 'date-fns'
import { Icon } from '../../shared/icons'
import { Markdown } from '../../shared/Markdown'
import type { Workflow } from '../../data/appData'
import { workflowApi } from '../../services/api'
import { modelSource, useAgentMeta } from './useWorkflowsData'
import { CHECKPOINT_CLASSES, HANDOFF_ROW, LOOP_CAPTION, composeStrip, stripFor, type RoleRef, type StageDef, type Strip } from './readerStages'

/* What a workflow is, read before it runs: the definition plus the preflight
   payload, for every kind. Takes a saved workflow's row or an unsaved draft. */

interface Helper { agent: string; name: string; tools: string[]; approval_required: boolean }
interface RoleLine { name: string; tools: string[] }
interface Roles { lead: RoleLine | null; helpers: Helper[]; reviewer: RoleLine | null }
interface Permission { name: string; label?: string; changes: 'asks_you' | 'on_its_own'; bound?: boolean }
interface Budgets { max_iterations?: number; max_turns?: number; max_calls?: number; max_cost_usd?: number; max_wall_ms?: number }

/** GET /workflows/{id}/preflight. A draft has none of it but the phases, so every key may be absent. */
interface Preflight {
  roles: Roles
  roles_note: string | null
  model: string | null
  model_source: 'assignment' | 'default' | null
  skills: string[]
  skills_note: string | null
  permissions: Permission[]
  permissions_note: string | null
  budgets: Budgets
  checkpoints: Record<string, 'ask' | 'auto'>
  checkpoints_note: string | null
}

interface Phase { agent?: string; agent_id?: string; name?: string; tools?: string[]; approval_required?: boolean }

/** GET /workflows/{id}, or an unsaved draft. */
interface Definition {
  description?: string
  version?: number
  updated_at?: string
  run_kind?: string
  hunt_like?: boolean
  objectives?: string[]
  body?: string
  phases?: Phase[] | null
}

export interface DraftDefinition {
  name: string
  description?: string
  phases?: Phase[]
}

export interface ReaderActions {
  /** Shown as "All workflows" when the pane stands alone; beside the card list it has none. */
  onBack?: () => void
  onHistory: () => void
  onRun: () => void
  onEdit: () => void
  onDelete: () => void
  /** the switch changed on the server, so the list should be read again */
  onToggled: () => void
}

/** A draft's `onSave` resolves once the workflow exists and rejects with what the server said. */
export type ReaderProps = ({ wf: Workflow } & ReaderActions) | { draft: DraftDefinition; onBack?: () => void; onSave: () => Promise<unknown> }

const DRAFT_MISSING = 'Shown once the workflow is saved.'
const NOT_LOADED = 'Couldn’t load this.'
const ALWAYS_ON = 'Alerts land here when nothing else fits, so it stays on.'

function errMsg(e: unknown): string {
  const r = e as { response?: { data?: { detail?: unknown } }; message?: string }
  const detail = r?.response?.data?.detail
  // a 422's detail is a list of {loc, msg}, which would print as [object Object]
  const text = Array.isArray(detail) ? detail.map((d) => d?.msg).filter(Boolean).join('; ') : detail
  return (typeof text === 'string' && text) || r?.message || 'request failed'
}

interface Source {
  phase: 'loading' | 'ready' | 'error'
  definition: Definition
  pre: Partial<Preflight>
  /** what a missing section says about itself */
  missing: string
}

const NO_DEFINITION: Definition = {}

function draftSource(draft: DraftDefinition): Source {
  const phases = draft.phases ?? []
  const helpers = phases.map((p) => ({
    agent: p.agent_id || p.agent || '',
    name: p.name || p.agent_id || '',
    tools: p.tools ?? [],
    approval_required: !!p.approval_required,
  }))
  return {
    phase: 'ready',
    definition: { description: draft.description, run_kind: 'compose', hunt_like: false, phases },
    pre: { roles: { lead: null, helpers, reviewer: null } },
    missing: DRAFT_MISSING,
  }
}

function useSource(id: string | null, draft: DraftDefinition | null): Source {
  const [state, setState] = useState<Source>({ phase: 'loading', definition: NO_DEFINITION, pre: {}, missing: NOT_LOADED })
  useEffect(() => {
    if (!id) return
    let cancelled = false
    setState({ phase: 'loading', definition: NO_DEFINITION, pre: {}, missing: NOT_LOADED })
    Promise.allSettled([workflowApi.get(id), workflowApi.preflight(id)]).then(([def, pre]) => {
      if (cancelled) return
      if (def.status === 'rejected') return setState((s) => ({ ...s, phase: 'error' }))
      setState({
        phase: 'ready',
        definition: def.value.data as Definition,
        pre: pre.status === 'fulfilled' ? (pre.value.data as Partial<Preflight>) : {},
        missing: NOT_LOADED,
      })
    })
    return () => { cancelled = true }
  }, [id])
  return draft ? draftSource(draft) : state
}

/** A role card, with the refs a stage may name it by. */
interface Entry { key: string; kind: 'lead' | 'helper' | 'reviewer'; name: string; agent?: string; tools: string[]; refs: RoleRef[] }

function entriesOf(roles: Roles | undefined): Entry[] {
  if (!roles) return []
  return [
    ...(roles.lead ? [{ key: 'lead', kind: 'lead' as const, name: roles.lead.name, tools: roles.lead.tools, refs: ['lead' as RoleRef] }] : []),
    ...roles.helpers.map((h, i) => ({ key: `h${i}`, kind: 'helper' as const, name: h.name, agent: h.agent, tools: h.tools, refs: ['helpers' as RoleRef, i] })),
    ...(roles.reviewer ? [{ key: 'reviewer', kind: 'reviewer' as const, name: roles.reviewer.name, tools: roles.reviewer.tools, refs: ['reviewer' as RoleRef] }] : []),
  ]
}

const covered = (entries: Entry[], wanted: RoleRef[] | null) => (wanted ? entries.filter((e) => e.refs.some((r) => wanted.includes(r))) : entries)

function usd(n: number): string {
  return `$${Number.isInteger(n) ? n : n.toFixed(2)}`
}

/** The budget and step limit as sentences; a limit the payload lacks is not mentioned. */
function limitSentences(b: Budgets): string[] {
  const parts = [
    b.max_cost_usd != null && `budget (${usd(b.max_cost_usd)})`,
    b.max_iterations != null && `step limit (${b.max_iterations})`,
    b.max_turns != null && `turn limit (${b.max_turns})`,
    b.max_calls != null && `call limit (${b.max_calls})`,
  ].filter(Boolean)
  return [
    ...(parts.length ? [`The ${parts.join(' or ')} is reached`] : []),
    ...(b.max_wall_ms != null ? [`It has run for ${Math.round(b.max_wall_ms / 60000)} minutes`] : []),
  ]
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section aria-label={title} className="flex flex-col gap-2 p-3.5 rounded-xl bg-bg-2 border border-line-soft min-w-0">
      <h3 className="m-0 text-[12px] leading-[1.3] text-tx-3" style={{ fontWeight: 600 }}>{title}</h3>
      {children}
    </section>
  )
}

const Muted = ({ children }: { children: React.ReactNode }) => <p className="m-0 text-[12px] leading-[1.45] text-tx-3">{children}</p>
const SubLabel = ({ children }: { children: React.ReactNode }) => <span className="text-[12px] leading-[1.3] text-tx-3" style={{ fontWeight: 600 }}>{children}</span>

function StageCard({ n, stage, on, gates, agent, onPick }: { n: number; stage: StageDef; on: boolean; gates: string[]; agent?: Entry; onPick: () => void }) {
  const agentMeta = useAgentMeta()
  const meta = agent?.agent ? agentMeta(agent.agent) : null
  return (
    <button
      type="button"
      aria-pressed={on}
      onClick={onPick}
      className={`flex-1 basis-0 min-w-[130px] flex flex-col gap-1.5 p-3 rounded-xl border-[1.5px] text-left cursor-pointer ${on ? 'border-accent bg-accent-dim' : 'border-line bg-bg-2 hover:bg-bg-3'}`}
    >
      <span className="flex items-center gap-2">
        <span className={`w-[22px] h-[22px] shrink-0 rounded-full inline-flex items-center justify-center text-[11px] ${on ? 'bg-accent text-[var(--ac-tx)]' : 'bg-bg-3 text-tx-2'}`} style={{ fontWeight: 700 }}>{n}</span>
        <span className="text-[13px] leading-[1.25] text-tx min-w-0" style={{ fontWeight: 700 }}>{stage.title}</span>
      </span>
      {meta && (
        <span className="agent-chip self-start max-w-full">
          <span className="ad" style={{ background: meta.color }} />
          <span className="truncate">{meta.label}</span>
        </span>
      )}
      {stage.body && <span className="text-[12px] leading-[1.4] text-tx-2">{stage.body}</span>}
      {gates.map((gate) => (
        <span key={gate} className="self-start inline-flex items-center gap-1.5 min-h-5 py-0.5 px-[7px] rounded-full text-[11px] bg-[var(--fair-bg)] text-[var(--fair)]" style={{ fontWeight: 650 }}>
          <Icon name="user" size={11} />{gate}
        </span>
      ))}
    </button>
  )
}

const Arrow = () => <span aria-hidden className="flex items-center text-tx-faint shrink-0"><Icon name="arrowR" size={16} /></span>

function HowItRuns({ strip, entries, pre, selected, onSelect }: {
  strip: Strip
  entries: Entry[]
  pre: Partial<Preflight>
  selected: number | null
  onSelect: (n: number | null) => void
}) {
  const gatesOf = (stage: StageDef) => [
    ...stage.checkpoints.filter((c) => pre.checkpoints?.[c] === 'ask').map((c) => CHECKPOINT_CLASSES[c].gate),
    // a compose phase's own pause
    ...(stage.helper !== undefined && pre.roles?.helpers[stage.helper]?.approval_required ? ['Approval required'] : []),
  ]
  let next = 0
  const card = (stage: StageDef) => {
    const i = next++
    return (
      <StageCard
        key={i}
        n={i + 1}
        stage={stage}
        on={selected === i}
        gates={gatesOf(stage)}
        agent={stage.helper === undefined ? undefined : entries.find((e) => e.refs.includes(stage.helper!))}
        onPick={() => onSelect(selected === i ? null : i)}
      />
    )
  }
  const flat = strip.loop.length === 0
  return (
    <section aria-label="How it runs" className="flex flex-col gap-2.5 p-4 rounded-[14px] bg-bg-1 border border-line-soft">
      <div className="flex items-center justify-between gap-3 min-h-[34px]">
        <h2 className="m-0 text-[15px] leading-[1.35] text-tx" style={{ fontWeight: 650 }}>How it runs</h2>
        {selected === null
          ? <span className="text-[12px] text-tx-3">Click a stage to see who does it and what it may do</span>
          : <button type="button" className="btn ghost" onClick={() => onSelect(null)}>Show the whole workflow</button>}
      </div>
      {flat ? (
        <div className="flex flex-wrap items-stretch gap-2">
          {strip.before.map((stage, k) => <Fragment key={k}>{k > 0 && <Arrow />}{card(stage)}</Fragment>)}
        </div>
      ) : (
        <div className="flex items-stretch gap-2">
          {strip.before.map((stage) => card(stage))}
          <Arrow />
          <div className="flex-[2.2_1_0] min-w-0 flex flex-col gap-1.5 p-2 rounded-[14px] border-[1.5px] border-dashed border-accent-line bg-accent-dim">
            <span className="flex items-center gap-1.5 text-[11px] text-accent" style={{ fontWeight: 700 }}>
              <Icon name="refresh" size={13} />{LOOP_CAPTION}
            </span>
            <div className="flex items-stretch gap-2">{strip.loop.map((stage) => card(stage))}</div>
          </div>
          <Arrow />
          {strip.after.map((stage) => card(stage))}
        </div>
      )}
    </section>
  )
}

function ModelLine({ pre }: { pre: Partial<Preflight> }) {
  if (!pre.model) return null
  const source = modelSource({ model: pre.model, modelSource: pre.model_source ?? null, category: 'investigation' })
  return (
    <span className="flex flex-col gap-px">
      <span className="text-[13px] text-tx" style={{ fontWeight: 650 }}>{pre.model}</span>
      {source && <span className="text-[12px] leading-[1.45] text-tx-3">{source}</span>}
    </span>
  )
}

const SKILLS_SHOWN = 12

function WhoDoesIt({ entries, pre, stage, missing }: { entries: Entry[]; pre: Partial<Preflight>; stage: StageDef | null; missing: string }) {
  const agentMeta = useAgentMeta()
  const shown = covered(entries, stage?.roles ?? null)
  const skills = pre.skills ?? []
  return (
    <Panel title="Who does it">
      {!pre.roles && <Muted>{missing}</Muted>}
      {pre.roles && shown.length === 0 && <Muted>{stage ? 'No role works this stage.' : pre.roles_note ?? 'No role runs this workflow.'}</Muted>}
      {/* roles are present but the run would be refused: said whenever it is set, not only when empty */}
      {!stage && shown.length > 0 && pre.roles_note && <Muted>{pre.roles_note}</Muted>}
      {shown.map((e) => {
        const meta = e.agent ? agentMeta(e.agent) : null
        return (
          <span key={e.key} className="flex flex-col gap-1">
            {meta ? (
              <span className="agent-chip self-start max-w-full">
                <span className="ad" style={{ background: meta.color }} />
                <span className="truncate">{meta.label}</span>
              </span>
            ) : (
              <span className="text-[13px] text-tx" style={{ fontWeight: 650 }}>{e.name}</span>
            )}
            <span className="text-[12px] leading-[1.45] text-tx-3">
              {e.kind === 'lead' ? 'Lead' : e.kind === 'reviewer' ? 'Reviewer' : e.name !== meta?.label && e.name !== e.agent ? e.name : 'Helper'}
            </span>
          </span>
        )
      })}
      {shown.length > 0 && <ModelLine pre={pre} />}
      {/* the library is the workflow's, so a stage shows it only where a card can read it */}
      {shown.length > 0 && pre.roles && (!stage || shown.some((e) => e.tools.includes('read_skill'))) && (
        <>
          <SubLabel>Skills</SubLabel>
          {skills.length > 0 ? (
            <span className="flex flex-wrap gap-1">
              {skills.slice(0, SKILLS_SHOWN).map((s) => (
                <span key={s} className="h-[22px] px-2 rounded-full bg-bg-3 font-mono text-[11px] text-tx-2 inline-flex items-center">{s}</span>
              ))}
              {skills.length > SKILLS_SHOWN && (
                <span title={skills.slice(SKILLS_SHOWN).join(', ')} className="h-[22px] px-2 rounded-full bg-bg-3 text-[11px] text-tx-2 inline-flex items-center">+{skills.length - SKILLS_SHOWN}</span>
              )}
            </span>
          ) : <Muted>{pre.skills_note ?? missing}</Muted>}
        </>
      )}
    </Panel>
  )
}

const CHANGES = {
  on_its_own: { text: 'On its own', color: 'var(--good)' },
  asks_you: { text: 'Asks you', color: 'var(--fair)' },
} as const

function MayDo({ entries, pre, stage, missing }: { entries: Entry[]; pre: Partial<Preflight>; stage: StageDef | null; missing: string }) {
  const tools = new Set(covered(entries, stage?.tools ?? null).flatMap((e) => e.tools))
  const rows = (pre.permissions ?? []).filter((p) => !stage || tools.has(p.name) || (stage.handoff && p.name === HANDOFF_ROW))
  return (
    <Panel title="What it may do on its own">
      {!pre.permissions && <Muted>{missing}</Muted>}
      {pre.permissions && rows.length === 0 && <Muted>{stage ? 'Nothing it does at this stage needs a permission.' : pre.permissions_note ?? 'It holds no tools.'}</Muted>}
      {rows.map((p) => {
        const state = p.bound === false ? { text: 'Not connected', color: 'var(--tx-3)' } : CHANGES[p.changes]
        return (
          <span key={p.name} className="flex justify-between gap-2 text-[12px]">
            <span className={`min-w-0 break-words text-tx-2${p.label ? '' : ' font-mono'}`}>{p.label ?? p.name}</span>
            <span className="shrink-0" style={{ fontWeight: 700, color: state.color }}>{state.text}</span>
          </span>
        )
      })}
    </Panel>
  )
}

function Pill({ ask }: { ask: boolean }) {
  return (
    <span
      className={`h-[22px] px-2 rounded-full border border-line inline-flex items-center text-[11px] ${ask ? 'bg-[var(--fair-bg)] text-[var(--fair)]' : 'bg-bg-3 text-tx-2'}`}
      style={{ fontWeight: 700 }}
    >
      {ask ? 'Ask' : 'Auto'}
    </span>
  )
}

function StopsAndCheckpoints({ definition, single, pre, stage, selected, missing }: {
  definition: Definition
  single: boolean
  pre: Partial<Preflight>
  stage: StageDef | null
  selected: number | null
  missing: string
}) {
  const stops = [...(single ? [] : definition.objectives ?? []), ...(pre.budgets ? limitSentences(pre.budgets) : [])]
  const compose = definition.run_kind === 'compose' || !definition.run_kind
  const phaseStops = (pre.roles?.helpers ?? []).map((h, i) => ({ h, i })).filter(({ h, i }) => h.approval_required && (selected === null || i === selected))
  const classes = Object.keys(CHECKPOINT_CLASSES).filter((c) => pre.checkpoints && c in pre.checkpoints && (!stage || stage.checkpoints.includes(c)))
  const hasCheckpoints = pre.checkpoints && Object.keys(pre.checkpoints).length > 0
  return (
    <Panel title="Stops when">
      {stops.length === 0 && <Muted>{pre.budgets ? 'It declares no objective.' : missing}</Muted>}
      {stops.map((line) => (
        <span key={line} className="flex gap-2 text-[12px] leading-[1.4] text-tx-2">
          <span className="text-accent inline-flex pt-0.5"><Icon name="info" size={10} /></span>{line}
        </span>
      ))}
      <Muted>Per-stage stops · Not measured yet</Muted>
      <SubLabel>Checkpoints</SubLabel>
      {!pre.checkpoints && <Muted>{missing}</Muted>}
      {classes.map((c) => (
        <span key={c} className="flex items-center justify-between gap-2 text-[12px] text-tx-2">
          {CHECKPOINT_CLASSES[c].label}
          <Pill ask={pre.checkpoints?.[c] === 'ask'} />
        </span>
      ))}
      {hasCheckpoints && stage && classes.length === 0 && <Muted>No checkpoint at this stage.</Muted>}
      {compose && phaseStops.map(({ h, i }) => (
        <span key={i} className="flex items-center justify-between gap-2 text-[12px] text-tx-2">
          Approval required · {h.name}
          <Pill ask />
        </span>
      ))}
      {compose && pre.roles && phaseStops.length === 0 && <Muted>{selected === null ? 'This definition declares no pause.' : 'No pause at this stage.'}</Muted>}
      {!compose && !hasCheckpoints && pre.checkpoints && <Muted>{pre.checkpoints_note ?? 'This definition declares no pause.'}</Muted>}
      {hasCheckpoints && pre.checkpoints_note && <Muted>{pre.checkpoints_note}</Muted>}
    </Panel>
  )
}

/** A few lines of the definition's own text, all of it on request. */
function Instructions({ body }: { body: string }) {
  const [open, setOpen] = useState(false)
  const long = body.length > 280 || body.split('\n').length > 4
  return (
    <div className="flex flex-col gap-1.5">
      <SubLabel>Instructions</SubLabel>
      {/* fades out rather than cutting a line in half */}
      <div className={`text-[12.5px] text-tx-2${open || !long ? '' : ' max-h-[6.5em] overflow-hidden [mask-image:linear-gradient(to_bottom,#000_55%,transparent)]'}`}><Markdown>{body}</Markdown></div>
      {long && <button type="button" className="btn ghost self-start" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? 'Show less' : 'Show all'}</button>}
    </div>
  )
}

function RunsAsOneAgent({ definition, entries, pre, missing }: { definition: Definition; entries: Entry[]; pre: Partial<Preflight>; missing: string }) {
  const lead = entries.find((e) => e.kind === 'lead')
  const objectives = definition.objectives ?? []
  return (
    <Panel title="Runs as one agent">
      {!pre.roles && <Muted>{missing}</Muted>}
      {lead && <span className="text-[13px] text-tx" style={{ fontWeight: 650 }}>{lead.name}</span>}
      <ModelLine pre={pre} />
      {objectives.length > 0 && (
        <ul className="m-0 pl-5 list-disc text-[12px] leading-[1.45] text-tx-2 flex flex-col gap-1">
          {objectives.map((line) => <li key={line}>{line}</li>)}
        </ul>
      )}
      {definition.body && <Instructions body={definition.body} />}
    </Panel>
  )
}

/** Opens the workflow's latest run as the Watch a run page. The run is looked up on
 *  the click, not once per workflow on load, and a workflow that never ran says so. */
function WatchButton({ wf }: { wf: Workflow }) {
  const navigate = useNavigate()
  const [state, setState] = useState<'idle' | 'busy' | 'none'>('idle')
  const [failed, setFailed] = useState<string | null>(null)
  const watch = () => {
    setState('busy')
    setFailed(null)
    workflowApi
      .listRuns(wf.id, { limit: 1 })
      .then((res) => {
        const latest = (res.data?.runs as { run_id?: string }[] | undefined)?.[0]?.run_id
        if (!latest) return setState('none')
        setState('idle')
        navigate({ search: `?run=${encodeURIComponent(latest)}` })
      })
      .catch((e) => { setFailed(errMsg(e)); setState('idle') })
  }
  return (
    <button
      className="btn ghost" disabled={state !== 'idle'} onClick={watch}
      title={state === 'none' ? 'No runs yet' : failed ? `Couldn’t look up runs — ${failed}` : 'Replay the latest run step by step'}
    >
      <Icon name="play" /> {state === 'none' ? 'No runs yet' : 'Watch it run'}
    </button>
  )
}

function Header({ name, wf, actions, definition, onSave }: { name: string; wf: Workflow | null; actions: ReaderActions | null; definition: Definition; onSave?: () => Promise<unknown> }) {
  // optimistic, dropped once the list reads the row again
  const [now, setNow] = useState<boolean | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  useEffect(() => setNow(null), [wf?.enabled])
  const enabled = now ?? wf?.enabled ?? true
  const toggle = () => {
    if (!wf || !actions) return
    const on = !enabled
    setErr(null)
    setNow(on)
    workflowApi.setEnabled(wf.id, on).then(actions.onToggled).catch((e) => {
      setNow(!on)
      setErr(`Couldn’t turn ${name} ${on ? 'on' : 'off'}: ${errMsg(e)}`)
    })
  }
  // on success the screen swaps this pane out, so only a failure comes back here
  const save = () => {
    if (!onSave) return
    setErr(null)
    setSaving(true)
    onSave().catch((e) => { setErr(`Couldn’t save ${name}: ${errMsg(e)}`); setSaving(false) })
  }
  const version = definition.version ?? '—'
  const edited = !wf ? 'AI draft — not saved'
    : wf.source !== 'custom' ? `Built in · version ${version}`
    : definition.updated_at && !Number.isNaN(new Date(definition.updated_at).getTime())
      ? `Edited ${formatDistanceToNowStrict(new Date(definition.updated_at), { addSuffix: true })} · version ${version}`
      : `Edited · version ${version}`
  return (
    <div className="flex items-start gap-3">
      <div className="flex flex-col gap-1 flex-1 min-w-0">
        <div className="flex items-center gap-2.5">
          <h2 className="m-0 text-[20px] leading-[1.25] tracking-[-0.2px] text-tx min-w-0" style={{ fontWeight: 700 }}>{name}</h2>
          {wf && (
            <button
              type="button"
              role="switch"
              aria-checked={enabled}
              aria-label={`${name} on`}
              className="ag-switch shrink-0 disabled:opacity-50 disabled:cursor-not-allowed"
              disabled={!wf.canDisable}
              title={wf.canDisable ? undefined : ALWAYS_ON}
              onClick={toggle}
            ><span /></button>
          )}
        </div>
        {definition.description && <p className="m-0 text-[12px] leading-[1.45] text-tx-3">{definition.description}</p>}
        <p className="m-0 text-[12px] leading-[1.45] text-tx-3">{edited}</p>
        {wf && !wf.canDisable && <p className="m-0 text-[12px] leading-[1.45] text-tx-3">{ALWAYS_ON}</p>}
        {err && <p role="alert" className="m-0 text-[12px] leading-[1.45] text-[var(--poor)]">{err}</p>}
      </div>
      {!wf && onSave && (
        <div className="flex items-center gap-2 shrink-0">
          <button className="btn primary disabled:opacity-50 disabled:cursor-not-allowed" disabled={saving} onClick={save}><Icon name="check2" /> {saving ? 'Saving…' : 'Save workflow'}</button>
        </div>
      )}
      {wf && actions && (
        <div className="flex items-center gap-2 shrink-0">
          <WatchButton wf={wf} />
          <button className="btn ghost" onClick={actions.onHistory}><Icon name="clock" /> History</button>
          {wf.source === 'custom' && (
            <>
              <button className="btn ghost icon" title="Edit workflow" onClick={actions.onEdit}><Icon name="edit" /></button>
              <button className="btn ghost icon danger" title="Delete workflow" onClick={actions.onDelete}><Icon name="trash" /></button>
            </>
          )}
          <button className="btn primary disabled:opacity-50 disabled:cursor-not-allowed" disabled={!enabled} title={enabled ? undefined : 'Turn it on to run it'} onClick={actions.onRun}><Icon name="play" /> Run workflow</button>
        </div>
      )}
    </div>
  )
}

export default function WorkflowReaderPane(props: ReaderProps) {
  const draft = 'draft' in props ? props.draft : null
  const { phase, definition, pre, missing } = useSource('wf' in props ? props.wf.id : null, draft)
  const [selected, setSelected] = useState<number | null>(null)
  const entries = entriesOf(pre.roles)
  const huntLike = definition.hunt_like === true
  const runKind = definition.run_kind || 'compose'
  const single = !huntLike && runKind !== 'compose'
  // a compose draws one stage per phase, which the payload's helpers are
  const strip = huntLike ? stripFor(runKind) : runKind === 'compose' ? composeStrip(pre.roles?.helpers ?? []) : null
  const stages = strip ? [...strip.before, ...strip.loop, ...strip.after] : []
  const stage = selected === null ? null : stages[selected] ?? null
  return (
    <>
      {props.onBack && (
        <div className="flex items-center gap-3 flex-wrap px-[22px] py-[13px] border-b border-line">
          <button className="btn ghost" onClick={props.onBack}><Icon name="chevL" size={13} /> All workflows</button>
        </div>
      )}
      <div className="wfk-pane flex flex-col gap-3.5 px-[22px] pt-5 pb-[110px]">
        {phase === 'loading' && <div className="muted text-[12.5px]">Loading…</div>}
        {phase === 'error' && <p className="m-0 text-[13px] text-tx-2">Couldn’t load this workflow.</p>}
        {phase === 'ready' && (
          <>
            <Header name={'wf' in props ? props.wf.name : props.draft.name} wf={'wf' in props ? props.wf : null} actions={'wf' in props ? props : null} definition={definition} onSave={'draft' in props ? props.onSave : undefined} />
            {strip && stages.length > 0 && <HowItRuns strip={strip} entries={entries} pre={pre} selected={selected} onSelect={setSelected} />}
            {strip && stages.length === 0 && <Muted>{pre.roles ? pre.roles_note ?? 'This workflow declares no phases.' : missing}</Muted>}
            {single ? (
              <div className="grid grid-cols-2 gap-3 items-start">
                <RunsAsOneAgent definition={definition} entries={entries} pre={pre} missing={missing} />
                <div className="flex flex-col gap-3">
                  <MayDo entries={entries} pre={pre} stage={null} missing={missing} />
                  <StopsAndCheckpoints definition={definition} single pre={pre} stage={null} selected={null} missing={missing} />
                </div>
              </div>
            ) : (
              <div className="grid grid-cols-3 gap-3 items-start">
                <WhoDoesIt entries={entries} pre={pre} stage={stage} missing={missing} />
                <MayDo entries={entries} pre={pre} stage={stage} missing={missing} />
                <StopsAndCheckpoints definition={definition} single={false} pre={pre} stage={stage} selected={selected} missing={missing} />
              </div>
            )}
          </>
        )}
      </div>
    </>
  )
}
