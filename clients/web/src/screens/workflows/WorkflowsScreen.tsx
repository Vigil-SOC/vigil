import { Fragment, createContext, useCallback, useContext, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { format } from 'date-fns'
import { Icon } from '../../shared/icons'
import { EmptyState, Popup, TextInput, activateOnKey } from '../../shared/ui'
import { Markdown } from '../../shared/Markdown'
import { type Workflow, type AgentTemplate, type Skill } from '../../data/appData'
import { useWorkflows, useAgents, useAgentMeta, useSkills, workflowsOffered, type Phase } from './useWorkflowsData'
import { TITLES } from '../../data/data'
import { approvalsApi, workflowApi, agentsApi, findingsApi, casesApi, type GeneratedAgentDraft, type ReplayReport } from '../../services/api'
import WorkflowBuilder from './WorkflowBuilder'
import { SkillDeleteModal, SkillDrawer } from './SkillDrawer'
import type { ConsoleScreenProps } from '../../shared/types'
import { Cost } from '../../shared/cost'
import { COMMANDS } from '../../shell/commandBar'
import { WatchRun } from './WatchRun'
import {
  IN_FLIGHT, callLine, errMsg, fmtDuration, runStatusColor, useInvestigateReplay, useRunDetail,
  type HuntCheckpoint, type HuntEvidence, type HuntGap, type HuntHandoff, type HuntMove,
  type HuntQuestion, type HuntRecall, type HuntStanding, type HuntStrength, type HuntView,
  type InvestigateDecisionView, type RecalledFrom, type RecalledWindow,
  type WfRun, type WfRunDetail,
} from './runRead'

type WfTab = 'workflows' | 'agents' | 'skills' | 'commands'

/** One list's hook result. The screen owns the three lists so a tab chip counts
 *  the same rows the tab shows, and a mutation in a tab updates the chip. */
export interface Feed<T> {
  rows: T[]
  phase: Phase
  error: string | null
  reload: () => void
}

const [PAGE_TITLE, PAGE_DESC] = TITLES.workflows

export default function WorkflowsScreen({ goSettings }: ConsoleScreenProps) {
  const [tab, setTab] = useState<WfTab>('workflows')
  // lifted so the header's "New workflow" opens the same builder from any tab
  const [creating, setCreating] = useState<null | 'blank' | 'ai'>(null)
  const workflows = useWorkflows()
  const agents = useAgents()
  const skills = useSkills()
  // ?run=<id> opens one run in place of the catalog, so a case activity can deep-link to it.
  const [searchParams, setSearchParams] = useSearchParams()
  const runId = searchParams.get('run')
  const backToCatalog = useCallback(() => setSearchParams({}), [setSearchParams])
  // no chip while a list is loading or failed: a count of 0 would read as empty
  const count = (feed: { rows: unknown[]; phase: Phase }) => (feed.phase === 'ready' ? feed.rows.length : null)
  const tabs: [WfTab, string, number | null][] = [
    ['workflows', 'Workflows', count(workflows)],
    ['agents', 'Agents', count(agents)],
    ['skills', 'Skills', count(skills)],
    ['commands', 'Commands', COMMANDS.length],
  ]
  // the Watch a run page carries its own header, as the board has it; the tab strip comes back with the catalog
  const watching = tab === 'workflows' && runId !== null
  return (
    <>
      <div className={`flex flex-col gap-3.5 px-[26px] pt-5${watching ? ' hidden' : ''}`}>
        <div className="flex items-end justify-between gap-5">
          <div className="flex flex-col gap-[5px] min-w-0">
            {/* inline weight: the shell's unlayered h1 rule would beat a utility class */}
            <h1 className="m-0 text-[20px] leading-[1.25] tracking-[-0.2px] text-tx" style={{ fontWeight: 700 }}>{PAGE_TITLE}</h1>
            <p className="m-0 text-[13px] leading-[1.5] text-tx-2 max-w-[760px]">{PAGE_DESC}</p>
          </div>
          <button className="btn primary wf-new" onClick={() => setCreating('blank')}><Icon name="plus" /> New workflow</button>
        </div>
        <div className="wf-tabs" role="tablist" aria-label="Workflow views">
          {tabs.map(([k, label, n]) => (
            <button
              key={k}
              role="tab"
              aria-selected={tab === k}
              aria-label={n === null ? label : `${label} ${n}`}
              className="wf-tab"
              onClick={() => setTab(k)}
            >
              {label}
              {n !== null && <span className="wf-count">{n}</span>}
            </button>
          ))}
        </div>
      </div>
      {tab === 'workflows' && (runId ? <RunView key={runId} runId={runId} onBack={backToCatalog} /> : <WorkflowCatalog feed={workflows} onCreate={setCreating} goSettings={goSettings} />)}
      {tab === 'agents' && <AgentsTab feed={agents} />}
      {tab === 'skills' && <SkillsTab feed={skills} workflows={workflows} agents={agents} />}
      {tab === 'commands' && <CommandsTab />}
      {creating && <WorkflowBuilder autoGenerate={creating === 'ai'} onClose={() => setCreating(null)} onSaved={() => { setCreating(null); workflows.reload() }} />}
    </>
  )
}

const CMD_GRID = 'grid grid-cols-[150px_minmax(0,2fr)_minmax(0,1.6fr)_170px_56px] gap-3 px-4'

/** The command bar's rows, read from COMMANDS. Later rows are marked and carry
 *  nothing focusable; nothing in the table runs a command. */
function CommandsTab() {
  return (
    <div className="px-[22px] py-5 flex flex-col gap-3">
      <span className="text-xs leading-[1.45] text-[var(--tx2)]">
        Type a command in search or in Ask Vigil. Each command runs a workflow or an action; anything that changes something still asks you.
      </span>
      <div role="table" aria-label="Commands" className="bg-[var(--bg1)] border border-[var(--ln0)] rounded-[14px] overflow-hidden">
        <div role="row" className={`${CMD_GRID} py-[11px] text-[11px] font-bold text-[var(--tx2)]`}>
          <span role="columnheader">Command</span>
          <span role="columnheader">What it does</span>
          <span role="columnheader">Runs</span>
          <span role="columnheader">Arguments</span>
          <span role="columnheader" className="sr-only">Status</span>
        </div>
        {COMMANDS.map((command) => (
          <div
            key={command.id}
            role="row"
            aria-disabled={command.later || undefined}
            className={`${CMD_GRID} items-center py-[11px] border-t border-[var(--ln0)]`}
          >
            <span role="cell" className="font-mono text-[13px] text-[var(--ac)] truncate">{command.name}</span>
            <span role="cell" title={command.desc} className="text-xs text-[var(--tx1)] line-clamp-2">{command.desc}</span>
            <span role="cell" title={command.runs} className="text-xs font-[650] text-[var(--tx0)] line-clamp-2">{command.runs}</span>
            <span role="cell" title={command.hint} className="font-mono text-[11px] text-[var(--tx2)] truncate">{command.hint}</span>
            <span role="cell" className="text-[11px] font-semibold text-[var(--tx2)]">{command.later ? 'Later' : ''}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

/** One run reached by URL rather than through History, opened as the Watch a run
 *  page. Same hook as RunRow, so the run polls while in flight and stops at
 *  terminal. Keyed on the id by the caller, so a new ?run= starts clean rather
 *  than over the old detail.
 *  No seed: the hook will not poll until getRun says the run is in flight, so a
 *  missing run is asked for once. */
function RunView({ runId, onBack }: { runId: string; onBack: () => void }) {
  const { detail, dphase, setDphase, load } = useRunDetail(runId, true)
  useEffect(() => {
    setDphase('loading')
    void load()
  }, [load, setDphase])
  if (dphase === 'ready' && detail) return <WatchRun d={detail} onBack={onBack} />
  return (
    <>
      <div className="flex items-center gap-3 flex-wrap px-[22px] py-[13px] border-b border-line">
        <button className="btn ghost" onClick={onBack}><Icon name="chevL" size={13} /> Workflows</button>
        <span className="mono text-[11.5px] text-tx-3">{runId}</span>
      </div>
      <div className="px-[22px] py-5">
        {dphase === 'error' ? <div className="muted">Couldn’t load run {runId}. It may have been removed, or the id may be wrong.</div> : <div className="muted">Loading run detail…</div>}
      </div>
    </>
  )
}

function StateMsg({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ padding: '10px 22px 22px' }}>
      {children}
    </div>
  )
}

function AgentSequence({ agents, ordered }: { agents: string[]; ordered: boolean }) {
  const agentMeta = useAgentMeta()
  const ref = useRef<HTMLDivElement>(null)
  // how many chips to show; null = render all, then measure how many fit on one line
  const [limit, setLimit] = useState<number | null>(null)
  useEffect(() => {
    const reset = () => setLimit(null)
    window.addEventListener('resize', reset)
    return () => window.removeEventListener('resize', reset)
  }, [])
  useEffect(() => setLimit(null), [agents])
  useLayoutEffect(() => {
    const el = ref.current
    if (limit !== null || !el) return
    const chips = Array.from(el.querySelectorAll<HTMLElement>('.agent-chip'))
    let k = chips.filter((c) => c.offsetTop === chips[0].offsetTop).length
    if (k < chips.length) {
      // leave room for the "+N" chip (and the arrow before it)
      while (k > 1 && chips[k - 1].offsetLeft + chips[k - 1].offsetWidth + MORE_CHIP_PX > el.clientWidth) k--
    }
    setLimit(k)
  }, [limit, agents])
  const shown = limit === null ? agents : agents.slice(0, limit)
  const hidden = agents.length - shown.length
  return (
    <div className="agent-seq" ref={ref} title={hidden > 0 ? agents.map((a) => agentMeta(a).label).join(ordered ? ' → ' : ', ') : undefined}>
      {shown.map((a, i) => {
        const meta = agentMeta(a)
        return (
          <Fragment key={i}>
            <span className="agent-chip">
              <span className="ad" style={{ background: meta.color }} />
              {meta.label}
            </span>
            {ordered && (i < shown.length - 1 || hidden > 0) && (
              <span className="seq-arrow"><Icon name="chevR" /></span>
            )}
          </Fragment>
        )
      })}
      {hidden > 0 && <span className="agent-chip">+{hidden}</span>}
    </div>
  )
}

// width reserved for the "+N" chip and its arrow when the agent list is collapsed
const MORE_CHIP_PX = 64

const TRUST_INFO = 'Tier, trust, and agreement are not recorded.'

function fmtEdited(iso?: string): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '—' : format(d, 'MMM d · HH:mm')
}

type WfModal = { kind: 'run' | 'history' | 'edit' | 'delete' | 'details'; wf: Workflow }

function WorkflowCatalog({ feed, onCreate, goSettings }: { feed: Feed<Workflow>; onCreate: (kind: 'blank' | 'ai') => void; goSettings: ConsoleScreenProps['goSettings'] }) {
  const { rows, phase, error, reload } = feed
  const [q, setQ] = useState('')
  const [modal, setModal] = useState<WfModal | null>(null)
  const close = () => setModal(null)
  const list: Workflow[] = q
    ? rows.filter((w) => w.name.toLowerCase().includes(q.toLowerCase()))
    : rows
  return (
    <>
      <div className="flex items-center gap-3 flex-wrap px-[22px] py-[13px] border-b border-line">
        <div className="search" style={{ maxWidth: 320 }}>
          <span><Icon name="search" /></span>
          <input aria-label="Search workflows" placeholder="Search workflows…" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        <div className="flex-1" />
        <button className="btn ghost icon" title="Refresh" onClick={reload}><Icon name="refresh" /></button>
        <button className="btn ghost" onClick={() => onCreate('ai')}><Icon name="sparkle" /> Generate with AI</button>
      </div>
      {phase === 'loading' && <StateMsg><EmptyState loading compact icon="flow" title="Loading workflows…" /></StateMsg>}
      {phase === 'error' && <StateMsg><EmptyState error icon="alert" title="Couldn’t load workflows" body={error} primary={{ label: 'Retry', onClick: reload, icon: 'refresh' }} /></StateMsg>}
      {phase === 'ready' && list.length === 0 && (
        <StateMsg>
          <EmptyState
            icon={q ? 'filter' : 'flow'}
            title={q ? 'No workflows match this search' : 'No workflows yet'}
            body={q ? 'Clear the search to return to the workflow catalog.' : 'Create a workflow manually or generate one with AI from a plain-language investigation goal.'}
            primary={q ? { label: 'Clear search', onClick: () => setQ(''), icon: 'close' } : { label: 'New workflow', onClick: () => onCreate('blank'), icon: 'plus' }}
            secondary={q ? undefined : { label: 'Generate with AI', onClick: () => onCreate('ai'), icon: 'sparkle' }}
          />
        </StateMsg>
      )}
      {phase === 'ready' && list.length > 0 && (
        // bottom padding keeps the last row's actions clear of the fixed Ask Vigil button
        <div className="px-[22px] pt-5 pb-[110px]">
          <div className="table-wrap">
            <table className="tbl wf-catalog">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Agents</th>
                  <th>Runs today</th>
                  <th>Cost per run</th>
                  <th>Last edited</th>
                  <th>Trust</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {list.map((w) => (
                  <tr key={w.id}>
                    <td className="wfc-name">
                      <div
                        role="button"
                        tabIndex={0}
                        className="font-semibold cursor-pointer"
                        onClick={() => setModal({ kind: 'details', wf: w })}
                        onKeyDown={activateOnKey(() => setModal({ kind: 'details', wf: w }))}
                      >
                        {w.name}
                      </div>
                      {w.desc && <div className="wfc-desc text-[12px] text-tx-3 mt-0.5" title={w.desc}>{w.desc}</div>}
                    </td>
                    <td className="wfc-agents">{w.agents.length > 0 ? <AgentSequence agents={w.agents} ordered={!w.huntLike} /> : '—'}</td>
                    <td>{w.runsToday}</td>
                    <td>{w.meanCostUsd == null ? '—' : <Cost usd={w.meanCostUsd} />}</td>
                    <td>{fmtEdited(w.updatedAt)}</td>
                    <td>
                      <span className="inline-flex items-center gap-1.5">
                        Not measured yet
                        <button type="button" className="btn ghost icon" aria-label={TRUST_INFO} title={TRUST_INFO}>
                          <Icon name="info" size={14} />
                        </button>
                      </span>
                    </td>
                    <td>
                      <div className="flex items-center gap-2 justify-end">
                        <WatchButton wf={w} />
                        <button className="btn ghost" onClick={() => setModal({ kind: 'history', wf: w })}><Icon name="clock" /> History</button>
                        {w.source === 'custom' && (
                          <>
                            <button className="btn ghost icon" title="Edit workflow" onClick={() => setModal({ kind: 'edit', wf: w })}><Icon name="edit" /></button>
                            <button className="btn ghost icon danger" title="Delete workflow" onClick={() => setModal({ kind: 'delete', wf: w })}><Icon name="trash" /></button>
                          </>
                        )}
                        <button className="btn primary" onClick={() => setModal({ kind: 'run', wf: w })}><Icon name="play" /> Run workflow</button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
      {modal?.kind === 'details' && <WorkflowReader wf={modal.wf} onClose={close} />}
      {modal?.kind === 'run' && <RunModal wf={modal.wf} onClose={close} onStarted={() => setModal({ kind: 'history', wf: modal.wf })} />}
      {modal?.kind === 'history' && <HistoryModal wf={modal.wf} onClose={close} />}
      {modal?.kind === 'edit' && <EditModal wf={modal.wf} onClose={close} onSaved={() => { close(); reload() }} />}
      {modal?.kind === 'delete' && <DeleteModal wf={modal.wf} onClose={close} onDeleted={() => { close(); reload() }} />}
      {phase === 'ready' && rows.length === 0 && (
        <div className="px-[22px] pb-5">
          <button className="btn ghost" onClick={() => goSettings('ai-config')}><Icon name="gear" /> Configure AI models</button>
        </div>
      )}
    </>
  )
}

/** Opens the workflow's latest run as the Watch a run page. The run is looked up on
 *  the click, not once per row on load, and a workflow that never ran says so. */
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
        const latest = (res.data?.runs as WfRun[] | undefined)?.[0]?.run_id
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

const INPUT_CLS = 'w-full bg-bg border border-line rounded-[7px] px-2.5 py-2 text-[13px] text-tx outline-none focus:border-accent-line'

function Field({ label, value, onChange, placeholder, textarea, mono, hint, maxLength, list, rows = 3 }: {
  label: string
  value: string
  onChange: (v: string) => void
  placeholder?: string
  textarea?: boolean
  mono?: boolean
  hint?: string
  maxLength?: number
  list?: string
  rows?: number
}) {
  const cls = `${INPUT_CLS}${mono ? ' font-mono' : ''}`
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-[11px] uppercase tracking-[0.06em] text-tx-3">{label}</span>
      {textarea ? (
        // resize-y + max-w-full: grow vertically only, never wider than the modal
        <textarea className={`${cls} resize-y max-w-full`} rows={rows} value={value} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} />
      ) : (
        <input className={cls} value={value} placeholder={placeholder} maxLength={maxLength} list={list} onChange={(e) => onChange(e.target.value)} />
      )}
      {hint && <span className="text-[11px] text-tx-3">{hint}</span>}
    </label>
  )
}

/** uses the console's .drop-menu, not the native datalist chrome */
function ComboField({ label, value, onChange, placeholder, options, hint }: {
  label: string
  value: string
  onChange: (v: string) => void
  placeholder?: string
  options: { id: string; label?: string }[]
  hint?: string
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false) }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('mousedown', onDoc); document.removeEventListener('keydown', onKey) }
  }, [open])

  const q = value.trim().toLowerCase()
  const filtered = options
    .filter((o) => !q || o.id.toLowerCase().includes(q) || (o.label || '').toLowerCase().includes(q))
    .slice(0, 50)

  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-[11px] uppercase tracking-[0.06em] text-tx-3">{label}</span>
      <div className="drop field-drop" ref={ref}>
        <input
          className={`${INPUT_CLS} font-mono`}
          value={value}
          placeholder={placeholder}
          onChange={(e) => { onChange(e.target.value); setOpen(true) }}
          onFocus={() => setOpen(true)}
        />
        {open && filtered.length > 0 && (
          <div className="drop-menu field-menu" role="listbox">
            {filtered.map((o) => (
              <button key={o.id} type="button" role="option" aria-selected={o.id === value} className={o.id === value ? 'sel' : ''} onMouseDown={(e) => { e.preventDefault(); onChange(o.id); setOpen(false) }}>
                <span className="font-mono">{o.id}</span>{o.label ? <span className="text-tx-3"> · {o.label}</span> : null}
              </button>
            ))}
          </div>
        )}
      </div>
      {hint && <span className="text-[11px] text-tx-3">{hint}</span>}
    </label>
  )
}

interface ReaderPhase {
  id?: string
  phase_id?: string
  agent?: string
  agent_id?: string
  name?: string
  tools?: string[]
  approval_required?: boolean
}

interface WfDetail {
  body?: string
  /** Single-agent file workflows only: the lead, and the model a run uses. */
  agent?: { role: string; model: string | null; model_source: 'assignment' | 'default' | null }
  run_kind?: string
  hunt_like?: boolean
  objectives?: unknown
  checkpoints?: unknown
  phases?: ReaderPhase[] | null
}

type ReaderKind = 'roster' | 'single' | 'ordered'

function readerKind(huntLike: boolean, runKind: string): ReaderKind {
  if (huntLike) return 'roster'
  if (runKind === 'investigate' || runKind === 'root_cause') return 'single'
  return 'ordered'
}

function phasesOf(detail: WfDetail): ReaderPhase[] {
  return Array.isArray(detail.phases) ? detail.phases : []
}

function phaseAgent(phase: ReaderPhase): string {
  return phase.agent || phase.agent_id || ''
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.filter((item): item is string => typeof item === 'string' && item.trim() !== '')
}

function checkpointEntries(value: unknown): [string, string][] {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return []
  return Object.entries(value).filter((entry): entry is [string, string] => typeof entry[1] === 'string')
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return <span className="text-[10.5px] uppercase tracking-[0.07em] text-tx-3">{children}</span>
}

function PhaseTools({ tools }: { tools: string[] }) {
  if (tools.length === 0) return null
  return (
    <div className="flex flex-wrap gap-1.5 mt-1.5">
      {tools.map((tool) => (
        <span key={tool} className="font-mono text-[11.5px] text-tx-2 bg-bg border border-line-soft rounded-[6px] px-2 py-1">{tool}</span>
      ))}
    </div>
  )
}

function Roster({ detail }: { detail: WfDetail }) {
  const agentMeta = useAgentMeta()
  return (
    <div className="flex flex-col gap-3">
      <p className="text-[13px] text-tx-2">The lead dispatches among these.</p>
      {phasesOf(detail).map((phase, i) => {
        const agent = phaseAgent(phase)
        const meta = agentMeta(agent)
        return (
          <div key={phase.id || phase.phase_id || `${agent}-${i}`}>
            <span className="agent-chip">
              <span className="ad" style={{ background: meta.color }} />
              {meta.label}
            </span>
            <PhaseTools tools={phase.tools || []} />
            {phase.approval_required && <div className="text-[12px] text-tx-2 mt-1">Approval required</div>}
          </div>
        )
      })}
    </div>
  )
}

/** Read-only instructions, clamped to a few lines until "Show all". */
function Instructions({ body }: { body: string }) {
  const ref = useRef<HTMLDivElement>(null)
  const [open, setOpen] = useState(false)
  const [clipped, setClipped] = useState(false)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el || open) return
    const measure = () => setClipped(el.scrollHeight > el.clientHeight + 1)
    measure()
    // The same text clips differently at another width.
    if (typeof ResizeObserver === 'undefined') return
    const watch = new ResizeObserver(measure)
    watch.observe(el)
    return () => watch.disconnect()
  }, [body, open])
  return (
    <div className="flex flex-col gap-1.5">
      <SectionLabel>Instructions</SectionLabel>
      <div
        ref={ref}
        className={`bg-bg-2 border border-line rounded-[10px] px-3 py-2.5 overflow-hidden${open ? '' : ' max-h-[7.5rem]'}`}
      >
        <ReportBody md={body} />
      </div>
      {(clipped || open) && (
        <button type="button" className="self-start text-[12px] text-accent" aria-expanded={open} onClick={() => setOpen(!open)}>
          {open ? 'Show less' : 'Show all'}
        </button>
      )}
    </div>
  )
}

function SingleAgent({ detail }: { detail: WfDetail }) {
  const objectives = stringList(detail.objectives)
  const { agent, body } = detail
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-2">
        <p className="text-[13px] text-tx-2">This workflow runs as one agent.</p>
        {agent && (
          <div className="flex flex-col min-w-0">
            <span className="text-[13px] font-bold text-tx">{agent.role}</span>
            {agent.model && (
              <span className="text-[12px] text-tx-2">
                <span className="font-semibold text-tx">{agent.model}</span>
                {' · '}
                {agent.model_source === 'assignment' ? 'Investigation default' : 'Default'}
              </span>
            )}
          </div>
        )}
        {objectives.length > 0 && (
          <ul className="list-disc pl-5 text-[13px] text-tx-2 flex flex-col gap-1">
            {objectives.map((line) => <li key={line}>{line}</li>)}
          </ul>
        )}
      </div>
      {body?.trim() && <Instructions body={body} />}
    </div>
  )
}

function OrderedPhases({ detail }: { detail: WfDetail }) {
  const agentMeta = useAgentMeta()
  return (
    <div className="flex flex-col gap-2">
      <p className="text-[13px] text-tx-2">Phases run in this order.</p>
      <ol className="flex flex-col gap-3 list-decimal pl-5">
        {phasesOf(detail).map((phase, i) => {
          const agent = phaseAgent(phase)
          const meta = agentMeta(agent)
          return (
            <li key={phase.id || phase.phase_id || `${agent}-${i}`} className="text-[13px] text-tx-2">
              <span className="font-semibold">{meta.label}</span>
              {phase.name ? <span className="text-tx-3"> · {phase.name}</span> : null}
              {phase.approval_required ? <span> · Approval required</span> : null}
              <PhaseTools tools={phase.tools || []} />
            </li>
          )
        })}
      </ol>
    </div>
  )
}

function ReaderShape({ detail }: { detail: WfDetail }) {
  const kind = readerKind(detail.hunt_like === true, detail.run_kind || 'compose')
  switch (kind) {
    case 'roster':
      return <Roster detail={detail} />
    case 'single':
      return <SingleAgent detail={detail} />
    case 'ordered':
      return <OrderedPhases detail={detail} />
    default: {
      const _exhaustive: never = kind
      return _exhaustive
    }
  }
}

function Pauses({ detail }: { detail: WfDetail }) {
  const policies = checkpointEntries(detail.checkpoints)
  const approval = phasesOf(detail).some((phase) => phase.approval_required)
  if (policies.length === 0 && !approval) {
    return <p className="text-[13px] text-tx-2">This definition declares no pause.</p>
  }
  if (policies.length === 0) return null
  return (
    <div className="flex flex-col gap-1.5">
      <SectionLabel>Checkpoints</SectionLabel>
      {policies.map(([name, policy]) => (
        <div key={name} className="text-[13px] text-tx-2">
          <span className="font-mono">{name}</span> {policy}
        </div>
      ))}
    </div>
  )
}

function WorkflowReader({ wf, onClose }: { wf: Workflow; onClose: () => void }) {
  const [detail, setDetail] = useState<WfDetail | null>(null)
  const [phase, setPhase] = useState<'loading' | 'ready' | 'error'>('loading')

  useEffect(() => {
    let cancelled = false
    workflowApi
      .get(wf.id)
      .then((res) => { if (!cancelled) { setDetail(res.data as WfDetail); setPhase('ready') } })
      .catch(() => { if (!cancelled) setPhase('error') })
    return () => { cancelled = true }
  }, [wf.id])

  return (
    <Popup open onClose={onClose} title={wf.name} width={820}>
      <div className="flex flex-col gap-4">
        {phase === 'loading' && <div className="muted text-[12.5px]">Loading…</div>}
        {phase === 'error' && <p className="text-[13px] text-tx-2">Couldn’t load this workflow.</p>}
        {phase === 'ready' && detail && (
          <>
            <ReaderShape detail={detail} />
            <Pauses detail={detail} />
            <div className="flex flex-col gap-1.5">
              <SectionLabel>Per-stage stops</SectionLabel>
              <p className="text-[13px] text-tx-2">Not measured yet</p>
            </div>
          </>
        )}
      </div>
    </Popup>
  )
}

/** Confirms the run reached the server, then hands off to History rather than
 *  becoming a second live view of it. */
function StartedRun({ runId, onView, onClose }: { runId: string; onView: () => void; onClose: () => void }) {
  const { detail, load } = useRunDetail(runId, true, 'running')
  useEffect(() => { void load() }, [load])

  return (
    <div className="flex flex-col items-center gap-3.5 text-center">
      <div className="w-11 h-11 rounded-full grid place-items-center bg-ok-dim" style={{ color: 'var(--ok)' }}>
        <Icon name="check" size={22} />
      </div>
      <p className="text-[14.5px] font-semibold">Started</p>
      <p className="text-[12.5px] text-tx-3 leading-[1.5] max-w-[340px]">
        It runs on the server whether this stays open or not. Everything from here — beliefs, evidence, cost and
        any checkpoint it raises — lives in History.
      </p>
      <span className="mono text-[11.5px] text-tx-3 bg-bg-2 border border-line rounded-md px-2.5 py-1">{runId.slice(0, 8)}</span>
      <StartedPreview detail={detail} />
      <div className="flex gap-2.5 w-full pt-1">
        <button className="btn ghost flex-1 justify-center" onClick={onClose}>Close</button>
        <button className="btn primary flex-1 justify-center" onClick={onView}>View in History <Icon name="chevR" size={14} /></button>
      </div>
    </div>
  )
}

/** The first seconds of the run, so Started is a fact about the ledger and not
 *  just about the POST — a hunt raises its approval checkpoint immediately. */
function StartedPreview({ detail }: { detail: WfRunDetail | null }) {
  if (detail === null) {
    return <div className="muted text-[12px] w-full text-left px-3 py-2.5 rounded-[9px] border border-line-soft bg-bg">Waiting for the run to open its ledger…</div>
  }
  const hunt = detail.hunt ?? null
  const open = hunt?.open_checkpoint ?? null
  return (
    <div className="w-full text-left rounded-[9px] border border-line-soft bg-bg px-3 py-2.5">
      <div className="text-[11.5px] font-semibold flex items-center gap-1.5" style={{ color: runStatusColor(detail.status) }}>
        <span className="w-1.5 h-1.5 rounded-full bg-current" />{detail.status}
      </div>
      {open === null && (
        <div className="text-[12px] text-tx-3 leading-[1.5] mt-1.5">
          {hunt ? `Iteration ${hunt.iteration} · ${hunt.evidence_count} piece(s) of evidence` : 'No checkpoint raised.'}
        </div>
      )}
      {hunt !== null && <OpenCheckpoint hunt={hunt} />}
    </div>
  )
}

/** Pre-run facts: what a hunt costs at most, and what this deployment cannot answer. */
interface WfLimits {
  capabilities?: { bound: string[]; unbound: string[] }
  budgets?: { max_iterations: number; max_cost_usd: number }
  /** exact, zero or unknown — how confidently the model's rate resolved. */
  pricing?: { model: string; source: string }
}

/** The ceiling, not an estimate: per-call cost varies several-fold as the transcript
 *  grows, so a per-turn figure would be invented precision. */
function turnsHint(asked: string, cost: string, limits: WfLimits | null): string {
  const turns = asked.trim() === '' ? limits?.budgets?.max_iterations : Number(asked)
  const cap = cost.trim() === '' ? limits?.budgets?.max_cost_usd : Number(cost)
  const where = cap === undefined || !Number.isFinite(cap) ? '' : ` It stops at $${cap.toFixed(2)} whatever happens.`
  if (turns === undefined) return 'Turns the hunt may take before it stops and reports.'
  return `${turns} turn(s): each is a lead decision, the workers it dispatches and the pass that argues against them.${where}`
}

/** What the hunt will not be able to look at, said before the run costs anything.
 *  The same fact reaches the journal only once the run is over. */
function Blindness({ unbound }: { unbound: string[] }) {
  if (unbound.length === 0) return null
  const blind = unbound.includes('telemetry_search')
  return (
    <div className="text-[12.5px] leading-[1.5]" style={{ color: 'var(--high)' }}>
      No tool here answers {unbound.join(', ')}.{' '}
      {blind
        ? 'Without telemetry_search the hunt cannot query a SIEM, so it can corroborate nothing and will report that nothing was proven — a fact about this deployment, not about your estate.'
        : 'The roles that need it will run without it, and the hunt will record the gap.'}
    </div>
  )
}

/** One belief per line — the same split the server does, so a pasted paragraph
 *  with hard wraps is shown as the beliefs it would really become. */
function parsedHypotheses(text: string): string[] {
  return text.split('\n').map((line) => line.trim()).filter((line) => line !== '')
}

/** The two fragment shapes that turn up: a lead-in ending in a colon, and a
 *  wrapped line that does not start a sentence. */
function looksUnfinished(line: string): boolean {
  return line.endsWith(':') || /^[a-z]/.test(line)
}

/** The ten kinds a key can carry, held to the hunt's own closed set by the spec
 *  loader, which refuses anything else rather than coercing it. Spelled here so
 *  the operator is told before submitting, and ratcheted against
 *  `workflows/hunt/types.ts` by `test_recall_contract_agrees` — a list that
 *  drifts tells them a key is fine that no reader will ever query. */
const ENTITY_TYPES = ['ip', 'domain', 'host', 'url', 'email', 'hash', 'arn', 'aws_key', 'user', 'process']

/** `type:value`, comma separated — the form the hunt already writes an entity in.
 *  Split on the first colon, because a url value carries its own. */
function parsedSubjects(raw: string): { keys: string[]; bad: string[] } {
  const keys: string[] = []
  const bad: string[] = []
  for (const piece of raw.split(',').map((one) => one.trim()).filter((one) => one !== '')) {
    const at = piece.indexOf(':')
    const kind = at < 1 ? '' : piece.slice(0, at).trim().toLowerCase()
    const value = at < 1 ? '' : piece.slice(at + 1).trim()
    if (value !== '' && ENTITY_TYPES.includes(kind)) keys.push(`${kind}:${value}`)
    else bad.push(piece)
  }
  return { keys, bad }
}

/** What each belief is about, keyed by the belief itself. Editing a statement drops
 *  its subjects, which is the honest outcome: they were about the older claim. */
function subjectsAsked(text: string, held: Record<string, string>): Record<string, string[]> {
  const asked: Record<string, string[]> = {}
  for (const belief of parsedHypotheses(text)) {
    const { keys } = parsedSubjects(held[belief] ?? '')
    if (keys.length > 0) asked[belief] = keys
  }
  return asked
}

function malformedSubjects(text: string, held: Record<string, string>): string[] {
  return parsedHypotheses(text).flatMap((belief) => parsedSubjects(held[belief] ?? '').bad)
}

function HypothesisPreview({
  text,
  subjects,
  onSubject,
}: {
  text: string
  subjects: Record<string, string>
  onSubject: (belief: string, raw: string) => void
}) {
  const beliefs = parsedHypotheses(text)
  if (beliefs.length === 0) return null
  const suspect = beliefs.filter(looksUnfinished).length

  return (
    <div className="hyp-preview">
      <div className="hyp-preview-head">
        This puts <b>{beliefs.length}</b> belief{beliefs.length === 1 ? '' : 's'} on the board, plus the benign
        account as the claim to beat.
      </div>
      <ol className="hyp-preview-list">
        {beliefs.map((belief, at) => {
          const raw = subjects[belief] ?? ''
          const { bad } = parsedSubjects(raw)
          return (
            <li key={at} className={looksUnfinished(belief) ? 'suspect' : undefined}>
              <span className="hyp-preview-n">H{at + 1}</span>
              <span className="hyp-preview-belief">
                <span>{belief}</span>
                <input
                  className={bad.length > 0 ? 'hyp-preview-subject bad' : 'hyp-preview-subject'}
                  value={raw}
                  onChange={(e) => onSubject(belief, e.target.value)}
                  placeholder="About — host:dev-830, ip:45.77.53.176"
                  aria-label={`What H${at + 1} is about`}
                />
                {bad.length > 0 && (
                  <span className="hyp-preview-bad">
                    {bad.join(', ')} — each subject is type:value, where type is one of {ENTITY_TYPES.join(', ')}.
                  </span>
                )}
              </span>
            </li>
          )
        })}
      </ol>
      {suspect > 0 && (
        <div className="hyp-preview-warn">
          {suspect === 1 ? 'One line reads' : `${suspect} lines read`} as a fragment rather than a claim — a
          wrapped sentence, or a lead-in. One belief per line: join the wrapped ones up, and drop anything that
          is not a claim the hunt can argue against.
        </div>
      )}
    </div>
  )
}

/** An unpriced model cannot be held to a cost ceiling, so the hunt stops a few
 *  calls in. Said here, before the spend, rather than after it. */
function Unpriced({ pricing }: { pricing?: { model: string; source: string } }) {
  if (pricing === undefined || pricing.source !== 'unknown') return null
  return (
    <div className="text-[12.5px] leading-[1.5]" style={{ color: 'var(--high)' }}>
      Nothing here can price {pricing.model}, so the hunt cannot hold itself to a cost
      ceiling and will stop after its first few calls rather than run uncosted. Set a rate
      for it, or pick a model that has one, before starting.
    </div>
  )
}

/** What `/workflows/threat-hunt/coverage` answers. `in_flight` rows arrive on
 *  `running`, `concluded` rows plus a `proposal` on `concluded`, only the
 *  `proposal` on `uncovered`. The proposal is an execute body as-is. */
interface HuntProposal {
  hypothesis: string
  hypothesis_subjects: Record<string, string[]>
  approve_hypotheses?: boolean
}
interface InFlightRow {
  run_id: string
  status: string
  matched_keys: string[]
  matched_techniques: string[]
}
interface ConcludedRow {
  statement: string
  outcome: string
  concluded_at: string | null
  origin_run_id: string | null
  matched_keys: string[]
  matched_techniques: string[]
}
interface HuntCoverage {
  status: 'running' | 'concluded' | 'uncovered'
  keys: string[]
  techniques: string[]
  matched_keys: string[]
  unmatched_keys: string[]
  matched_techniques: string[]
  unmatched_techniques: string[]
  in_flight?: InFlightRow[]
  concluded?: ConcludedRow[]
  proposal?: HuntProposal
}

/** Matched and unmatched keys/T-IDs side by side, so a report only half
 *  covered reads as half covered rather than as covered. */
function CoverageSplit({ answer }: { answer: HuntCoverage }) {
  const rows: [string, string[]][] = [
    ['Matched', [...answer.matched_keys, ...answer.matched_techniques]],
    ['Unmatched', [...answer.unmatched_keys, ...answer.unmatched_techniques]],
  ]
  return (
    <div className="flex flex-col gap-1 text-[12px] leading-[1.5]">
      {rows.map(([label, list]) => (
        <div key={label}>
          <span className="text-tx-3">{label}: </span>
          <span className="font-mono">{list.length > 0 ? list.join(', ') : '—'}</span>
        </div>
      ))}
    </div>
  )
}

/** The `?run=` deep link WorkflowsScreen already honours; following it swaps the
 *  catalog, and this modal with it, for the run. */
function RunLink({ runId }: { runId: string }) {
  return (
    <Link to={{ search: `?run=${encodeURIComponent(runId)}` }} className="font-mono underline">
      {runId.slice(0, 8)}
    </Link>
  )
}

/** Report in, one of three answers out. Owns only the report text and the answer:
 *  the hypothesis, subjects and approve state stay in RunModal, which is why the
 *  two prefill actions hand a proposal back up rather than posting anything. Errors
 *  land in the modal's one error slot, and only the existing Run button executes. */
function CoveragePanel({ entityKeys, onError, onProposal }: {
  entityKeys: string[]
  onError: (msg: string | null) => void
  onProposal: (p: HuntProposal) => void
}) {
  const [report, setReport] = useState('')
  const [answer, setAnswer] = useState<HuntCoverage | null>(null)
  const [checking, setChecking] = useState(false)
  const [extended, setExtended] = useState<Record<string, 'sending' | 'sent'>>({})

  const canCheck = !checking && (report.trim() !== '' || entityKeys.length > 0)
  // A check can run on typed subjects alone, but an extend with nothing to say is no directive.
  const canExtend = report.trim() !== ''

  const check = async () => {
    setChecking(true)
    onError(null)
    try {
      const res = await workflowApi.checkCoverage({
        ...(report.trim() && { report: report.trim() }),
        ...(entityKeys.length > 0 && { entity_keys: entityKeys }),
      })
      setAnswer(res.data as HuntCoverage)
      setExtended({})
    } catch (e) {
      onError(errMsg(e))
      setAnswer(null) // the old answer was about a different report
    }
    setChecking(false)
  }

  // One directive of kind extend, carrying the report text, against the run that already covers it.
  const extend = (runId: string) => {
    setExtended((held) => ({ ...held, [runId]: 'sending' }))
    workflowApi.steer(runId, 'extend', report.trim())
      .then(() => setExtended((held) => ({ ...held, [runId]: 'sent' })))
      .catch((e) => {
        onError(errMsg(e))
        setExtended((held) => { const next = { ...held }; delete next[runId]; return next })
      })
  }

  const verdict = (a: HuntCoverage) => {
    switch (a.status) {
      case 'running':
        return (
          <>
            <div className="text-[12.5px] leading-[1.5]">Already being hunted. Extend a run with this report rather than starting another.</div>
            <ul className="flex flex-col gap-1.5 text-[12px] leading-[1.5]" aria-label="In-flight hunts">
              {(a.in_flight ?? []).map((row) => {
                const state = extended[row.run_id]
                return (
                  <li key={row.run_id} className="flex items-center gap-2 flex-wrap">
                    <RunLink runId={row.run_id} />
                    <span className="text-tx-3">{row.status}</span>
                    <span className="font-mono text-tx-3">{[...row.matched_keys, ...row.matched_techniques].join(', ')}</span>
                    <button
                      className="btn ghost"
                      disabled={!canExtend || state !== undefined}
                      title={canExtend ? undefined : 'Paste the report to extend with'}
                      aria-label={`Extend ${row.run_id.slice(0, 8)}`}
                      onClick={() => extend(row.run_id)}
                    >
                      {state === 'sent' ? 'Extended' : state === 'sending' ? 'Extending…' : 'Extend'}
                    </button>
                  </li>
                )
              })}
            </ul>
          </>
        )
      case 'concluded':
        return (
          <>
            <div className="text-[12.5px] leading-[1.5]">Hunted before. Reopen puts the proposal in the form below; Run starts it.</div>
            <ul className="flex flex-col gap-1.5 text-[12px] leading-[1.5]" aria-label="Concluded verdicts">
              {(a.concluded ?? []).map((row, at) => (
                <li key={at} className="flex items-center gap-2 flex-wrap">
                  {row.origin_run_id ? <RunLink runId={row.origin_run_id} /> : null}
                  <span>{row.statement}</span>
                  <span className="text-tx-3">{row.outcome} · {fmtStarted(row.concluded_at)}</span>
                </li>
              ))}
            </ul>
            {a.proposal && (
              <div><button className="btn ghost" onClick={() => onProposal(a.proposal as HuntProposal)}>Reopen</button></div>
            )}
          </>
        )
      case 'uncovered':
        return (
          <>
            <div className="text-[12.5px] leading-[1.5]">Nobody has hunted this. Use proposal fills the form below; Run starts it.</div>
            {a.proposal && (
              <div><button className="btn ghost" onClick={() => onProposal(a.proposal as HuntProposal)}>Use proposal</button></div>
            )}
          </>
        )
      default: {
        const never: never = a.status
        return never
      }
    }
  }

  return (
    <div className="flex flex-col gap-2.5 p-3 rounded border border-line">
      <Field
        label="Report"
        value={report}
        onChange={setReport}
        placeholder="Paste a threat report — STIX JSON or text with indicators and T-IDs…"
        textarea
        hint="Checks whether its indicators are already being hunted, were hunted, or are untouched. Read-only: nothing starts until you press Run."
      />
      <div className="flex justify-end">
        <button className="btn ghost" disabled={!canCheck} style={{ opacity: canCheck ? 1 : 0.5 }} onClick={check}>
          {checking ? 'Checking…' : 'Check coverage'}
        </button>
      </div>
      {answer && (
        <div className="flex flex-col gap-2" data-testid="coverage-answer" data-status={answer.status}>
          <div className="text-[11px] uppercase tracking-[0.06em] text-tx-3">Coverage · {answer.status}</div>
          <CoverageSplit answer={answer} />
          {verdict(answer)}
        </div>
      )}
    </div>
  )
}

/** Run a workflow — collects a target, starts it on the agent layer, then hands
    off to History, which reports phases, beliefs and anything the run waits on. */
export function RunModal({ wf, onStarted, onClose }: { wf: Workflow; onStarted: () => void; onClose: () => void }) {
  const [findingId, setFindingId] = useState('')
  const [caseId, setCaseId] = useState('')
  const [context, setContext] = useState('')
  const [hypothesis, setHypothesis] = useState('')
  const [subjects, setSubjects] = useState<Record<string, string>>({})
  const [approve, setApprove] = useState(false)
  const [iterations, setIterations] = useState('')
  const [maxCost, setMaxCost] = useState('')
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [startedId, setStartedId] = useState<string | null>(null) // watched in place, rather than closing
  const [limits, setLimits] = useState<WfLimits | null>(null)
  const [findingOpts, setFindingOpts] = useState<{ id: string; label: string }[]>([])
  const [caseOpts, setCaseOpts] = useState<{ id: string; label: string }[]>([])

  useEffect(() => {
    let cancelled = false
    findingsApi.getAll({ limit: 50 }).then((r) => {
      if (cancelled) return
      const list = (r.data?.findings || []) as { finding_id: string; title?: string; severity?: string }[]
      setFindingOpts(list.map((f) => ({ id: f.finding_id, label: [f.severity, f.title].filter(Boolean).join(' · ') })))
    }).catch(() => {})
    casesApi.getAll().then((r) => {
      if (cancelled) return
      setCaseOpts(
        r.data.cases
          .filter((c): c is typeof c & { case_id: string } => Boolean(c.case_id))
          .map((c) => ({ id: c.case_id, label: c.title || '' })),
      )
    }).catch(() => {})
    workflowApi.preflight(wf.id).then((r) => {
      if (!cancelled) setLimits(r.data as WfLimits)
    }).catch(() => {})
    return () => { cancelled = true }
  }, [wf.id])

  // The backend's own answer, not a list of kinds held here. A hunt-like kind gets
  // the ceilings and the unbound-tool warning. Everything else, root-cause included,
  // gets the finding, case, and context dialog.
  const isHuntLike = wf.huntLike
  const turns = Number(iterations)
  const turnsBad = iterations.trim() !== '' && (!Number.isInteger(turns) || turns < 1 || turns > 40)
  const cost = Number(maxCost)
  const costBad = maxCost.trim() !== '' && (!Number.isFinite(cost) || cost <= 0 || cost > 100)

  const params = {
    ...(findingId.trim() && { finding_id: findingId.trim() }),
    ...(caseId.trim() && { case_id: caseId.trim() }),
    ...(context.trim() && { context: context.trim() }),
    ...(hypothesis.trim() && { hypothesis: hypothesis.trim() }),
  }
  const asked = subjectsAsked(hypothesis, subjects)
  const malformed = malformedSubjects(hypothesis, subjects)
  // A turn count says how long to run, never what to run on, so it is not a target.
  const withTurns = {
    ...params,
    ...(isHuntLike && Object.keys(asked).length > 0 && { hypothesis_subjects: asked }),
    ...(isHuntLike && !turnsBad && iterations.trim() && { iterations: turns }),
    ...(isHuntLike && !costBad && maxCost.trim() && { max_cost_usd: cost }),
    ...(isHuntLike && approve && { approve_hypotheses: true }),
  }
  // Checked on Run, not per keystroke: a button dead through a sentence reads as an argument.
  const needsHypothesis = isHuntLike && hypothesis.trim() === ''
  const canRun = Object.keys(params).length > 0 && !turnsBad && !costBad && !starting

  // The proposal is an execute body already; it lands in the same three fields the
  // operator would have typed, so Run sends it through the same withTurns build.
  const takeProposal = (p: HuntProposal) => {
    setHypothesis(p.hypothesis)
    setSubjects(Object.fromEntries(Object.entries(p.hypothesis_subjects).map(([line, keys]) => [line, keys.join(', ')])))
    setApprove(p.approve_hypotheses === true)
  }

  const run = async () => {
    if (needsHypothesis) {
      setError('This run tests a claim you state. Put at least one in Hypothesis — the benign account is added for you.')
      return
    }
    // Refused rather than dropped: a subject that does not reach the run is a
    // verdict nobody can recall later, and silence about it is the worst outcome.
    if (malformed.length > 0) {
      setError(`Not an entity key: ${malformed.join(', ')}. Each subject is type:value — host:dev-830, ip:45.77.53.176.`)
      return
    }
    setStarting(true)
    setError(null)
    try {
      const res = await workflowApi.execute(wf.id, withTurns)
      const started = (res.data as { run_id?: string })?.run_id ?? null
      setStarting(false)
      if (started === null) onStarted() // nothing to confirm or link to
      else setStartedId(started)
    } catch (e) {
      setError(errMsg(e))
      setStarting(false)
    }
  }

  if (startedId !== null) {
    return (
      <Popup open onClose={onClose} title={`Run · ${wf.name}`}>
        <StartedRun runId={startedId} onView={onStarted} onClose={onClose} />
      </Popup>
    )
  }

  return (
    <Popup open onClose={onClose} title={`Run · ${wf.name}`}>
      <div className="flex flex-col gap-3.5">
        <p className="text-[12.5px] text-tx-3 leading-[1.5]">Provide at least one target, then start the run — the agents work it on the server and History reports where it got to. A finding or case gives the run something to work from, and the report comes back onto the case you pick. A run that tests beliefs takes what you state: each line of Hypothesis goes on the board as its own, and the benign explanation goes up beside them as the claim to beat.</p>
        {error && <div className="text-[12.5px] leading-[1.5]" style={{ color: 'var(--crit)' }}>{error}</div>}
        {isHuntLike && <Unpriced pricing={limits?.pricing} />}
        {isHuntLike && <Blindness unbound={limits?.capabilities?.unbound ?? []} />}
        <ComboField label="Finding ID" value={findingId} onChange={setFindingId} placeholder="f-20260614-3b5c585e" options={findingOpts} hint={findingOpts.length ? `${findingOpts.length} recent findings — start typing to filter.` : undefined} />
        <ComboField label="Case ID" value={caseId} onChange={setCaseId} placeholder="case-2026-0142" options={caseOpts} />
        <Field label="Context" value={context} onChange={setContext} placeholder="Active ransomware on HOST-42…" textarea />
        {isHuntLike && (
          <CoveragePanel entityKeys={Object.values(asked).flat()} onError={setError} onProposal={takeProposal} />
        )}
        <Field
          label="Hypothesis"
          value={hypothesis}
          onChange={setHypothesis}
          placeholder="Credentials taken from HOST-42 were reused on another host…"
          textarea
          hint={isHuntLike
            ? 'One belief per line, each a claim the run can argue against. The benign account is added for you as the claim to beat. Say what each one is about below, so its verdict can be found again by that host or address.'
            : undefined}
        />
        {isHuntLike && (
          <HypothesisPreview
            text={hypothesis}
            subjects={subjects}
            onSubject={(belief, raw) => setSubjects((held) => ({ ...held, [belief]: raw }))}
          />
        )}
        {isHuntLike && (
          <Field
            label="Iterations"
            value={iterations}
            onChange={setIterations}
            placeholder={String(limits?.budgets?.max_iterations ?? 8)}
            hint={turnsBad ? 'A whole number of turns between 1 and 40.' : turnsHint(iterations, maxCost, limits)}
          />
        )}
        {isHuntLike && (
          <Field
            label="Cost ceiling"
            value={maxCost}
            onChange={setMaxCost}
            placeholder={(limits?.budgets?.max_cost_usd ?? 15).toFixed(2)}
            hint={costBad
              ? 'A dollar amount above 0 and no more than 100.'
              : 'Dollars this run may spend before it stops and reports on what it has. The turn count above is the other ceiling; whichever it reaches first ends the run.'}
          />
        )}
        {isHuntLike && (
          <label className="flex items-start gap-2 text-[12.5px] leading-[1.5] text-tx-2 cursor-pointer">
            <input type="checkbox" className="mt-0.5" checked={approve} onChange={(e) => setApprove(e.target.checked)} />
            <span>
              Ask me before it starts. The run puts its board up and waits for approval in
              History rather than approving itself — which is what a run with nobody watching
              has to do, and why nothing asked you last time.
            </span>
          </label>
        )}
        <div className="flex justify-end gap-2.5 pt-1">
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={!canRun} style={{ opacity: canRun ? 1 : 0.5 }} onClick={run}>
            <Icon name="play" /> {starting ? 'Starting…' : 'Run workflow'}
          </button>
        </div>
      </div>
    </Popup>
  )
}

/** A secondary badge for the terminals the three-value status folds together.
 *  completed and failed already say what they are. */
const OUTCOME_BADGE: Record<string, string> = {
  budget_exhausted: 'stopped at budget',
  abandoned: 'abandoned',
  aborted: 'aborted',
}

function OutcomeBadge({ outcome, reason }: { outcome?: string | null; reason?: string | null }) {
  const label = outcome ? OUTCOME_BADGE[outcome] : undefined
  if (!label) return null
  return (
    <span className="status ml-2" style={{ background: 'transparent', color: 'var(--tx-2)', border: '1px solid var(--line)' }} title={reason || undefined}>{label}</span>
  )
}

function fmtStarted(iso?: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString()
}

export function HistoryModal({ wf, onClose }: { wf: Workflow; onClose: () => void }) {
  const [runs, setRuns] = useState<WfRun[]>([])
  const [phase, setPhase] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)
    workflowApi
      .listRuns(wf.id, { limit: 50 })
      .then((res) => {
        if (cancelled) return
        setRuns((res.data?.runs || []) as WfRun[])
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setError(errMsg(e))
        setPhase('error')
      })
    return () => { cancelled = true }
  }, [wf.id])

  useEffect(() => load(), [load])

  return (
    <Popup open onClose={onClose} title={`History · ${wf.name}`} width="min(1400px, 94vw)">
      {phase === 'loading' && <EmptyState loading compact icon="clock" title="Loading run history…" />}
      {phase === 'error' && <EmptyState error compact icon="alert" title="Couldn’t load history" body={error} primary={{ label: 'Retry', onClick: load, icon: 'refresh' }} />}
      {phase === 'ready' && runs.length === 0 && <EmptyState compact icon="clock" title="No runs yet" body="Run this workflow to capture execution history, duration, trigger, and cost." />}
      {phase === 'ready' && runs.length > 0 && (
        <div className="table-wrap">
          <table className="tbl">
            <thead><tr><th /><th>Status</th><th>Started</th><th>Duration</th><th>Trigger</th><th>Cost</th><th /></tr></thead>
            <tbody>
              {runs.map((r) => <RunRow key={r.run_id} run={r} onRemoved={load} onWatch={onClose} />)}
            </tbody>
          </table>
        </div>
      )}
    </Popup>
  )
}

/** One step of a root-cause trace as last written. A name on a step that is not
 *  proven arrives already as [unlinked]. */
interface RootCauseStep {
  step_id: string
  event: string
  who: string
  at: string
  link: string
  artifact: string
  cause_id: string | null
  origin: boolean
  link_status: string
  origin_status: string
  proven: boolean
}
interface RootCauseSearch {
  tool: string
  args: string
  rows: number
  failed: boolean
}
/** A root-cause run's projection. `recent_searches` is capped; `searches` is the total. */
interface RootCauseView {
  run_kind: 'root_cause'
  status: string
  cost_usd: number | null
  max_cost_usd: number | null
  steps: RootCauseStep[]
  proven: number
  notices: string[]
  searches: number
  recent_searches: RootCauseSearch[]
}

function rootCauseOf(projection: unknown): RootCauseView | null {
  if (typeof projection !== 'object' || projection === null) return null
  const view = projection as Partial<RootCauseView>
  if (view.run_kind !== 'root_cause' || !Array.isArray(view.steps)) return null
  return {
    run_kind: 'root_cause',
    status: typeof view.status === 'string' ? view.status : '',
    cost_usd: typeof view.cost_usd === 'number' ? view.cost_usd : null,
    max_cost_usd: typeof view.max_cost_usd === 'number' ? view.max_cost_usd : null,
    steps: view.steps,
    proven: typeof view.proven === 'number' ? view.proven : 0,
    notices: Array.isArray(view.notices) ? view.notices : [],
    searches: typeof view.searches === 'number' ? view.searches : 0,
    recent_searches: Array.isArray(view.recent_searches) ? view.recent_searches : [],
  }
}

/** Takes a finished run out of History. Two clicks rather than a browser confirm,
 *  since the row is one of fifty. A run in flight is ended with cancel, not this. */
function RemoveRun({ run, onRemoved }: { run: WfRun; onRemoved: () => void }) {
  const [asked, setAsked] = useState(false)
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)

  if (IN_FLIGHT.includes(run.status)) return null

  const remove = () => {
    setBusy(true)
    setFailed(null)
    workflowApi
      .deleteRun(run.run_id)
      .then(() => onRemoved())
      .catch((e) => { setFailed(errMsg(e)); setBusy(false); setAsked(false) })
  }

  if (failed !== null) return <span className="text-[11px]" style={{ color: 'var(--crit)' }} title={failed}>failed</span>
  if (!asked) {
    return (
      <button className="btn ghost icon" title="Remove this run from History" onClick={() => setAsked(true)}>
        <Icon name="trash" size={13} />
      </button>
    )
  }
  return (
    <span className="flex gap-1.5 items-center">
      <button className="btn ghost text-[11px]" disabled={busy} onClick={remove}>{busy ? 'removing…' : 'remove'}</button>
      <button className="btn ghost text-[11px]" disabled={busy} onClick={() => setAsked(false)}>keep</button>
    </span>
  )
}

/** A run row that lazily fetches its full detail (getRun) when expanded. */
function RunRow({ run, onRemoved, onWatch }: { run: WfRun; onRemoved: () => void; onWatch: () => void }) {
  const [open, setOpen] = useState(false)
  const { detail, dphase, setDphase, load } = useRunDetail(run.run_id, open, run.status)

  const toggle = () => {
    const next = !open
    setOpen(next)
    if (next && dphase === 'idle') {
      setDphase('loading')
      void load()
    }
  }

  return (
    <>
      <tr className="clickable" onClick={toggle}>
        <td style={{ width: 24 }}><span className="caret" style={{ transform: open ? 'rotate(90deg)' : 'none' }}><Icon name="chevR" size={13} /></span></td>
        <td>
          <span className="status" style={{ background: 'transparent', color: runStatusColor(run.status), border: `1px solid ${runStatusColor(run.status)}55` }}>{run.status}</span>
          <OutcomeBadge outcome={run.outcome} reason={run.reason} />
          {run.error && <span className="ml-2" style={{ color: 'var(--crit)' }} title={run.error}>⚠</span>}
        </td>
        <td className="muted">{fmtStarted(run.started_at)}</td>
        <td className="muted">{fmtDuration(run.duration_ms)}</td>
        <td className="muted">{run.triggered_by || '—'}</td>
        <td className="muted"><Cost usd={run.total_cost_usd} digits={3} /></td>
        <td className="tight" onClick={(e) => e.stopPropagation()}>
          <span className="flex items-center gap-1.5 justify-end">
            {/* closes History as it navigates; the cell above keeps the row from expanding */}
            <Link to={{ search: `?run=${encodeURIComponent(run.run_id)}` }} className="btn ghost no-underline" onClick={onWatch}>
              <Icon name="play" size={13} /> Watch it run
            </Link>
            <RemoveRun run={run} onRemoved={onRemoved} />
          </span>
        </td>
      </tr>
      {open && (
        <tr className="run-detail-row">
          <td colSpan={7}>
            {dphase === 'loading' && <div className="muted" style={{ padding: '10px 4px' }}>Loading run detail…</div>}
            {dphase === 'error' && <div className="muted" style={{ padding: '10px 4px' }}>Couldn’t load run detail.</div>}
            {dphase === 'ready' && detail && <RunDetail d={detail} onSteered={load} />}
          </td>
        </tr>
      )}
    </>
  )
}

/** A hunt that hit its own ceiling and waits to be told what to do next. It is not
 *  a checkpoint and not an ending, so nothing else on the panel reports it. */
function Parked({ hunt }: { hunt: HuntView }) {
  if (hunt.status !== 'parked' || hunt.open_checkpoint) return null
  return (
    <div className="text-[12.5px] leading-[1.5] mt-2" style={{ color: 'var(--high)' }}>
      Stopped and waiting: {hunt.reason || 'the hunt reached one of its own ceilings'}. It keeps
      everything it has found: use <b>Keep going</b> below to let it carry on from here, or
      conclude to have it write up what it has.
    </div>
  )
}

/** H1, H2 … in board order, so a table of records reads as beliefs rather than
 *  hashes. The id stays on the element's title, since the ledger and report use it. */
const HypLabels = createContext<ReadonlyMap<string, string>>(new Map())

function labelsOf(hypotheses: readonly HuntStanding[]): ReadonlyMap<string, string> {
  return new Map(hypotheses.map((h, at) => [h.hypothesis_id, `H${at + 1}`]))
}

/** Falls back to the id rather than hiding a reference the board does not hold: a
 *  link to a hypothesis this projection never carried is worth seeing, not eliding. */
function Hyp({ id }: { id?: string | null }) {
  const labels = useContext(HypLabels)
  if (!id) return <span className="muted">unattributed</span>
  return <span className="hyp-ref" title={id}>{labels.get(id) ?? id}</span>
}

/** What the hunt is asking someone to do — the one thing here somebody acts on. */
function HuntActions({ hunt }: { hunt: HuntView }) {
  const steps = hunt.narrative?.next_steps ?? []
  if (steps.length === 0) return null
  return (
    <div className="hunt-actions">
      <div className="hunt-actions-head">What to do now</div>
      <ol className="hunt-actions-list">
        {steps.map((step, at) => (
          <li key={at}>
            <span className="hunt-actions-n">{at + 1}</span>
            <span>{step}</span>
          </li>
        ))}
      </ol>
    </div>
  )
}

/** Exported for test: the run detail panel is the whole of what a hunt shows an
 *  operator, and driving the screen down to it would test the History modal instead. */
export function RunDetail({ d, onSteered }: { d: WfRunDetail; onSteered: () => void }) {
  const hunt = d.hunt ?? null
  const trace = hunt === null ? rootCauseOf(d.projection) : null
  // A hunt already answers its wait through OpenCheckpoint. A phase gate is an
  // approval row, and only a phase-walking run has one.
  const phaseGated = hunt === null && (d.phases ?? []).some((p) => p.status === 'pending_approval')
  return (
    <div className="run-detail">
      <RunBar d={d} hunt={hunt} trace={trace} onSteered={onSteered} />
      {hunt && <OpenCheckpoint hunt={hunt} />}
      {phaseGated && <PhaseGate runId={d.run_id} onAnswered={onSteered} />}
      {hunt && <Parked hunt={hunt} />}
      {hunt?.reason && !IN_FLIGHT.includes(d.status) && (
        <div className="muted text-[12px] leading-[1.5] mt-2">Why it ended: {hunt.reason}</div>
      )}
      {d.error && d.error !== hunt?.reason && (
        <div className="modal-section">
          <h4 style={{ color: 'var(--crit)' }}>Error</h4>
          <pre className="font-mono text-[11.5px] leading-[1.5] whitespace-pre-wrap m-0" style={{ color: 'var(--crit)' }}>{d.error}</pre>
        </div>
      )}
      {trace && <RootCauseTrace trace={trace} />}
      {hunt ? <HuntTabs d={d} hunt={hunt} onReload={onSteered} /> : (
        <RunWithoutHunt d={d} inFlight={IN_FLIGHT.includes(d.status)} />
      )}
      {IN_FLIGHT.includes(d.status) && <Steer runId={d.run_id} hunt={hunt !== null} onSteered={onSteered} />}
    </div>
  )
}

/** What the run is doing, and Stop. It sits with the status rather than among the
 *  steering directives, which are only notes the lead reads at its next turn. */
function RunBar({ d, hunt, trace, onSteered }: { d: WfRunDetail; hunt: HuntView | null; trace: RootCauseView | null; onSteered: () => void }) {
  // The row's total is written only when the run ends; a projection prices it as it goes.
  const cost = hunt?.cost_usd ?? trace?.cost_usd ?? d.total_cost_usd
  const budgets = hunt?.budgets
  const ceiling = budgets?.max_cost_usd ?? trace?.max_cost_usd ?? undefined
  const spent = typeof cost === 'number' && ceiling !== undefined && ceiling > 0
    ? Math.min(100, (cost / ceiling) * 100)
    : null
  return (
    <div className="run-bar">
      <span className="pill" style={{ color: runStatusColor(d.status) }}><span className="dot" />{d.status}</span>
      {hunt?.outcome && !IN_FLIGHT.includes(d.status) && (
        <span className="muted text-[11.5px]" title={hunt.reason ?? undefined}>{hunt.outcome}</span>
      )}
      {!hunt && <OutcomeBadge outcome={d.outcome} reason={d.reason} />}
      <span className="mono text-[11.5px] text-tx-3">{d.run_id.slice(0, 13)}</span>
      <span className="flex-1" />
      <div className="meta">
        {hunt && <span>Iteration <b>{hunt.iteration}</b>{budgets && ` of ${budgets.max_iterations}`}</span>}
        <span><b><Cost usd={cost} /></b>{typeof cost === 'number' && ceiling !== undefined && ` of $${ceiling.toFixed(2)}`}</span>
        {spent !== null && (
          <div className="budget-track" title={`${spent.toFixed(0)}% of the cost ceiling`}>
            <div className="budget-fill" style={{ width: `${spent}%` }} />
          </div>
        )}
      </div>
      {IN_FLIGHT.includes(d.status) && (
        <>
          <span className="sep" />
          <StopRun runId={d.run_id} onStopped={onSteered} />
        </>
      )}
    </div>
  )
}

/** What a root-cause trace has recorded and asked so far, off the same poll as the
 *  run. The checked summary below it is still the report; this is how it got there. */
function RootCauseTrace({ trace }: { trace: RootCauseView }) {
  const steps = trace.steps.length
  return (
    <div className="modal-section">
      <h4>Trace</h4>
      <div className="muted text-[11.5px] mb-2">
        {steps} {steps === 1 ? 'step' : 'steps'} · {trace.proven} proven · {trace.searches} {trace.searches === 1 ? 'search' : 'searches'}
      </div>
      {trace.notices.map((text) => (
        <div key={text} className="muted text-[12px] leading-[1.5] mb-2">{text}</div>
      ))}
      {steps === 0 ? (
        <div className="muted text-[12.5px]">No step recorded yet.</div>
      ) : (
        <div className="table-wrap">
          <table className="tbl">
            <thead><tr><th>Step</th><th>Event</th><th>Tied by</th><th>Proof</th></tr></thead>
            <tbody>
              {trace.steps.map((s) => (
                <tr key={s.step_id}>
                  <td className="mono tight" style={{ fontSize: 11 }}>{s.step_id}</td>
                  <td>
                    {s.event}
                    <div className="muted text-[11px]">{s.at}{s.who && ` · ${s.who}`}</div>
                  </td>
                  <td className="muted">{tiedBy(s)}</td>
                  <td className="tight"><span style={{ color: s.proven ? 'var(--ok)' : 'var(--med)' }}>{s.proven ? 'proven' : 'unproven'}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {trace.recent_searches.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <h4>Latest searches</h4>
          <ul className="text-[12px] mt-1 mb-0" style={{ paddingLeft: 18 }}>
            {trace.recent_searches.map((q, at) => (
              <li key={at}>
                <span className="font-mono">{q.args || q.tool}</span>{' '}
                <span className="muted">{q.failed ? 'failed' : `${q.rows} ${q.rows === 1 ? 'row' : 'rows'}`}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

/** What a step claims ties it into the trace, and where that claim stands. */
function tiedBy(s: RootCauseStep): string {
  const claims: string[] = []
  if (s.cause_id) claims.push(`link ${s.link || '—'} to ${s.cause_id}: ${s.link_status}`)
  if (s.origin) claims.push(`origin: ${s.origin_status}`)
  return claims.join(' · ') || 'nothing yet'
}

/** Ending a run cannot be undone, so it asks first. Goes through cancel, not steer:
 *  a queued abort needs a live worker, and cancel escalates behind one that cannot. */
function StopRun({ runId, onStopped }: { runId: string; onStopped: () => void }) {
  const [asking, setAsking] = useState(false)
  const [busy, setBusy] = useState(false)
  const [said, setSaid] = useState<string | null>(null)

  const stop = () => {
    setBusy(true)
    workflowApi
      .cancelRun(runId, 'stopped from the console')
      .then(() => { setSaid('stopping — it settles itself if it can'); onStopped() })
      .catch((e) => setSaid(errMsg(e)))
      .finally(() => setBusy(false))
  }

  if (said !== null) return <span className="muted text-[11.5px]">{said}</span>
  if (!asking) {
    return (
      <button className="btn danger" onClick={() => setAsking(true)}>
        <Icon name="stop" size={13} /> Stop
      </button>
    )
  }
  return (
    <div className="flex items-center gap-2 flex-wrap">
      <span className="text-[11.5px] text-tx-2">Stop this run? It cannot be resumed.</span>
      <button className="btn danger solid" disabled={busy} onClick={stop}>
        {busy ? 'Stopping…' : 'Confirm'}
      </button>
      <button className="btn ghost" disabled={busy} onClick={() => setAsking(false)}>Cancel</button>
    </div>
  )
}

type HuntTab = 'memory' | 'hyp' | 'evidence' | 'moves' | 'frontier' | 'gaps' | 'esc' | 'report' | 'next'

/** Views of one run as tabs rather than stacked tables. A view with nothing in it
 *  is not offered rather than offered empty. */
function HuntTabs({ d, hunt, onReload }: { d: WfRunDetail; hunt: HuntView; onReload: () => void }) {
  const found = hunt.evidence ?? []
  // Live, not only from the finalized report, so gaps are visible mid-run.
  const gaps = hunt.report?.gaps ?? found.filter((one) => one.is_gap).map(liveGap)
  const checkpoints = hunt.report?.checkpoints ?? []
  const handoffs = hunt.handoffs ?? []
  const report = hunt.report_markdown ?? d.result_summary ?? ''
  const supervision = handoffs.length + checkpoints.length
  const moves = hunt.moves ?? []
  const frontier = hunt.open_questions ?? []
  const steps = hunt.narrative?.next_steps ?? []
  const memory = hunt.recall ?? null
  const remembered = memory === null ? null : recalledCount(memory)
  const tabs: [HuntTab, string, number | null][] = [
    ['hyp', 'Hypotheses', hunt.hypotheses.length],
    ...(hunt.evidence_count > 0 ? ([['evidence', 'Evidence', hunt.evidence_count]] as [HuntTab, string, number][]) : []),
    // After what this run gathered, because it is not one of that: memory is what
    // earlier investigations left behind, and reading it first invites it to be
    // taken for a finding of this hunt's own. Absent entirely when the run never
    // asked, so an older run does not grow an empty tab.
    ...(memory !== null ? ([['memory', 'Memory', remembered]] as [HuntTab, string, number | null][]) : []),
    ...(moves.length > 0 ? ([['moves', 'Moves', moves.length]] as [HuntTab, string, number][]) : []),
    ...(frontier.length > 0 ? ([['frontier', 'Frontier', frontier.length]] as [HuntTab, string, number][]) : []),
    ...(gaps.length > 0 ? ([['gaps', 'Gaps', gaps.length]] as [HuntTab, string, number][]) : []),
    ...(supervision > 0 ? ([['esc', 'Escalations & checkpoints', supervision]] as [HuntTab, string, number][]) : []),
    ['report', 'Report', null],
    ...(steps.length > 0 ? ([['next', 'Next steps', steps.length]] as [HuntTab, string, number][]) : []),
  ]
  const [tab, setTab] = useState<HuntTab>(report === '' ? 'hyp' : 'report') // at mount, so a poll cannot move it
  const shown = tabs.some(([k]) => k === tab) ? tab : 'hyp'

  return (
    <HypLabels.Provider value={labelsOf(hunt.hypotheses)}>
      <div className="detail-tabs" role="tablist" aria-label="Hunt views">
        {tabs.map(([k, label, count]) => (
          <button key={k} role="tab" aria-selected={shown === k} className={`tab${shown === k ? ' active' : ''}`} onClick={() => setTab(k)}>
            {label}{count !== null && <span className="mono text-[10.5px] text-tx-3 ml-1.5">{count}</span>}
          </button>
        ))}
      </div>
      {shown === 'memory' && memory !== null && <HuntMemory recall={memory} />}
      {shown === 'hyp' && <HuntStandings hunt={hunt} />}
      {shown === 'evidence' && <HuntEvidenceTable found={found} total={hunt.evidence_count} />}
      {shown === 'moves' && <HuntMoves runId={d.run_id} moves={moves} />}
      {shown === 'frontier' && <HuntFrontier runId={d.run_id} frontier={frontier} inFlight={IN_FLIGHT.includes(d.status)} />}
      {shown === 'gaps' && <HuntGaps gaps={gaps} />}
      {shown === 'esc' && (
        <>
          <HuntEscalations handoffs={handoffs} />
          <HuntCheckpoints checkpoints={checkpoints} />
        </>
      )}
      {shown === 'report' && (report === ''
        ? <div className="muted text-[12.5px] py-3">The report is written when the hunt reaches a terminal state — completed, cancelled, or stopped at its budget.</div>
        : (
          <HuntAccount
            hunt={hunt}
            report={report}
            gaps={gaps.length}
            onGo={setTab}
            // A run still going writes its own account when it ends, so rewriting one
            // now buys a page about to be replaced.
            rewrite={IN_FLIGHT.includes(d.status) ? null : { runId: d.run_id, onRewritten: onReload }}
          />
        ))}
      {shown === 'next' && <HuntActions hunt={hunt} />}
    </HypLabels.Provider>
  )
}

/** The account, laid out — what happened, with the tabs carrying the evidence and
 *  metadata the report also lists. The markdown itself is untouched. */
function HuntAccount({ hunt, report, gaps, onGo, rewrite }: { hunt: HuntView; report: string; gaps: number; onGo: (tab: HuntTab) => void; rewrite: RewriteTarget | null }) {
  const account = hunt.narrative ?? null
  if (account === null) { // no narrative written: show the whole report rather than nothing
    return (
      <>
        <div className="hunt-account-foot" style={{ marginTop: 0, borderTop: 'none', paddingTop: 0 }}>
          <span className="muted text-[11.5px]">No account was written for this run.</span>
          <span className="flex-1" />
          {rewrite && <Rewrite target={rewrite} label="write the account" />}
          <CopyReport md={report} />
        </div>
        <ReportBody md={withoutHeader(report)} />
      </>
    )
  }

  return (
    <div className="hunt-account">
      <p className="hunt-lede">{account.summary}</p>
      <VerdictStrip hunt={hunt} onGo={onGo} />
      <OpenedOn hunt={hunt} onGo={onGo} />
      <Incidents md={account.what_happened} />
      <div className="hunt-account-foot">
        <span className="muted text-[11.5px]">
          {hunt.evidence_count} record(s) gathered · {gaps} blind spot(s)
        </span>
        <button className="btn ghost text-[11px]" onClick={() => onGo('evidence')}>Evidence ▸</button>
        {gaps > 0 && <button className="btn ghost text-[11px]" onClick={() => onGo('gaps')}>Gaps ▸</button>}
        <span className="flex-1" />
        {rewrite && <Rewrite target={rewrite} label="rewrite" />}
        <CopyReport md={report} />
      </div>
      <div className="muted text-[11px] mt-2">
        Written from the ledger by {account.model_id}. The tabs above are the hunt's own record; this is an account of it.
      </div>
    </div>
  )
}

/** Drops the report's metadata header, which the run bar above already shows. The
 *  markdown the copy control hands over keeps it. */
function withoutHeader(md: string): string {
  const at = md.indexOf('\n## ')
  return at === -1 ? md : md.slice(at + 1)
}

/** The account writes what happened as a section per incident; split on the headings
 *  it already put there rather than rendering one continuous wall. */
function Incidents({ md }: { md: string }) {
  const parts = md.split(/^### /m).map((part) => part.trim()).filter((part) => part !== '')
  if (!md.trimStart().startsWith('### ') || parts.length < 2) return <ReportBody md={md} /> // one story, told whole

  return (
    <div className="incidents">
      {parts.map((part, at) => {
        const brk = part.indexOf('\n')
        const title = (brk === -1 ? part : part.slice(0, brk)).trim()
        const body = brk === -1 ? '' : part.slice(brk + 1).trim()
        return (
          <section className="incident" key={at}>
            <h4 className="incident-h">
              <span className="incident-n">{String(at + 1).padStart(2, '0')}</span>
              <span>{title}</span>
            </h4>
            {body !== '' && <ReportBody md={body} />}
          </section>
        )
      })}
    </div>
  )
}

/** One line on the account: what the run started from. The account says what
 *  happened; the markdown carries the same fact, but this branch renders the
 *  narrative rather than the report, so without this the read is invisible here. */
function OpenedOn({ hunt, onGo }: { hunt: HuntView; onGo: (tab: HuntTab) => void }) {
  const recall = hunt.recall ?? null
  if (recall === null) return null
  const asked = recall.keys.length === 0 ? 'no entities' : recall.keys.join(', ')
  const found = recalledCount(recall)

  return (
    <div className="muted text-[11.5px]" style={{ marginTop: 8 }}>
      {found === null
        ? <>Episodic memory could not be read, so this hunt opened on nothing.</>
        : found === 0
          ? <>No earlier investigation had looked at <span className="mono">{asked}</span>.</>
          : <>Opened on {found} record{found === 1 ? '' : 's'} earlier investigations left about <span className="mono">{asked}</span>.</>}
      {' '}
      <button className="btn ghost text-[11px]" onClick={() => onGo('memory')}>Memory ▸</button>
    </div>
  )
}

/** Where the beliefs landed, grouped by standing — a chip per belief would restate
 *  the board rather than report a verdict. */
function VerdictStrip({ hunt, onGo }: { hunt: HuntView; onGo: (tab: HuntTab) => void }) {
  if (hunt.hypotheses.length === 0) return null
  const asked = hunt.hypotheses.filter((h) => h.provenance !== 'base_rate') // the loop's own claim, not the operator's
  if (asked.length === 0) return null
  const byStatus = new Map<string, number>()
  for (const h of asked) byStatus.set(h.status, (byStatus.get(h.status) ?? 0) + 1)

  return (
    <div className="verdict-strip">
      {[...byStatus].map(([status, n]) => (
        <span key={status} className="verdict-chip">
          <span className="dot" style={{ background: hypothesisColor(status) }} />
          <b>{n}</b>
          <span style={{ color: hypothesisColor(status) }}>{status}</span>
        </span>
      ))}
      <button className="btn ghost text-[11px]" onClick={() => onGo('hyp')}>
        {asked.length} belief{asked.length === 1 ? '' : 's'} tested ▸
      </button>
    </div>
  )
}

/** The markdown is the deliverable; not rendering it inline is not taking it away. */
interface RewriteTarget { runId: string; onRewritten: () => void }

/** Another pass over the same ledger. One press is a whole model call over the run's
 *  record, so the button is the only thing stopping two: nothing on the server refuses
 *  a second while the first is still writing. The answer comes back on the response,
 *  but the reload is what renders it — the ledger, not this call, is the account. */
function Rewrite({ target, label }: { target: RewriteTarget; label: string }) {
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)

  const ask = () => {
    setBusy(true)
    setFailed(null)
    workflowApi
      .narrateRun(target.runId)
      .then(() => { setBusy(false); target.onRewritten() })
      .catch((e) => { setFailed(errMsg(e)); setBusy(false) })
  }

  return (
    <span className="flex gap-1.5 items-center">
      {/* The reason, not "failed": a timeout here means it is still being written. */}
      {failed !== null && (
        <span className="text-[11px] max-w-[320px] truncate" style={{ color: 'var(--crit)' }} title={failed}>{failed}</span>
      )}
      <button className="btn ghost text-[11px]" disabled={busy} onClick={ask}>
        {busy ? 'writing…' : label}
      </button>
    </span>
  )
}

function CopyReport({ md }: { md: string }) {
  const [said, setSaid] = useState(false)
  const copy = () => {
    void navigator.clipboard?.writeText(md).then(() => {
      setSaid(true)
      setTimeout(() => setSaid(false), 1600)
    })
  }
  return (
    <button className="btn ghost text-[11px]" onClick={copy}>{said ? 'copied' : 'copy full report'}</button>
  )
}

/** A gap the projection reports live. query_intent belongs to the dispatch, which
 *  the finalized report joins in and a live read cannot, so the summary carries it. */
function liveGap(one: HuntEvidence): HuntGap {
  return {
    evidence_id: one.evidence_id,
    iteration: one.iteration,
    summary: one.summary,
    hypothesis_id: one.bears_on?.[0]?.hypothesis_id ?? null,
  }
}

/** The leads waiting to be taken. Pinning is queued like every other directive, so
 *  the row says the ask was sent rather than that the hunt obeyed. */
function HuntFrontier({ runId, frontier, inFlight }: { runId: string; frontier: HuntQuestion[]; inFlight: boolean }) {
  const [pinned, setPinned] = useState<Record<string, string>>({})
  const pin = (questionId: string) => {
    setPinned((held) => ({ ...held, [questionId]: 'sending' }))
    workflowApi
      .steer(runId, 'boost', '', { question_id: questionId })
      .then(() => setPinned((held) => ({ ...held, [questionId]: 'pinned for the next turn' })))
      .catch((e) => setPinned((held) => ({ ...held, [questionId]: errMsg(e) })))
  }
  return (
    <div style={{ marginTop: 12 }}>
      <div className="muted text-[11.5px] mb-2">Open leads, in the order a worker would take them.</div>
      <div className="table-wrap">
        <table className="tbl">
          <thead><tr><th className="tight">Opened</th><th>Lead</th><th className="tight">Bears on</th><th className="tight" /></tr></thead>
          <tbody>
            {frontier.map((q) => (
              <tr key={q.question_id}>
                <td className="muted tight">{q.spawned_iteration ?? '—'}</td>
                <td>
                  {q.question}
                  {q.entity_key && <div className="muted mono text-[11px]">{q.entity_key}</div>}
                </td>
                <td className="muted tight"><Hyp id={q.hypothesis_id} /></td>
                <td className="tight">
                  {pinned[q.question_id]
                    ? <span className="muted text-[11px]">{pinned[q.question_id]}</span>
                    : inFlight && <button className="btn ghost" onClick={() => pin(q.question_id)} title="Ask the lead to take this one next.">take next</button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

/** Why an emission was refused, without the emission. The rejection carries the
 *  validator's complaint and then the whole payload it complained about — hundreds of
 *  characters of the model's own prose, cut off mid-word, in a table cell. The
 *  complaint names the field; the payload is the digest the lead already reads. */
export function refusalReason(rejection: string): string {
  const payloadAt = rejection.indexOf('{')
  const complaint = (payloadAt === -1 ? rejection : rejection.slice(0, payloadAt)).replace(/[:\s]+$/, '')
  if (complaint === '') return 'the emission did not match the schema'
  return complaint.length > 160 ? `${complaint.slice(0, 160)}…` : complaint
}

/** One move opened for its digest. Replay folds the whole ledger, so it is asked for
 *  on the click and held here; the poll that refreshes `moves` never touches it. */
interface OpenedMove { id: string; report: ReplayReport | null; failed: string | null }

/** Every move the lead made and why — the only account of what a turn decided, and
 *  of a turn that stalled. Choosing one shows what the lead was looking at when it
 *  decided. */
function HuntMoves({ runId, moves }: { runId: string; moves: HuntMove[] }) {
  const [opened, setOpened] = useState<OpenedMove | null>(null)
  const pick = (decisionId: string) => {
    if (opened?.id === decisionId) { setOpened(null); return }
    setOpened({ id: decisionId, report: null, failed: null })
    workflowApi
      .getReplay(runId, decisionId)
      .then((r) => setOpened((held) => (held?.id === decisionId ? { ...held, report: r.data } : held)))
      .catch((e) => setOpened((held) => (held?.id === decisionId ? { ...held, failed: errMsg(e) } : held)))
  }
  return (
    <div style={{ marginTop: 12 }}>
      <div className="muted text-[11.5px] mb-2">Newest first. One decision per turn; a turn may re-ask after a refused emission. Choose a move to see what the lead was shown.</div>
      <div className="table-wrap">
        <table className="tbl">
          <thead><tr><th className="tight">Turn</th><th className="tight">Move</th><th>Why</th><th className="tight">On</th></tr></thead>
          <tbody>
            {moves.map((m) => (
              <Fragment key={m.decision_id}>
                {/* The row takes the click; the action is the control a keyboard reaches,
                    so the table keeps its own semantics rather than posing as a button. */}
                <tr className={`clickable${opened?.id === m.decision_id ? ' sel' : ''}`} onClick={() => pick(m.decision_id)}>
                  <td className="muted tight">{m.iteration}</td>
                  <td className="tight">
                    <button
                      className="btn ghost mono text-[11px]"
                      aria-expanded={opened?.id === m.decision_id}
                      title="Show what the lead was looking at when it decided this."
                      onClick={(e) => { e.stopPropagation(); pick(m.decision_id) }}
                    >
                      {m.action}
                    </button>
                  </td>
                  <td>
                    {m.rationale}
                    {m.query_intent && <div className="muted text-[11px] mt-0.5">asked: {m.query_intent}</div>}
                    {/* The entity and the worker live here rather than in On: an ip or a
                        role name in a tight column wrapped a character to a line. */}
                    {(m.target_entity || m.worker_agent_id) && (
                      <div className="flex gap-1.5 flex-wrap mt-1">
                        {m.target_entity && <span className="chip mono" style={{ fontSize: 10 }}>{m.target_entity}</span>}
                        {m.worker_agent_id && <span className="chip" style={{ fontSize: 10 }}>{m.worker_agent_id}</span>}
                      </div>
                    )}
                    {!!m.rejected_attempts?.length && (
                      <div className="text-[11px] mt-1" style={{ color: 'var(--high)' }}>
                        {m.rejected_attempts.length} emission(s) refused first — {refusalReason(m.rejected_attempts[0]!)}
                      </div>
                    )}
                  </td>
                  <td className="tight"><Hyp id={m.target_hypothesis_id} /></td>
                </tr>
                {opened?.id === m.decision_id && (
                  <tr>
                    <td colSpan={4} style={{ background: 'var(--bg-2)' }}><MoveDigest opened={opened} /></td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

/** The digest one decision was shown, as the record has it, and whether folding the
 *  ledger again reproduces it. The recorded half is rendered — that is what the lead
 *  read — and the rebuilt half is what the mismatch line speaks for. */
function MoveDigest({ opened }: { opened: OpenedMove }) {
  if (opened.failed !== null) {
    return <div className="text-[12px] py-1" style={{ color: 'var(--high)' }}>Could not read what this move was shown — {opened.failed}</div>
  }
  const decision = opened.report?.decisions[0]
  if (opened.report === null) return <div className="muted text-[12px] py-1">Rebuilding the digest from the ledger…</div>
  if (decision === undefined) return <div className="muted text-[12px] py-1">Replay returned nothing for this decision.</div>

  const seen = decision.recorded
  const recalled = opened.report.recalled
  return (
    <div className="py-1" style={{ whiteSpace: 'normal' }}>
      <div className="flex gap-1.5 items-center flex-wrap mb-2">
        <h4 style={{ margin: 0 }}>What the lead was shown at turn {decision.iteration}</h4>
        {decision.mismatch === null
          ? <span className="chip sel" style={{ fontSize: 10 }}>rebuild matches the record</span>
          : <span className="chip" style={{ fontSize: 10, color: 'var(--high)' }}>{decision.mismatch}</span>}
        {!decision.exact && (
          <span className="muted text-[11px]">prefix inferred — the ledger predates digest_seq, so a difference may be the boundary rather than drift</span>
        )}
      </div>

      <div className="text-[12.5px]">{seen.narrative || <span className="muted">No narrative was in the digest.</span>}</div>
      <div className="muted text-[11px] mt-1">
        focus {seen.focus.entity ? <span className="mono">{seen.focus.entity}</span> : 'no entity'} · <Hyp id={seen.focus.hypothesis} />
        {' '}· {seen.budget_remaining.iterations} turn(s) and ${seen.budget_remaining.cost_usd.toFixed(2)} left
      </div>

      {seen.hypotheses.length > 0 && (
        <div style={{ marginTop: 10 }}>
          <h4>Beliefs as they stood ({seen.hypotheses.length})</h4>
          <div className="table-wrap">
            <table className="tbl">
              <tbody>
                {seen.hypotheses.map((h) => (
                  <tr key={h.hypothesis_id}>
                    <td className="tight"><Hyp id={h.hypothesis_id} /></td>
                    <td>{h.statement}</td>
                    <td className="tight" style={{ color: hypothesisColor(h.status) }}>{h.status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {(seen.recent_evidence.length > 0 || seen.omitted.count > 0) && (
        <div style={{ marginTop: 10 }}>
          <h4>Recent evidence ({seen.recent_evidence.length}{seen.omitted.count > 0 && `, ${seen.omitted.count} routine omitted`})</h4>
          {seen.recent_evidence.length === 0
            ? <div className="muted text-[12px]">Every record in the window was routine; the lead saw only the count.</div>
            : (
              <div className="table-wrap">
                <table className="tbl">
                  <tbody>
                    {seen.recent_evidence.map((one) => (
                      <tr key={one.evidence_id}>
                        <td className="muted tight">{one.source_system || '—'}</td>
                        <td>
                          {one.summary}
                          {one.why_notable && <div className="muted text-[11px]">{one.why_notable}</div>}
                          <div className="text-[11px] mt-0.5 flex gap-2 flex-wrap">
                            <span className="muted">{one.salience}</span>
                            {one.instruction_like && <span style={{ color: 'var(--crit)' }}>reads as instruction</span>}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
        </div>
      )}

      {seen.open_questions.length > 0 && <DigestList title="Open questions" rows={seen.open_questions} />}
      {seen.directives.length > 0 && <DigestList title="Operator directives" rows={seen.directives} />}
      {seen.notes.length > 0 && <DigestList title="Notes" rows={seen.notes} />}

      <div style={{ marginTop: 10 }}>
        <h4>Recalled from earlier investigations ({recalled.length})</h4>
        <div className="muted text-[11.5px] mb-1">
          Read off the run's own recall event — the record it opened on, not a live re-read of memory.
        </div>
        {recalled.length === 0
          ? <div className="muted text-[12px]">Nothing recalled: the run never read memory, or the read could not be served.</div>
          : <ul className="text-[12px]" style={{ paddingLeft: 18, margin: 0 }}>{recalled.map((row, at) => <li key={at}>{row}</li>)}</ul>}
      </div>
    </div>
  )
}

function DigestList({ title, rows }: { title: string; rows: string[] }) {
  return (
    <div style={{ marginTop: 10 }}>
      <h4>{title} ({rows.length})</h4>
      <ul className="text-[12px]" style={{ paddingLeft: 18, margin: 0 }}>{rows.map((row, at) => <li key={at}>{row}</li>)}</ul>
    </div>
  )
}

/** The lead rules every observation against every active belief, so most rulings are
 *  `neither`. Those stay on the ledger rather than filling a cell. */
type Bears = NonNullable<HuntEvidence['bears_on']>

function bearing(links: HuntEvidence['bears_on']): { shown: Bears; ruledOut: number } {
  const all: Bears = links ?? []
  const shown = all.filter((link) => link.relation !== 'neither')
  return { shown, ruledOut: all.length - shown.length }
}

/** Grouped by relation, not one line per link: a record weakening three beliefs
 *  printed "weakens" three times down a narrow column and made the row taller than
 *  the summary it belongs to. The relation is the fact; the beliefs are a list. */
function BearsOn({ links }: { links: HuntEvidence['bears_on'] }) {
  const { shown, ruledOut } = bearing(links)
  if (shown.length > 0) {
    const byRelation = new Map<string, string[]>()
    for (const link of shown) byRelation.set(link.relation, [...(byRelation.get(link.relation) ?? []), link.hypothesis_id])
    return (
      <>
        {[...byRelation].map(([relation, ids]) => (
          <div key={relation} className="whitespace-nowrap">
            {relation}{' '}
            {ids.map((id) => <Fragment key={id}><Hyp id={id} /> </Fragment>)}
          </div>
        ))}
      </>
    )
  }
  // A record weighed and set aside is not one nobody has ruled on yet.
  if (ruledOut > 0) return <span className="muted" title={`ruled against ${ruledOut} belief(s), bears on none`}>bears on none</span>
  return <span className="muted">nothing yet</span>
}

/** Whether a verdict may rest on this record, and which values an adversary chose.
 *  "attacker-influenceable" was on every record of a real run: in a hunt the adversary's
 *  behaviour is the signal, so attacker-caused is universal and said nothing. What a
 *  reader needs is whether anything here was attested by the telemetry. */
function Attested({ record }: { record: HuntEvidence }) {
  const authored = (record.rests_on ?? []).filter((basis) => basis.authored !== 'sensor')
  const attested = record.sensor_attested ?? !record.attacker_influenceable
  return (
    <>
      {!attested && (
        <span style={{ color: 'var(--high)' }} title="No value this finding rests on was attested by the telemetry, so it cannot carry a verdict on its own.">
          nothing sensor-attested
        </span>
      )}
      {authored.length > 0 && (
        <span className="muted" title={authored.map((basis) => `${basis.field}: ${basis.authored}`).join(', ')}>
          {authored.length} attacker-authored field{authored.length === 1 ? '' : 's'}
        </span>
      )}
    </>
  )
}

function HuntEvidenceTable({ found, total }: { found: HuntEvidence[]; total: number }) {
  const [showRoutine, setShowRoutine] = useState(false)
  if (found.length === 0) {
    return <div className="muted text-[12.5px] py-3">{total} record(s) gathered, none reported by this run yet.</div>
  }
  // A negative result is evidence, so routine records are folded rather than dropped.
  const routine = found.filter((one) => one.salience === 'routine' && !one.is_gap)
  const rows = showRoutine ? found : found.filter((one) => !routine.includes(one))
  return (
    <div style={{ marginTop: 12 }}>
      <div className="muted text-[11.5px] mb-2 flex gap-2 items-center flex-wrap">
        <span>Newest first{found.length < total && `, showing ${found.length} of ${total}`}.</span>
        {routine.length > 0 && (
          <button className="btn ghost text-[11px]" onClick={() => setShowRoutine((v) => !v)}>
            {showRoutine ? `hide ${routine.length} routine` : `${routine.length} routine hidden`}
          </button>
        )}
      </div>
      <div className="table-wrap">
        <table className="tbl">
          <thead><tr><th className="tight">Turn</th><th className="tight">Source</th><th>What it says</th><th className="tight">Bears on</th></tr></thead>
          <tbody>
            {rows.map((one) => (
              <tr key={one.evidence_id}>
                <td className="muted tight">{one.iteration}</td>
                <td className="muted tight">{one.source_system || '—'}</td>
                <td>
                  {one.summary}
                  {one.why_notable && <div className="muted text-[11px]">{one.why_notable}</div>}
                  <div className="text-[11px] mt-0.5 flex gap-2 flex-wrap">
                    {one.is_gap && <span style={{ color: 'var(--high)' }}>could not look — a blind spot, not a finding</span>}
                    {one.is_gap && one.gap_detail && <span className="muted mono break-all">{one.gap_detail}</span>}
                    {one.salience && !one.is_gap && <span className="muted">{one.salience}</span>}
                    {one.attack_technique && <span className="muted mono">{one.attack_technique}</span>}
                    <Attested record={one} />
                    {one.instruction_like && <span style={{ color: 'var(--crit)' }}>reads as instruction</span>}
                  </div>
                </td>
                <td className="muted"><BearsOn links={one.bears_on} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

/** Title, description, and reason live on the approval, not on the phase row. */
interface GateApproval {
  action_id: string
  title?: string
  description?: string
  reason?: string
}

/** Approve / Reject for a phase the playbook marked approval_required. Same calls
 *  as the approvals inbox: they resume the run. Reject needs a reason. */
function PhaseGate({ runId, onAnswered }: { runId: string; onAnswered: () => void }) {
  const [actions, setActions] = useState<GateApproval[] | null>(null)
  const [failed, setFailed] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [answered, setAnswered] = useState(false)
  const [rejectFor, setRejectFor] = useState<GateApproval | null>(null)

  useEffect(() => {
    let cancelled = false
    approvalsApi
      .list({ status: 'pending', workflow_run_id: runId })
      .then((res) => {
        if (cancelled) return
        const body = res.data as { actions?: GateApproval[] }
        setActions(body.actions ?? [])
      })
      .catch((e) => { if (!cancelled) setFailed(errMsg(e)) })
    return () => { cancelled = true }
  }, [runId])

  const settle = (actionId: string, call: Promise<unknown>) => {
    setBusy(true)
    setFailed(null)
    call
      .then(() => {
        setAnswered(true)
        setActions((rows) => (rows ?? []).filter((a) => a.action_id !== actionId))
        setRejectFor(null)
        onAnswered()
      })
      .catch((e) => setFailed(errMsg(e)))
      .finally(() => setBusy(false))
  }

  return (
    <div className="modal-section run-ask">
      <div className="flex items-center gap-2" style={{ color: 'var(--high)' }}>
        <Icon name="alert" size={15} />
        <h4 style={{ color: 'var(--tx)', margin: 0 }}>Waiting on approval</h4>
      </div>
      {actions === null && failed === null && <div className="muted text-[12.5px] mt-2">Loading the approval…</div>}
      {actions !== null && actions.length === 0 && (
        <div className="muted text-[12.5px] mt-2">
          {answered
            ? 'Answer sent. The run picks it up from here.'
            : 'This phase is waiting, but no pending approval is on file for this run.'}
        </div>
      )}
      {actions?.map((action) => (
        <div key={action.action_id} className="mt-2">
          <div className="text-[12.5px] leading-[1.55]">{action.title || action.action_id}</div>
          {action.description && action.description !== action.title && (
            <div className="muted text-[12px] mt-1">{action.description}</div>
          )}
          {action.reason && <div className="text-[12.5px] mt-1">{action.reason}</div>}
          <div className="flex gap-2 items-center flex-wrap mt-2">
            <button className="btn primary" disabled={busy} onClick={() => settle(action.action_id, approvalsApi.approve(action.action_id))}>
              <Icon name="check2" /> Approve
            </button>
            <button className="btn danger" disabled={busy} onClick={() => setRejectFor(action)}>
              <Icon name="x2" /> Reject
            </button>
          </div>
        </div>
      ))}
      {failed && <div className="text-[11.5px] mt-2" style={{ color: 'var(--crit)' }}>{failed}</div>}
      <RejectPhaseGate
        open={rejectFor !== null}
        title={rejectFor?.title || rejectFor?.action_id || ''}
        busy={busy}
        onClose={() => { if (!busy) setRejectFor(null) }}
        onConfirm={(reason) => {
          if (!rejectFor) return
          settle(rejectFor.action_id, approvalsApi.reject(rejectFor.action_id, reason))
        }}
      />
    </div>
  )
}

function RejectPhaseGate({
  open, title, busy, onClose, onConfirm,
}: {
  open: boolean
  title: string
  busy: boolean
  onClose: () => void
  onConfirm: (reason: string) => void
}) {
  const [reason, setReason] = useState('')
  useEffect(() => { if (open) setReason('') }, [open])

  const submit = () => {
    const text = reason.trim()
    if (!text || busy) return
    onConfirm(text)
  }

  return (
    <Popup open={open} onClose={onClose} title="Reject action" width={520}>
      <div className="flex flex-col gap-3.5">
        <p className="text-[13px] text-tx-2 m-0">{title}</p>
        <Field
          label="Rejection reason"
          hint="Required. Recorded on the workflow run’s audit trail."
          textarea
          value={reason}
          onChange={setReason}
          placeholder="Why is this action being rejected?"
        />
        <div className="flex justify-end gap-2.5">
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn danger" disabled={!reason.trim() || busy} onClick={submit}>
            {busy ? 'Rejecting…' : 'Reject'}
          </button>
        </div>
      </div>
    </Popup>
  )
}

function RunWithoutHunt({ d, inFlight }: { d: WfRunDetail; inFlight: boolean }) {
  const replay = useInvestigateReplay(d.run_id, inFlight)
  return (
    <>
      {replay.kind === 'investigate' && <InvestigateDecisions decisions={replay.decisions} />}
      {replay.kind === 'failed' && (
        <div className="text-[12.5px] py-2" style={{ color: 'var(--crit)' }}>
          Couldn’t read decisions — {replay.message}
        </div>
      )}
      <ComposeDetail d={d} suppressEmpty={replay.kind === 'pending' || replay.kind === 'investigate'} />
    </>
  )
}

function InvestigateDecisions({ decisions }: { decisions: InvestigateDecisionView[] }) {
  return (
    <div className="modal-section">
      <h4>Decisions</h4>
      {decisions.length === 0 ? (
        <div className="muted text-[12.5px]">No decisions recorded.</div>
      ) : (
        decisions.map((decision) => (
          <div key={decision.iteration} className="mb-3">
            <div className="flex gap-2 items-baseline flex-wrap">
              <span className="muted text-[11.5px]">Iteration {decision.iteration}</span>
              <span className="font-mono text-[12px]">{decision.action}</span>
              <span className="muted text-[11.5px]"><Cost usd={decision.cost_usd} digits={3} /></span>
            </div>
            <div className="text-[12.5px] text-tx-2 leading-[1.5] mt-1">
              <span className="muted text-[11px] uppercase tracking-[0.06em] mr-1.5">model text</span>
              {decision.rationale || '—'}
            </div>
            <DecisionCalls calls={decision.calls} />
          </div>
        ))
      )}
    </div>
  )
}

function DecisionCalls({ calls }: { calls: unknown[] }) {
  if (calls.length === 0) return <div className="muted text-[11.5px] mt-1">No calls followed.</div>
  return (
    <ul className="text-[12px] mt-1 mb-0" style={{ paddingLeft: 18 }}>
      {calls.map((call, at) => {
        const line = callLine(call)
        return (
          <li key={at}>
            <span className="font-mono">{line.tool}</span>
            {line.rest !== '' && <span className="muted"> {line.rest}</span>}
          </li>
        )
      })}
    </ul>
  )
}

/** A run that walks phases has steps and a summary; there is nothing to tab between.
 *  `suppressEmpty` holds the placeholder while a replay read is still out, and while
 *  an investigate body is already showing its decisions. */
function ComposeDetail({ d, suppressEmpty = false }: { d: WfRunDetail; suppressEmpty?: boolean }) {
  const agentMeta = useAgentMeta()
  if (!d.result_summary && !d.phases?.length) {
    if (d.error || suppressEmpty) return null
    return <div className="muted" style={{ padding: '10px 4px' }}>No additional detail recorded for this run.</div>
  }
  return (
    <>
      {d.result_summary && (
        <div className="modal-section">
          <h4>Result summary</h4>
          <ReportBody md={d.result_summary} />
        </div>
      )}
      {!!d.phases?.length && (
        <div className="modal-section">
          <h4>Phases</h4>
          <div className="table-wrap">
            <table className="tbl">
              <thead><tr><th>#</th><th>Agent</th><th>Status</th><th>Duration</th><th>Cost</th></tr></thead>
              <tbody>
                {d.phases.map((p) => (
                  <tr key={p.phase_id}>
                    <td className="muted tight">{p.phase_order}</td>
                    <td>{agentMeta(p.agent_id).label}{p.error && <span className="ml-2" style={{ color: 'var(--crit)' }} title={p.error}>⚠</span>}</td>
                    <td className="tight"><span style={{ color: runStatusColor(p.status) }}>{p.status}</span></td>
                    <td className="muted tight">{fmtDuration(p.duration_ms)}</td>
                    <td className="muted tight"><Cost usd={p.cost_usd} digits={3} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </>
  )
}

/** Markdown at the panel's type scale rather than the document scale it is written at. */
function ReportBody({ md }: { md: string }) {
  return (
    <div className="text-[12.5px] text-tx-2 leading-[1.55] [&_h1]:text-[15px] [&_h1]:font-semibold [&_h2]:text-[13.5px] [&_h2]:font-semibold [&_h3]:text-[12.5px] [&_h3]:font-semibold [&_h1]:mt-1 [&_h2]:mt-2.5 [&_h3]:mt-2">
      <Markdown>{md}</Markdown>
    </div>
  )
}

/** What one press of extend buys. Sent as a typed grant rather than as prose the run
 *  has to parse: `extend` with an empty note parsed to nothing, journaled a note saying
 *  so, and left the hunt parked at the ceiling it was asking to be let past. */
const GRANTS: [string, { iterations: number; cost_usd: number; wall_ms: number }][] = [
  ['+3 turns', { iterations: 3, cost_usd: 0, wall_ms: 0 }],
  ['+$5', { iterations: 0, cost_usd: 5, wall_ms: 0 }],
  ['+30 min', { iterations: 0, cost_usd: 0, wall_ms: 30 * 60_000 }],
]

/** queued for the worker holding the ledger, so nothing here is instant — the
 *  panel re-reads rather than claiming the run obeyed */
function Steer({ runId, hunt, onSteered }: { runId: string; hunt: boolean; onSteered: () => void }) {
  const [note, setNote] = useState('')
  const [entity, setEntity] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [said, setSaid] = useState<string | null>(null)

  const send = (kind: string, text: string, fields?: Record<string, unknown>) => {
    setBusy(kind)
    setSaid(null)
    workflowApi
      .steer(runId, kind, text, fields)
      .then(() => { setSaid(`${kind} queued`); setNote(''); setEntity(''); onSteered() })
      .catch((e) => setSaid(errMsg(e)))
      .finally(() => setBusy(null))
  }

  return (
    <div className="modal-section">
      <h4>Steer</h4>
      {hunt && <SteerExtend busy={busy !== null} note={note} send={send} />}
      <div className="flex gap-2 items-center flex-wrap">
        {hunt && (
          <button className="btn ghost" disabled={busy !== null} onClick={() => send('conclude', note.trim())}>
            conclude
          </button>
        )}
        <TextInput
          className="grow"
          placeholder="A note for the run — sent with the button you press, or on its own."
          value={note}
          onChange={(e) => setNote(e.target.value)}
        />
        <button className="btn ghost" disabled={busy !== null || !note.trim()} onClick={() => send('note', note.trim())}>
          note
        </button>
      </div>
      {hunt && <SteerEntity busy={busy !== null} entity={entity} setEntity={setEntity} note={note} send={send} />}
      {said && <div className="muted text-[11.5px] mt-2">{said}</div>}
    </div>
  )
}

/** Keep going, by a stated amount. The amount is the whole of the directive — an extend
 *  that grants nothing is refused now, so there is no press that quietly does nothing. */
function SteerExtend({
  busy, note, send,
}: {
  busy: boolean
  note: string
  send: (kind: string, text: string, fields?: Record<string, unknown>) => void
}) {
  return (
    <div className="flex gap-2 items-center flex-wrap mb-2">
      <span className="text-[11.5px] text-tx-3">Keep going:</span>
      {GRANTS.map(([label, grant]) => (
        <button
          key={label}
          className="btn ghost"
          disabled={busy}
          title={`extend this run by ${label.replace('+', '')} and let it carry on from where it parked`}
          onClick={() => send('extend', note.trim(), { grant })}
        >
          {label}
        </button>
      ))}
    </div>
  )
}

/** The directives that name something rather than just say something, and so need a
 *  typed field the note cannot carry. */
function SteerEntity({
  busy, entity, setEntity, note, send,
}: {
  busy: boolean
  entity: string
  setEntity: (value: string) => void
  note: string
  send: (kind: string, text: string, fields?: Record<string, string>) => void
}) {
  return (
    <div className="flex gap-2 items-center flex-wrap mt-2">
      <TextInput
        className="grow"
        placeholder="type:value — e.g. ip:45.77.53.176"
        value={entity}
        onChange={(e) => setEntity(e.target.value)}
      />
      <button
        className="btn ghost"
        disabled={busy || !entity.trim()}
        title="Mark known-benign: its evidence stands, the hunt opens no new work on it."
        onClick={() => send('benign', note.trim(), { entity_key: entity.trim() })}
      >
        known-benign
      </button>
      <button
        className="btn ghost"
        disabled={busy || !note.trim()}
        title="Put a lead on the frontier — what you want looked at, and the entity it is about."
        onClick={() => send('lead', note.trim(), entity.trim() ? { entity_key: entity.trim() } : undefined)}
      >
        add lead
      </button>
      <button
        className="btn ghost"
        disabled={busy || !note.trim()}
        title="Declare a blind spot no query will report — 'we have no EDR on that subnet'."
        onClick={() => send('gap', note.trim())}
      >
        declare gap
      </button>
    </div>
  )
}

/** What evidence actually cited, falling back to a declared technique so a run
 *  from before the two were separated still reads correctly. */
function techniquesOf(h: HuntStanding): string {
  const cited = h.techniques_cited ?? []
  return cited.length > 0 ? cited.join(', ') : h.attack_technique || ''
}

/** The corroboration a verdict rested on, in the report's own words. */
function strengthLine(s: HuntStrength): string {
  return [
    `${s.corroborating_sources} corroborating source system(s)`,
    `${s.contradicting_records} contradicting record(s)`,
    `${s.open_gaps} open gap(s)`,
    s.attacker_influenceable_only ? 'support is attacker-influenceable only' : 'support is not attacker-authored alone',
    s.survived_disconfirmation ? 'survived disconfirmation' : 'did not survive disconfirmation',
  ].join(', ')
}

/** What a hunt has tested and how each belief stands — its equivalent of phase rows. */
/** How the evidence landed on each belief, counted from the rulings the projection
 *  already carries. Every other field on a standing is written at verdict time, so an
 *  unresolved board reported the coerced status and nothing else — nine rows saying
 *  "inconclusive" over a run that had four records supporting one of them. */
interface Bearing { supports: number; weakens: number; ruledOut: number }

export function bearings(evidence: readonly HuntEvidence[]): Map<string, Bearing> {
  const held = new Map<string, Bearing>()
  for (const record of evidence) {
    for (const link of record.bears_on ?? []) {
      const tally = held.get(link.hypothesis_id) ?? { supports: 0, weakens: 0, ruledOut: 0 }
      if (link.relation === 'supports') tally.supports += 1
      else if (link.relation === 'weakens') tally.weakens += 1
      else tally.ruledOut += 1
      held.set(link.hypothesis_id, tally)
    }
  }
  return held
}

/** A belief nothing has been ruled against yet reads differently from one every record
 *  was weighed against and set aside: the second is a hunt that looked. */
function BearingCell({ tally }: { tally?: Bearing }) {
  if (tally === undefined || tally.supports + tally.weakens + tally.ruledOut === 0) {
    return <span className="muted">nothing ruled yet</span>
  }
  if (tally.supports + tally.weakens === 0) {
    return <span className="muted" title={`weighed against ${tally.ruledOut} record(s), none bore on it`}>not engaged</span>
  }
  return (
    <span className="whitespace-nowrap">
      <b>{tally.supports}</b> for · <b>{tally.weakens}</b> against
    </span>
  )
}

function HuntStandings({ hunt }: { hunt: HuntView }) {
  const strengthOf = (id: string) =>
    hunt.report?.hypotheses.find((h) => h.hypothesis_id === id)?.evidence_strength ?? null
  // Sorted rather than filtered: the benign account is what the others are measured against.
  const ordered = [...hunt.hypotheses].sort(
    (a, b) => Number(a.provenance === 'base_rate') - Number(b.provenance === 'base_rate'),
  )
  const tallies = bearings(hunt.evidence ?? [])
  // One run-level fact, said once. A terminal coerces every unresolved belief with the
  // same sentence, which printed per row is the run bar's news repeated nine times.
  const reasons = new Set(ordered.map((h) => h.resolution_reason).filter((why) => !!why))
  const shared = reasons.size === 1 && ordered.length > 1 ? [...reasons][0] : null
  // The rulings are counted over the records the projection carries, which is capped.
  const partial = (hunt.evidence?.length ?? 0) < hunt.evidence_count
  return (
    <div style={{ marginTop: 12 }}>
      <div className="muted text-[11.5px] mb-2">
        {hunt.evidence_count} piece{hunt.evidence_count === 1 ? '' : 's'} of evidence gathered so far.
        {partial && ` Rulings counted over the ${hunt.evidence?.length} most recent.`}
        {shared !== null && <> All of them: {shared}.</>}
      </div>
      {hunt.hypotheses.length === 0 && <div className="muted" style={{ padding: '4px 0' }}>No hypotheses on the board yet.</div>}
      {hunt.hypotheses.length > 0 && (
        <div className="table-wrap">
          <table className="tbl">
            <thead><tr><th className="tight" /><th>Statement</th><th className="tight">Evidence</th><th>Techniques cited</th><th>Standing</th></tr></thead>
            <tbody>
              {ordered.map((h) => {
                const strength = strengthOf(h.hypothesis_id)
                return (
                  <tr key={h.hypothesis_id}>
                    <td className="tight"><Hyp id={h.hypothesis_id} /></td>
                    <td>
                      {h.statement}
                      {h.provenance === 'operator' && <span className="chip ml-2" style={{ fontSize: 10 }}>yours</span>}
                      {h.provenance === 'base_rate' && (
                        <span className="chip ml-2" style={{ fontSize: 10 }} title="Seeded on every hunt as the claim to beat, not something you asked for.">
                          the claim to beat
                        </span>
                      )}
                      {h.resolution_reason && shared === null && <div className="muted text-[11px]">{h.resolution_reason}</div>}
                      {strength && <div className="muted text-[11px]">{strengthLine(strength)}</div>}
                    </td>
                    <td className="tight"><BearingCell tally={tallies.get(h.hypothesis_id)} /></td>
                    <td className="muted">{techniquesOf(h) || '—'}</td>
                    <td className="tight"><span style={{ color: hypothesisColor(h.status) }}>{h.status}</span></td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

/** The one thing on this panel waiting on a person, and the approve/reject that
 *  answers it. */
function OpenCheckpoint({ hunt }: { hunt: HuntView }) {
  const open = hunt.open_checkpoint
  const [busy, setBusy] = useState<string | null>(null)
  const [failed, setFailed] = useState<string | null>(null)
  // Which question was answered: the projection reports it until the run journals a resolution.
  const [answered, setAnswered] = useState<{ checkpoint_id: string; kind: string } | null>(null)
  const [why, setWhy] = useState('')
  if (!open) return null

  const answer = (kind: 'approve' | 'reject') => {
    setBusy(kind)
    setFailed(null)
    workflowApi
      .steer(hunt.run_id ?? '', kind, why.trim(), { checkpoint_id: open.checkpoint_id })
      .then(() => { setAnswered({ checkpoint_id: open.checkpoint_id, kind }); setWhy('') })
      .catch((e) => setFailed(errMsg(e)))
      .finally(() => setBusy(null))
  }

  // Answered and waiting on the run, not on a person; clears when the ledger catches up.
  if (answered?.checkpoint_id === open.checkpoint_id) {
    return (
      <div className="modal-section run-ask" style={{ borderLeftColor: 'var(--ok)', background: 'var(--ok-dim)' }}>
        <div className="flex items-center gap-2 text-[12.5px]">
          <span style={{ color: 'var(--ok)', display: 'inline-flex' }}><Icon name="check" size={15} /></span>
          <span><b>{answered.kind}</b> sent. The run picks it up at its next turn.</span>
        </div>
      </div>
    )
  }

  const unbound = (open.context?.['unbound_capabilities'] as string[] | undefined) ?? []

  return (
    <div className="modal-section run-ask">
      <div className="flex items-center gap-2" style={{ color: 'var(--high)' }}>
        <Icon name="alert" size={15} />
        <h4 style={{ color: 'var(--tx)', margin: 0 }}>Waiting on you{open.checkpoint_class ? ` · ${open.checkpoint_class}` : ''}</h4>
      </div>
      <div style={{ height: 8 }} />
      <div className="text-[12.5px] leading-[1.55] mb-2" style={{ whiteSpace: 'pre-wrap' }}>{open.question}</div>
      {unbound.length > 0 && (
        <div className="muted text-[11.5px] mb-2">No tool here answers {unbound.join(', ')}.</div>
      )}
      <div className="flex gap-2 items-center flex-wrap">
        <button className="btn primary" disabled={busy !== null} onClick={() => answer('approve')}>approve</button>
        <button className="btn ghost" disabled={busy !== null} onClick={() => answer('reject')} style={{ color: 'var(--crit)' }}>
          reject
        </button>
        <TextInput
          className="grow"
          placeholder="Why — recorded with your answer, and read by the run."
          value={why}
          onChange={(e) => setWhy(e.target.value)}
        />
      </div>
      {failed && <div className="text-[11.5px] mt-2" style={{ color: 'var(--crit)' }}>{failed}</div>}
    </div>
  )
}

/** Questions the hunt could not answer. Its own section because "not there" and
 *  "could not look" read identically otherwise, and only one clears a hypothesis. */
/** Rows across all three kinds. Null when the read could not be served: a zero there
 *  would read as "memory holds nothing about these entities", which is what an
 *  answered read that came back empty says. */
function recalledCount(recall: HuntRecall): number | null {
  if (recall.unavailable !== undefined) return null
  return (recall.verdicts?.length ?? 0) + (recall.gaps?.length ?? 0) + (recall.sightings?.length ?? 0)
}

function fmtWindow(w: RecalledWindow): string {
  return w.first_seen === w.last_seen ? fmtStarted(w.first_seen) : `${fmtStarted(w.first_seen)} → ${fmtStarted(w.last_seen)}`
}

/** Which investigation left the row, not which run read it: one read returns rows
 *  from many, and a Case is not a hunt. */
function RecalledFromCell({ row }: { row: RecalledFrom }) {
  return (
    <>
      <span className="mono text-[11px]">{row.investigation_id}</span>
      <div className="muted text-[11px]">{row.investigation_kind}, concluded {fmtStarted(row.concluded_at)}</div>
    </>
  )
}

/** Per kind and per reason: a per-key cap says one entity had more history than its
 *  share, an overall cap says the read was simply broad, and summing them keeps the
 *  number while discarding the half a reader would act on. */
function droppedNote(recall: HuntRecall): string | null {
  const dropped = recall.dropped
  if (dropped === undefined) return null
  const held: string[] = []
  for (const kind of ['verdicts', 'gaps', 'sightings'] as const) {
    const rows = dropped[kind]
    if (rows === undefined) continue
    if (rows.per_key_cap > 0) held.push(`${rows.per_key_cap} ${kind} past one entity's share`)
    if (rows.overall_cap > 0) held.push(`${rows.overall_cap} ${kind} past the read's own limit`)
  }
  return held.length === 0 ? null : `Some history was not carried: ${held.join(', ')}.`
}

/** What the hunt was shown before its first decision. Rendered from the payload the
 *  run journaled, so this is the record it actually opened on — re-reading memory
 *  here would show a neighbourhood that has moved since. */
function HuntMemory({ recall }: { recall: HuntRecall }) {
  const asked = recall.keys.length === 0 ? 'no entities' : recall.keys.join(', ')

  if (recall.unavailable !== undefined) {
    return (
      <div style={{ marginTop: 12 }}>
        <h4>What this hunt knew going in</h4>
        <div className="text-[12.5px]">Episodic memory could not be read, so the hunt ran without it.</div>
        <div className="muted text-[11.5px] mt-2">{recall.unavailable}</div>
        <div className="muted text-[11.5px] mt-2">
          It would have asked about <span className="mono">{asked}</span>. Nothing this hunt concluded rests on what
          earlier investigations found.
        </div>
      </div>
    )
  }

  const verdicts = recall.verdicts ?? []
  const gaps = recall.gaps ?? []
  const sightings = recall.sightings ?? []
  const dropped = droppedNote(recall)

  return (
    <div style={{ marginTop: 12 }}>
      <h4>What this hunt knew going in</h4>
      <div className="muted text-[11.5px] mb-2">
        Read on <span className="mono">{asked}</span>, as of {fmtStarted(recall.as_of)}. The state of memory read by
        the hunt when opened.
      </div>

      {verdicts.length + gaps.length + sightings.length === 0 && (
        <div className="muted text-[12.5px] py-2">
          Nothing had been concluded about {recall.keys.length === 1 ? 'this entity' : 'these entities'} before this
          hunt. The read ran and came back empty: this is the first investigation on record to look.
        </div>
      )}

      {verdicts.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <h4>Settled by earlier investigations ({verdicts.length})</h4>
          <div className="table-wrap">
            <table className="tbl">
              <thead><tr><th>Outcome</th><th>Claim</th><th>Subjects</th><th>From</th></tr></thead>
              <tbody>
                {verdicts.map((v) => (
                  <tr key={`${v.investigation_id}|${v.hypothesis_id}`}>
                    <td className="tight">{v.outcome}</td>
                    <td>
                      {v.statement}
                      {v.rationale && <div className="muted text-[11px]">{v.rationale}</div>}
                      {v.attacker_influenceable_only && (
                        <div className="muted text-[11px]">Rests only on evidence an adversary could have written.</div>
                      )}
                      <div className="muted text-[11px]">
                        activity {fmtWindow(v.window)}{v.window_source === 'asserted' ? ' (asserted)' : ''} · {v.trust}
                      </div>
                    </td>
                    <td className="mono tight text-[11px]">{v.subject_entities.join(', ')}</td>
                    <td className="tight"><RecalledFromCell row={v} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {gaps.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <h4>Left unanswered by earlier investigations ({gaps.length})</h4>
          <div className="muted text-[11.5px] mb-2">Asked before and never settled — an open question, not a finding.</div>
          <div className="table-wrap">
            <table className="tbl">
              <thead><tr><th>Claim</th><th>Why it stands open</th><th>Subjects</th><th>From</th></tr></thead>
              <tbody>
                {gaps.map((g) => (
                  <tr key={`${g.investigation_id}|${g.hypothesis_id}`}>
                    <td>{g.statement}</td>
                    {/* Not tight: the disposition is one word but the reason under it is a
                        sentence, and nowrap on a sentence takes the whole row -- which leaves
                        the claim beside it a column one character wide. */}
                    <td>
                      {g.disposition}
                      {g.reason && <div className="muted text-[11px]">{g.reason}</div>}
                    </td>
                    <td className="mono tight text-[11px]">{g.subject_entities.join(', ')}</td>
                    <td className="tight"><RecalledFromCell row={g} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {sightings.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <h4>Seen by earlier investigations ({sightings.length})</h4>
          <div className="muted text-[11.5px] mb-2">Where an entity turned up before. A lead, not a conclusion.</div>
          <div className="table-wrap">
            <table className="tbl">
              <thead><tr><th>Entity</th><th>Source</th><th>Hits</th><th>Window</th><th>From</th></tr></thead>
              <tbody>
                {sightings.map((one) => (
                  <tr key={`${one.investigation_id}|${one.entity_key}|${one.source_system}`}>
                    <td className="mono tight text-[11px]">
                      {one.entity_key}
                      {one.attacker_influenceable && <div className="muted text-[11px]">attacker-influenceable</div>}
                    </td>
                    <td className="muted tight">{one.source_system}</td>
                    <td className="muted tight">{one.hit_count}</td>
                    <td className="muted tight text-[11px]">{fmtWindow(one.window)}</td>
                    <td className="tight"><RecalledFromCell row={one} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {dropped !== null && <div className="muted text-[11px] mt-2">{dropped}</div>}
    </div>
  )
}

function HuntGaps({ gaps }: { gaps: HuntGap[] }) {
  if (gaps.length === 0) return null
  const asked = groupedGaps(gaps)
  return (
    <div style={{ marginTop: 12 }}>
      <h4>Visibility gaps ({gaps.length})</h4>
      <div className="muted text-[11.5px] mb-2">Each is a blind spot, not a finding.</div>
      <div className="table-wrap">
        <table className="tbl">
          <thead><tr><th>Iteration</th><th>Bears on</th><th>What went unanswered</th></tr></thead>
          <tbody>
            {asked.map((g) => (
              <tr key={g.key}>
                <td className="muted tight">{g.iteration}</td>
                <td className="muted tight"><Hyp id={g.hypothesis_id} /></td>
                <td>
                  {g.query_intent || g.reasons[0]}
                  {g.query_intent && g.reasons.map((reason) => (
                    <div key={reason} className="muted text-[11px]">{reason}</div>
                  ))}
                  {g.workers > 1 && <div className="muted text-[11px]">{g.workers} workers, same question.</div>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

/** One row per question, not per worker: a fan-out sends the same query_intent to
 *  every worker, so a failure would otherwise repeat it and bury the reasons. */
export function groupedGaps(gaps: HuntGap[]) {
  const byQuestion = new Map<string, { key: string; iteration: number; hypothesis_id: string | null; query_intent: string; reasons: string[]; workers: number }>()
  for (const gap of gaps) {
    const intent = gap.query_intent ?? ''
    const key = `${gap.iteration}|${gap.hypothesis_id ?? ''}|${intent}`
    const held = byQuestion.get(key)
    if (held === undefined) {
      byQuestion.set(key, {
        key,
        iteration: gap.iteration,
        hypothesis_id: gap.hypothesis_id ?? null,
        query_intent: intent,
        reasons: [gap.summary],
        workers: 1,
      })
      continue
    }
    held.workers += 1
    if (!held.reasons.includes(gap.summary)) held.reasons.push(gap.summary)
  }
  return [...byQuestion.values()]
}

function HuntEscalations({ handoffs }: { handoffs: HuntHandoff[] }) {
  if (handoffs.length === 0) return null
  return (
    <div style={{ marginTop: 12 }}>
      <h4>Escalated to incident response ({handoffs.length})</h4>
      <div className="table-wrap">
        <table className="tbl">
          <thead><tr><th>Case</th><th>Hypothesis</th><th>Why</th></tr></thead>
          <tbody>
            {handoffs.map((h) => (
              <tr key={h.case_id}>
                <td className="mono tight" style={{ fontSize: 11 }}>{h.case_id}</td>
                <td className="muted tight"><Hyp id={h.hypothesis_id} /></td>
                <td>{h.rationale}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

/** Where a human was in the loop, and where policy stood in for one. */
function HuntCheckpoints({ checkpoints }: { checkpoints: HuntCheckpoint[] }) {
  if (checkpoints.length === 0) return null
  return (
    <div style={{ marginTop: 16 }}>
      <h4>Checkpoints ({checkpoints.length})</h4>
      <div className="table-wrap">
        <table className="tbl">
          <thead><tr><th>Class</th><th>Question</th><th>Answer</th></tr></thead>
          <tbody>
            {checkpoints.map((c) => (
              <tr key={c.checkpoint_id}>
                <td className="muted tight">{c.class}</td>
                <td>{c.question}</td>
                <td className="muted">
                  {c.resolution ? `${c.resolution.answer} by ${c.resolution.actor}${c.resolution.text ? ` — ${c.resolution.text}` : ''}` : 'still pending'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function hypothesisColor(s: string): string {
  if (s === 'proven' || s === 'handed_off') return 'var(--crit)'
  if (s === 'disproven') return 'var(--ok)'
  if (s === 'parked' || s === 'inconclusive') return 'var(--tx-2)'
  return 'var(--med)' // active
}

function EditModal({ wf, onClose, onSaved }: { wf: Workflow; onClose: () => void; onSaved: () => void }) {
  const [name, setName] = useState(wf.name)
  const [description, setDescription] = useState(wf.desc)
  const [useCase, setUseCase] = useState(wf.useCase)
  const [triggers, setTriggers] = useState(wf.cmds.join('\n'))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const save = () => {
    setBusy(true)
    setError(null)
    workflowApi
      .updateCustom(wf.id, {
        name: name.trim(),
        description: description.trim(),
        use_case: useCase.trim(),
        trigger_examples: triggers.split('\n').map((t) => t.trim()).filter(Boolean),
      })
      .then(onSaved)
      .catch((e) => { setError(errMsg(e)); setBusy(false) })
  }

  return (
    <Popup open onClose={onClose} title={`Edit · ${wf.name}`}>
      <div className="flex flex-col gap-3.5">
        <Field label="Name" value={name} onChange={setName} />
        <Field label="Description" value={description} onChange={setDescription} textarea />
        <Field label="Use case" value={useCase} onChange={setUseCase} textarea />
        <Field label="Trigger examples (one per line)" value={triggers} onChange={setTriggers} textarea mono />
        <p className="text-[11.5px] text-tx-3">Phases and agent sequence are edited in the workflow builder.</p>
        {error && <div className="text-[12.5px]" style={{ color: 'var(--crit)' }}>{error}</div>}
        <div className="flex justify-end gap-2.5 pt-1">
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={busy || !name.trim()} style={{ opacity: busy || !name.trim() ? 0.5 : 1 }} onClick={save}>{busy ? 'Saving…' : 'Save changes'}</button>
        </div>
      </div>
    </Popup>
  )
}

function DeleteModal({ wf, onClose, onDeleted }: { wf: Workflow; onClose: () => void; onDeleted: () => void }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const del = () => {
    setBusy(true)
    setError(null)
    workflowApi
      .deleteCustom(wf.id)
      .then(onDeleted)
      .catch((e) => { setError(errMsg(e)); setBusy(false) })
  }

  return (
    <Popup open onClose={onClose} title="Delete workflow" width={460}>
      <div className="flex flex-col gap-3.5">
        <p className="text-[13px] text-tx-2 leading-[1.5]">Delete <strong>{wf.name}</strong>? This removes the custom workflow definition. Past run history is retained.</p>
        {error && <div className="text-[12.5px]" style={{ color: 'var(--crit)' }}>{error}</div>}
        <div className="flex justify-end gap-2.5 pt-1">
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn danger" disabled={busy} style={{ opacity: busy ? 0.5 : 1 }} onClick={del}><Icon name="trash" /> {busy ? 'Deleting…' : 'Delete'}</button>
        </div>
      </div>
    </Popup>
  )
}

const CHANGES_LABEL = { read_only: 'Read-only', asks_first: 'Asks first', on_its_own: 'On its own' } as const
const CHANGES_TIP = 'Its actions wait for you unless Settings lets a high-confidence reversible one through.'
const SUCCESS_TIP = 'Counts workflow steps and chat turns only. “Success” means it ran to the end, not that the conclusion was right.'
const LEVELS = {
  good: { name: 'Good', glyph: 'M5 12.5l4.2 4.2L19 7.2' },
  fair: { name: 'Fair', glyph: 'M5 12h14' },
  poor: { name: 'Poor', glyph: 'M7 7l10 10M17 7 7 17' },
} as const
const ASSIGNMENT_NOTE = 'Workflow runs use the investigation assignment in Settings › AI models.'

/** The line under the model name: where the model came from. */
function modelSource(a: AgentTemplate): string | null {
  if (!a.model) return null
  if (a.modelSource === 'agent') return 'Set for this agent'
  if (a.modelSource === 'assignment' && a.category) return `${a.category.charAt(0).toUpperCase()}${a.category.slice(1).replace(/_/g, ' ')} default`
  return 'Default'
}

function AgentsTab({ feed }: { feed: Feed<AgentTemplate> }) {
  const { rows, phase, error, reload } = feed
  const [busy, setBusy] = useState<string | null>(null)
  const [editId, setEditId] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [deleteAgent, setDeleteAgent] = useState<AgentTemplate | null>(null)
  // optimistic On switches, dropped when the list reloads
  const [enabledNow, setEnabledNow] = useState<Record<string, boolean>>({})
  const [toggleErr, setToggleErr] = useState<string | null>(null)
  useEffect(() => setEnabledNow({}), [rows])

  const builtins = rows.filter((a) => !a.custom)
  const ordered = [...builtins, ...rows.filter((a) => a.custom)]

  const fork = (handle: string) => {
    setBusy(handle)
    agentsApi
      .forkAgent(handle)
      .then((res) => {
        reload()
        const newId = res.data?.id
        if (newId) setEditId(newId)
      })
      .finally(() => setBusy(null))
  }

  const setEnabled = (a: AgentTemplate, on: boolean) => {
    setToggleErr(null)
    setEnabledNow((m) => ({ ...m, [a.handle]: on }))
    agentsApi.setEnabled(a.handle, on).catch((e) => {
      setEnabledNow((m) => ({ ...m, [a.handle]: !on }))
      setToggleErr(`Couldn’t turn ${a.name} ${on ? 'on' : 'off'}: ${(e as { message?: string })?.message || 'request failed'}`)
    })
  }

  return (
    <div className="ag-page">
      <div className="ag-bar">
        <span className="ag-summary">
          {phase === 'ready' ? `${builtins.length} built-in agent${builtins.length === 1 ? '' : 's'} plus your own. Each can use its own model.` : 'Built-in agents plus your own. Each can use its own model.'}
        </span>
        <button className="ag-btn" title="Refresh" aria-label="Refresh" onClick={reload}><Icon name="refresh" /></button>
        <button className="ag-btn" onClick={() => setCreating(true)}><Icon name="sparkle" /> Describe a new agent</button>
        <button className="ag-btn primary" onClick={() => setCreating(true)}><Icon name="plus" /> New agent</button>
      </div>

      {toggleErr && <div className="ag-err" role="alert">{toggleErr}</div>}
      {phase === 'loading' && <StateMsg><EmptyState loading compact icon="brain" title="Loading agents…" /></StateMsg>}
      {phase === 'error' && <StateMsg><EmptyState error icon="alert" title="Couldn’t load agents" body={error} primary={{ label: 'Retry', onClick: reload, icon: 'refresh' }} /></StateMsg>}
      {phase === 'ready' && rows.length === 0 && <StateMsg><EmptyState icon="brain" title="No agents yet" body="Create a custom SOC agent or refresh to load built-in templates." primary={{ label: 'New agent', onClick: () => setCreating(true), icon: 'plus' }} secondary={{ label: 'Refresh', onClick: reload, icon: 'refresh' }} /></StateMsg>}

      {phase === 'ready' && rows.length > 0 && (
        <>
          <AgentTable
            agents={ordered.map((a) => ({ ...a, enabled: enabledNow[a.handle] ?? a.enabled }))}
            onOpen={(a) => (a.custom ? setEditId(a.handle) : fork(a.handle))}
            onToggle={setEnabled}
            renderActions={(a) => a.custom ? (
              <span className="row-act">
                <button title="Edit" aria-label={`Edit ${a.name}`} onClick={() => setEditId(a.handle)}><Icon name="edit" /></button>
                <button title="Fork into a new copy" aria-label={`Fork ${a.name}`} disabled={busy !== null} onClick={() => fork(a.handle)}><Icon name={busy === a.handle ? 'refresh' : 'copy'} /></button>
                <button title="Delete" aria-label={`Delete ${a.name}`} onClick={() => setDeleteAgent(a)}><Icon name="trash" /></button>
              </span>
            ) : (
              <span className="row-act">
                <button title="Fork to editable copy" aria-label={`Fork ${a.name}`} disabled={busy !== null} onClick={() => fork(a.handle)}><Icon name={busy === a.handle ? 'refresh' : 'fork'} /></button>
              </span>
            )}
          />
          <p className="ag-foot">{ASSIGNMENT_NOTE}</p>
        </>
      )}

      {(creating || editId) && (
        <AgentEditModal
          agentId={editId}
          onClose={() => { setEditId(null); setCreating(false) }}
          onSaved={() => { setEditId(null); setCreating(false); reload() }}
        />
      )}
      {deleteAgent && <AgentDeleteModal agent={deleteAgent} onClose={() => setDeleteAgent(null)} onDeleted={() => { setDeleteAgent(null); reload() }} />}
    </div>
  )
}

function SuccessInfo() {
  const [open, setOpen] = useState(false)
  return (
    <span className="ag-info" onBlur={() => setOpen(false)} onKeyDown={(e) => e.key === 'Escape' && setOpen(false)}>
      <button type="button" aria-label="How success is calculated" aria-expanded={open} onClick={() => setOpen((o) => !o)}><Icon name="info" size={15} /></button>
      {open && <span role="dialog" className="ag-info-pop">{SUCCESS_TIP}</span>}
    </span>
  )
}

function AgentTable({ agents, onOpen, onToggle, renderActions }: {
  agents: AgentTemplate[]
  onOpen: (a: AgentTemplate) => void
  onToggle: (a: AgentTemplate, on: boolean) => void
  renderActions: (a: AgentTemplate) => React.ReactNode
}) {
  // a click on the switch or a row action must not also open the row
  const stop = (e: React.SyntheticEvent) => e.stopPropagation()
  return (
    <div className="ag-card">
      <table className="tbl agents-tbl">
        <colgroup>
          <col className="c-agent" /><col /><col className="c-model" /><col className="c-skills" /><col className="c-changes" />
          <col className="c-runs" /><col className="c-success" /><col className="c-on" /><col className="c-act" />
        </colgroup>
        <thead><tr>
          <th>Agent</th><th>What it does</th><th>Model</th><th>Skills</th><th>Changes things?</th><th>Runs, 7 days</th>
          <th><span className="ag-th-info">Success<SuccessInfo /></span></th><th>On</th><th><span className="sr-only">Actions</span></th>
        </tr></thead>
        <tbody>
          {agents.map((a) => {
            const source = modelSource(a)
            const level = a.successPct !== null && a.successLevel ? LEVELS[a.successLevel] : null
            return (
              <tr key={a.handle} className={`clickable${a.enabled ? '' : ' ag-off'}`} onClick={() => onOpen(a)}>
                <td>
                  <button type="button" className="ag-who" title={a.custom ? `Edit ${a.name}` : `Fork ${a.name} to an editable copy`}>
                    <span className="ag-ini">{a.ini.charAt(0)}</span>
                    <span className="ag-who-txt"><span className="ag-name">{a.name}</span><span className="ag-sub">{a.custom ? 'Yours' : 'Built in'}</span></span>
                  </button>
                </td>
                <td><span className="ag-does" title={a.does}>{a.does}</span></td>
                <td>
                  {a.model
                    ? <span className="ag-model"><span className="ag-model-name">{a.model}</span>{source && <span className="ag-sub">{source}</span>}</span>
                    : <span className="ag-dash">—</span>}
                </td>
                <td className="ag-num">{a.skills}</td>
                <td>
                  {a.changes
                    ? <span className={`ag-chg ${a.changes}`} title={a.changes === 'asks_first' ? CHANGES_TIP : undefined}>{CHANGES_LABEL[a.changes]}</span>
                    : <span className="ag-dash">—</span>}
                </td>
                <td className="ag-num">{a.runs7d === null ? '—' : a.runs7d.toLocaleString('en-US')}</td>
                <td>
                  <span className="ag-rate">
                    {a.successPct === null ? '—' : `${a.successPct.toFixed(1)}%`}
                    {level && a.successLevel && (
                      <span className={`ag-level ${a.successLevel}`}>
                        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" aria-hidden="true"><path d={level.glyph} /></svg>
                        {level.name}
                      </span>
                    )}
                  </span>
                </td>
                <td className="ag-on" onClick={stop}>
                  <button type="button" role="switch" aria-checked={a.enabled} aria-label={`${a.name} on`} className="ag-switch" onClick={() => onToggle(a, !a.enabled)}><span /></button>
                </td>
                <td className="ag-acts" onClick={stop}>{renderActions(a)}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

interface CustomAgentDetail {
  id: string
  name?: string
  description?: string | null
  specialization?: string | null
  icon?: string | null
  color?: string | null
  role?: string | null
  methodology?: string | null
  extra_principles?: string | null
  system_prompt_override?: string | null
  recommended_tools?: string[]
  max_tokens?: number
  enable_thinking?: boolean
  model?: string | null
  fallback_model?: string | null
  effective_prompt?: string
  forked_from?: string | null
}

interface AgentForm {
  name: string
  specialization: string
  description: string
  icon: string
  color: string
  role: string
  extra_principles: string
  methodology: string
  system_prompt_override: string
  recommended_tools: string
  max_tokens: string
  enable_thinking: boolean
  model: string
  fallback_model: string
}

const BLANK_AGENT_FORM: AgentForm = {
  name: '', specialization: '', description: '', icon: '', color: '#7d74f3', role: '',
  extra_principles: '', methodology: '', system_prompt_override: '', recommended_tools: '',
  max_tokens: '', enable_thinking: false, model: '', fallback_model: '',
}

/** AI-assisted drafting;
    mirroring the old Agent Builder. `agentId === null` ⇒ create mode. */
function AgentEditModal({ agentId, onClose, onSaved }: { agentId: string | null; onClose: () => void; onSaved: () => void }) {
  const isCreate = agentId === null
  const [agent, setAgent] = useState<CustomAgentDetail | null>(null)
  const [phase, setPhase] = useState<'loading' | 'ready' | 'error'>(isCreate ? 'ready' : 'loading')
  const [loadErr, setLoadErr] = useState<string | null>(null)
  const [form, setForm] = useState<AgentForm | null>(isCreate ? { ...BLANK_AGENT_FORM } : null)
  const [advanced, setAdvanced] = useState(false)
  const [showPreview, setShowPreview] = useState(false)
  const [toolNames, setToolNames] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [aiOpen, setAiOpen] = useState(isCreate)
  const [aiDesc, setAiDesc] = useState('')
  const [aiFeedback, setAiFeedback] = useState('')
  const [aiDraft, setAiDraft] = useState<GeneratedAgentDraft | null>(null)
  const [aiBusy, setAiBusy] = useState(false)
  const [aiErr, setAiErr] = useState<string | null>(null)

  const set = <K extends keyof AgentForm>(k: K, v: AgentForm[K]) =>
    setForm((f) => (f ? { ...f, [k]: v } : f))

  useEffect(() => {
    let cancelled = false
    agentsApi.getAvailableTools().then((r) => !cancelled && setToolNames((r.data?.tools || []) as string[])).catch(() => {})
    if (agentId === null) return () => { cancelled = true }
    agentsApi
      .getCustom(agentId)
      .then((res) => {
        if (cancelled) return
        const a = res.data as CustomAgentDetail
        setAgent(a)
        setForm({
          name: a.name || '',
          specialization: a.specialization || '',
          description: a.description || '',
          icon: a.icon || '',
          color: a.color || '#7d74f3',
          role: a.role || '',
          extra_principles: a.extra_principles || '',
          methodology: a.methodology || '',
          system_prompt_override: a.system_prompt_override || '',
          recommended_tools: (a.recommended_tools || []).join(', '),
          max_tokens: a.max_tokens ? String(a.max_tokens) : '',
          enable_thinking: !!a.enable_thinking,
          model: a.model || '',
          fallback_model: a.fallback_model || '',
        })
        setAdvanced(!!a.system_prompt_override)
        setPhase('ready')
      })
      .catch((e) => { if (!cancelled) { setLoadErr(errMsg(e)); setPhase('error') } })
    return () => { cancelled = true }
  }, [agentId])

  // merge an AI draft into the form, preserving a name the user already typed
  const mergeDraft = (d: GeneratedAgentDraft) =>
    setForm((f) => f ? {
      ...f,
      name: f.name.trim() ? f.name : d.name,
      specialization: d.specialization || f.specialization,
      description: d.description || f.description,
      icon: d.icon || f.icon,
      color: d.color || f.color,
      role: d.role || f.role,
      extra_principles: d.extra_principles || f.extra_principles,
      methodology: d.methodology || f.methodology,
      recommended_tools: (d.recommended_tools || []).join(', ') || f.recommended_tools,
      max_tokens: d.max_tokens ? String(d.max_tokens) : f.max_tokens,
      enable_thinking: typeof d.enable_thinking === 'boolean' ? d.enable_thinking : f.enable_thinking,
    } : f)

  const generate = (feedback?: string) => {
    if (!aiDesc.trim()) return
    setAiBusy(true)
    setAiErr(null)
    agentsApi
      .generateCustom({ description: aiDesc.trim(), current_draft: aiDraft, feedback: feedback?.trim() || undefined })
      .then((res) => {
        const d = res.data?.draft
        if (d) { setAiDraft(d); mergeDraft(d); setAiFeedback('') }
      })
      .catch((e) => setAiErr(errMsg(e)))
      .finally(() => setAiBusy(false))
  }

  const save = () => {
    if (!form) return
    setBusy(true)
    setError(null)
    const tokens = parseInt(form.max_tokens, 10)
    const payload = {
      name: form.name.trim(),
      specialization: form.specialization.trim(),
      description: form.description.trim(),
      icon: form.icon.trim() || null,
      color: form.color || null,
      role: form.role.trim(),
      extra_principles: form.extra_principles.trim(),
      methodology: form.methodology.trim(),
      // Advanced override replaces the base template; clear it when toggled off.
      system_prompt_override: advanced ? form.system_prompt_override.trim() || null : null,
      recommended_tools: form.recommended_tools.split(',').map((t) => t.trim()).filter(Boolean),
      ...(Number.isFinite(tokens) && tokens > 0 ? { max_tokens: tokens } : {}),
      enable_thinking: form.enable_thinking,
      model: form.model.trim() || null,
      fallback_model: form.fallback_model.trim() || null,
    }
    const req = isCreate ? agentsApi.createCustom(payload) : agentsApi.updateCustom(agentId, payload)
    req.then(onSaved).catch((e) => { setError(errMsg(e)); setBusy(false) })
  }

  const title = isCreate ? 'New agent' : (phase === 'ready' ? `Edit agent · ${agent?.name || agentId}` : 'Edit agent')

  return (
    <Popup open onClose={onClose} title={title} width={760}>
      {phase === 'loading' && <div className="muted" style={{ padding: '24px 0', textAlign: 'center' }}>Loading agent…</div>}
      {phase === 'error' && <div className="muted" style={{ padding: '24px 0', textAlign: 'center' }}>Couldn’t load agent: {loadErr}</div>}
      {phase === 'ready' && form && (
        <div className="flex flex-col gap-3.5">
          {agent?.forked_from && <p className="text-[11.5px] text-tx-3">Forked from <span className="mono">{agent.forked_from}</span></p>}

          {/* AI assist — describe the agent and let Vigil draft the fields */}
          <div className="border border-line rounded-[8px] overflow-hidden">
            <button className="w-full flex items-center gap-2 px-3 py-2.5 text-[12.5px] text-tx-2 bg-bg hover:bg-panel" onClick={() => setAiOpen((v) => !v)}>
              <Icon name="sparkle" size={14} /> AI assist — describe the agent, Vigil drafts the fields
              <span className="ml-auto" style={{ transform: aiOpen ? 'rotate(90deg)' : 'none', transition: 'transform .12s', display: 'inline-flex' }}><Icon name="chevR" size={13} /></span>
            </button>
            {aiOpen && (
              <div className="border-t border-line p-3 flex flex-col gap-2.5">
                <Field label="Describe the agent" value={aiDesc} onChange={setAiDesc} textarea rows={2} placeholder="e.g. Triages cloud IAM misconfigurations and privilege-escalation paths in AWS/GCP." />
                <div className="flex justify-end">
                  <button className="btn primary" disabled={aiBusy || !aiDesc.trim()} style={{ opacity: aiBusy || !aiDesc.trim() ? 0.5 : 1 }} onClick={() => generate()}>
                    <Icon name="sparkle" /> {aiBusy ? 'Generating…' : aiDraft ? 'Regenerate draft' : 'Generate draft'}
                  </button>
                </div>
                {aiDraft && (
                  <>
                    <p className="text-[11.5px] text-tx-3">Draft applied to the form below — tweak any field directly, or refine with a follow-up:</p>
                    <div className="flex gap-2.5 items-end">
                      <div className="flex-1"><Field label="Refine" value={aiFeedback} onChange={setAiFeedback} placeholder="Add memory-forensics tools; be more conservative on containment." /></div>
                      <button className="btn ghost" disabled={aiBusy || !aiFeedback.trim()} style={{ opacity: aiBusy || !aiFeedback.trim() ? 0.5 : 1 }} onClick={() => generate(aiFeedback)}>Refine</button>
                    </div>
                  </>
                )}
                {aiErr && <div className="text-[12.5px]" style={{ color: 'var(--crit)' }}>{aiErr}</div>}
              </div>
            )}
          </div>

          {/* Identity */}
          <div className="text-[11px] font-semibold tracking-[0.06em] uppercase text-tx-3">Identity</div>
          <Field label="Name *" value={form.name} onChange={(v) => set('name', v)} hint={isCreate ? 'Agent ID is derived from the name.' : 'Agent ID is derived from the name and cannot be changed.'} />
          <Field label="Specialization" value={form.specialization} onChange={(v) => set('specialization', v)} />
          <Field label="Description" value={form.description} onChange={(v) => set('description', v)} textarea />
          <div className="grid grid-cols-2 gap-3.5">
            <Field label="Icon (1 char)" value={form.icon} onChange={(v) => set('icon', v.slice(0, 1))} maxLength={1} />
            <label className="flex flex-col gap-1.5">
              <span className="text-[11px] uppercase tracking-[0.06em] text-tx-3">Color</span>
              <input type="color" className="w-full h-[38px] bg-bg border border-line rounded-[7px] p-1 cursor-pointer" value={form.color} onChange={(e) => set('color', e.target.value)} />
            </label>
          </div>

          {/* Prompt fragments */}
          <div className="pt-1.5 text-[11px] font-semibold tracking-[0.06em] uppercase text-tx-3">Prompt fragments</div>
          <p className="text-[11.5px] text-tx-3 -mt-2">Rendered into the Vigil base prompt (preserves entity-recognition directives; adds read-only memory when recall_entity is granted).</p>
          <Field label="Role *" value={form.role} onChange={(v) => set('role', v)} hint={'Renders as: "You are a SOC {role} in the Vigil SOC platform."'} />
          <Field label="Extra principles" value={form.extra_principles} onChange={(v) => set('extra_principles', v)} textarea />
          <Field label="Methodology" value={form.methodology} onChange={(v) => set('methodology', v)} textarea />
          <label className="flex items-center gap-2.5 text-[12.5px] text-tx-2 cursor-pointer">
            <span
              className={`sk-toggle${advanced ? ' on' : ''}`}
              role="switch"
              aria-checked={advanced}
              aria-label="Advanced: write the full system prompt yourself"
              tabIndex={0}
              onClick={() => setAdvanced((v) => !v)}
              onKeyDown={activateOnKey(() => setAdvanced((v) => !v))}
            ><span className="kn" /></span>
            Advanced: bypass base template (write the full system prompt yourself)
          </label>
          {advanced && (
            <Field label="System prompt (verbatim — replaces the base template)" value={form.system_prompt_override} onChange={(v) => set('system_prompt_override', v)} textarea mono rows={12} />
          )}

          <div className="pt-1.5 text-[11px] font-semibold tracking-[0.06em] uppercase text-tx-3">Model</div>
          <p className="text-[11.5px] text-tx-3 -mt-2">Workflow runs use the investigation assignment in Settings › AI models.</p>
          <div className="grid grid-cols-2 gap-3.5">
            <Field label="Model" value={form.model} onChange={(v) => set('model', v)} mono placeholder="Assignment model" />
            <Field label="Fallback model" value={form.fallback_model} onChange={(v) => set('fallback_model', v)} mono placeholder="Optional" />
          </div>

          {/* Tools & behavior */}
          <div className="pt-1.5 text-[11px] font-semibold tracking-[0.06em] uppercase text-tx-3">Tools &amp; behavior</div>
          <Field
            label="Recommended MCP tools (comma-separated)"
            value={form.recommended_tools}
            onChange={(v) => set('recommended_tools', v)}
            mono
            list="agent-tool-names"
            hint={toolNames.length ? `${toolNames.length} tools available — free text accepted if a tool isn't in the registry yet.` : undefined}
          />
          <datalist id="agent-tool-names">{toolNames.map((t) => <option key={t} value={t} />)}</datalist>
          <div className="grid grid-cols-2 gap-3.5 items-end">
            <Field label="Max tokens" value={form.max_tokens} onChange={(v) => set('max_tokens', v.replace(/[^0-9]/g, ''))} placeholder="2048" />
            <label className="flex items-center gap-2.5 text-[12.5px] text-tx-2 cursor-pointer h-[38px]">
              <span
                className={`sk-toggle${form.enable_thinking ? ' on' : ''}`}
                role="switch"
                aria-checked={form.enable_thinking}
                aria-label="Enable thinking"
                tabIndex={0}
                onClick={() => set('enable_thinking', !form.enable_thinking)}
                onKeyDown={activateOnKey(() => set('enable_thinking', !form.enable_thinking))}
              ><span className="kn" /></span>
              Enable thinking
            </label>
          </div>

          {/* Preview of the saved effective prompt (the exact text Claude receives) */}
          {agent?.effective_prompt && (
            <div className="border border-line rounded-[8px] overflow-hidden">
              <button className="w-full flex items-center gap-2 px-3 py-2.5 text-[12.5px] text-tx-2 bg-bg hover:bg-panel" onClick={() => setShowPreview((v) => !v)}>
                <span style={{ transform: showPreview ? 'rotate(90deg)' : 'none', transition: 'transform .12s', display: 'inline-flex' }}><Icon name="chevR" size={13} /></span>
                Preview effective prompt
              </button>
              {showPreview && (
                <div className="border-t border-line">
                  <pre className="font-mono text-[11px] leading-[1.5] text-tx-2 whitespace-pre-wrap p-3 m-0 overflow-auto" style={{ maxHeight: '40vh' }}>{agent.effective_prompt}</pre>
                  <p className="text-[11px] text-tx-3 px-3 pb-2.5">This is the exact system prompt Claude receives. Re-save to refresh.</p>
                </div>
              )}
            </div>
          )}

          {error && <div className="text-[12.5px]" style={{ color: 'var(--crit)' }}>{error}</div>}
          <div className="flex justify-end gap-2.5 pt-1">
            <button className="btn ghost" onClick={onClose}>Cancel</button>
            <button className="btn primary" disabled={busy || !form.name.trim() || !form.role.trim()} style={{ opacity: busy || !form.name.trim() || !form.role.trim() ? 0.5 : 1 }} onClick={save}>{busy ? (isCreate ? 'Creating…' : 'Saving…') : (isCreate ? 'Create agent' : 'Save changes')}</button>
          </div>
        </div>
      )}
    </Popup>
  )
}

function AgentDeleteModal({ agent, onClose, onDeleted }: { agent: AgentTemplate; onClose: () => void; onDeleted: () => void }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const del = () => {
    setBusy(true)
    setError(null)
    agentsApi
      .deleteCustom(agent.handle)
      .then(onDeleted)
      .catch((e) => { setError(errMsg(e)); setBusy(false) })
  }

  return (
    <Popup open onClose={onClose} title="Delete agent" width={460}>
      <div className="flex flex-col gap-3.5">
        <p className="text-[13px] text-tx-2 leading-[1.5]">Delete <strong>{agent.name}</strong>? This cannot be undone. The built-in template it was forked from (if any) is unaffected.</p>
        {error && <div className="text-[12.5px]" style={{ color: 'var(--crit)' }}>{error}</div>}
        <div className="flex justify-end gap-2.5 pt-1">
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn danger" disabled={busy} style={{ opacity: busy ? 0.5 : 1 }} onClick={del}><Icon name="trash" /> {busy ? 'Deleting…' : 'Delete'}</button>
        </div>
      </div>
    </Popup>
  )
}

const SKILL_GRANT_INFO = 'The grant offers the whole library.'

function SkillsTab({ feed, workflows, agents }: { feed: Feed<Skill>; workflows: Feed<Workflow>; agents: ReturnType<typeof useAgents> }) {
  const { rows, phase, error, reload } = feed
  const [editName, setEditName] = useState<string | null>(null)
  const [building, setBuilding] = useState(false)
  const [deleteSkill, setDeleteSkill] = useState<Skill | null>(null)
  const offered = workflows.phase === 'ready' && agents.phase === 'ready'
    ? workflowsOffered(workflows.rows, agents.grants)
    : null
  const offeredText = offered === null ? '…' : (offered.length > 0 ? offered.join(', ') : '—')

  return (
    <>
      <div className="flex items-center gap-3 px-[22px] pt-[14px]">
        <div className="flex-1 min-w-0">
          <span className="block text-[12px] leading-[1.45] text-tx-3">A skill is a folder with a SKILL.md file: when to use it, the steps, and any scripts. Agents read the skills they are given. Editing one saves a new version.</span>
          <span className="sk-offered" title={`Offered to ${offeredText}`}>
            Offered to
            <button type="button" className="btn ghost icon" aria-label={SKILL_GRANT_INFO} title={SKILL_GRANT_INFO}>
              <Icon name="info" size={14} />
            </button>
            <span className="sk-offered-list">{offeredText}</span>
          </span>
        </div>
        <button className="btn primary h-[34px] rounded-[10px] font-semibold" disabled={phase !== 'ready'} style={{ opacity: phase === 'ready' ? 1 : 0.5 }} onClick={() => setBuilding(true)}><Icon name="sparkle" /> Build a skill</button>
      </div>
      {phase === 'loading' && <StateMsg><EmptyState loading compact icon="sparkle" title="Loading skills…" /></StateMsg>}
      {phase === 'error' && <StateMsg><EmptyState error icon="alert" title="Couldn’t load skills" body={error} primary={{ label: 'Retry', onClick: reload, icon: 'refresh' }} /></StateMsg>}
      {phase === 'ready' && rows.length === 0 && <StateMsg><EmptyState icon="sparkle" title="No skills found" body="Add skill files to the repository or the mounted skills directory and refresh." primary={{ label: 'Refresh', onClick: reload, icon: 'refresh' }} /></StateMsg>}
      {phase === 'ready' && rows.length > 0 && (
        <div className="grid gap-x-5 gap-y-[26px] px-[22px] pt-4 pb-24 [grid-template-columns:repeat(4,minmax(0,1fr))]">
          {rows.map((s) => (
            <div className={`sk-card${s.bundled ? '' : ' sk-custom'}`} key={s.id}>
              <button type="button" className="sk-open" aria-label={`Edit ${s.name}`} onClick={() => setEditName(s.name)}>
                <span className="sk-folder" aria-hidden="true">
                  <span className="sk-tab" />
                  <span className="sk-back" />
                  <span className="sk-paper"><span className="sk-paper-name">SKILL.md</span><i /><i /><i /></span>
                  <span className="sk-flap"><span className="sk-origin">{s.bundled ? 'Built in' : 'Custom'}</span></span>
                </span>
                <span className="sk-text">
                  <span className="sk-name" title={s.name}>{s.name}</span>
                  <span className="sk-desc" title={s.desc}>{s.desc}</span>
                </span>
              </button>
              <div className="sk-meta">
                {s.bundled
                  ? <span className="sk-ro">Read-only</span>
                  : <button className="btn ghost" onClick={() => setDeleteSkill(s)}>Delete</button>}
              </div>
            </div>
          ))}
        </div>
      )}
      {(editName || building) && (
        <SkillDrawer
          name={editName}
          existingNames={rows.map((r) => r.name)}
          onClose={() => { setEditName(null); setBuilding(false) }}
          onSaved={() => { setEditName(null); setBuilding(false); reload() }}
        />
      )}
      {deleteSkill && (
        <SkillDeleteModal
          skill={deleteSkill}
          onClose={() => setDeleteSkill(null)}
          onDeleted={() => { setDeleteSkill(null); reload() }}
        />
      )}
    </>
  )
}
