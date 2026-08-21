/* ============================================================
   Workflows screen — Workflows · Agents · Skills tabs
   Wired to the real backend (workflowApi / agentsApi / skillsApi)
   via useWorkflows / useAgents / useSkills, with loading / empty /
   error states. See REDESIGN_GAPS.md §9.
   ============================================================ */
import { Fragment, createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { Icon } from '../../shared/icons'
import { EmptyState, Popup, TextInput, activateOnKey } from '../../shared/ui'
import { Markdown } from '../../shared/Markdown'
import { type Workflow, type AgentTemplate } from '../../data/appData'
import { useWorkflows, useAgents, useAgentMeta, useSkills } from './useWorkflowsData'
import { workflowApi, agentsApi, findingsApi, casesApi, type GeneratedAgentDraft } from '../../../services/api'
import { skillsApi, SKILL_CATEGORIES, type SkillCategory, type SkillDraft } from '../../../services/skillsApi'
import WorkflowBuilder from './WorkflowBuilder'
import type { Skill } from '../../data/appData'
import type { ScreenProps } from '../../shared/types'

type WfTab = 'workflows' | 'agents' | 'skills'

export default function WorkflowsScreen({ goSettings }: ScreenProps) {
  const [tab, setTab] = useState<WfTab>('workflows')
  const tabs: [WfTab, string][] = [
    ['workflows', 'Workflows'],
    ['agents', 'Agents'],
    ['skills', 'Skills'],
  ]
  return (
    <>
      <div className="flex items-center gap-3 flex-wrap px-[22px] py-[13px] border-b border-line">
        <div className="tabs" role="tablist" aria-label="Workflow views">
          {tabs.map(([k, label]) => (
            <button
              key={k}
              role="tab"
              aria-selected={tab === k}
              className={`tab${tab === k ? ' active' : ''}`}
              onClick={() => setTab(k)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>
      {tab === 'workflows' && <WorkflowCatalog goSettings={goSettings} />}
      {tab === 'agents' && <AgentsTab />}
      {tab === 'skills' && <SkillsTab />}
    </>
  )
}

/* ---- shared empty/loading/error row ---- */
function StateMsg({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ padding: '10px 22px 22px' }}>
      {children}
    </div>
  )
}

/* ---- agent sequence ---- */
function AgentSequence({ agents }: { agents: string[] }) {
  const agentMeta = useAgentMeta()
  return (
    <div className="agent-seq">
      {agents.map((a, i) => {
        const meta = agentMeta(a)
        return (
          <Fragment key={i}>
            <span className="agent-chip">
              <span className="ad" style={{ background: meta.color }} />
              {meta.label}
            </span>
            {i < agents.length - 1 && (
              <span className="seq-arrow"><Icon name="chevR" /></span>
            )}
          </Fragment>
        )
      })}
    </div>
  )
}

/* ---------------- Workflows catalog ---------------- */
type WfModal = { kind: 'run' | 'history' | 'edit' | 'delete' | 'details'; wf: Workflow }

function WorkflowCatalog({ goSettings }: { goSettings: ScreenProps['goSettings'] }) {
  const { rows, phase, error, reload } = useWorkflows()
  const [q, setQ] = useState('')
  const [modal, setModal] = useState<WfModal | null>(null)
  const [creating, setCreating] = useState<null | 'blank' | 'ai'>(null)
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
        <button className="btn ghost" onClick={() => setCreating('ai')}><Icon name="sparkle" /> Generate with AI</button>
        <button className="btn primary" onClick={() => setCreating('blank')}><Icon name="plus" /> New workflow</button>
      </div>
      {phase === 'loading' && <StateMsg><EmptyState loading compact icon="flow" title="Loading workflows…" /></StateMsg>}
      {phase === 'error' && <StateMsg><EmptyState error icon="alert" title="Couldn’t load workflows" body={error} primary={{ label: 'Retry', onClick: reload, icon: 'refresh' }} /></StateMsg>}
      {phase === 'ready' && list.length === 0 && (
        <StateMsg>
          <EmptyState
            icon={q ? 'filter' : 'flow'}
            title={q ? 'No workflows match this search' : 'No workflows yet'}
            body={q ? 'Clear the search to return to the workflow catalog.' : 'Create a workflow manually or generate one with AI from a plain-language investigation goal.'}
            primary={q ? { label: 'Clear search', onClick: () => setQ(''), icon: 'close' } : { label: 'New workflow', onClick: () => setCreating('blank'), icon: 'plus' }}
            secondary={q ? undefined : { label: 'Generate with AI', onClick: () => setCreating('ai'), icon: 'sparkle' }}
          />
        </StateMsg>
      )}
      {phase === 'ready' && list.length > 0 && (
        <div className="grid gap-4 px-[22px] py-5 [grid-template-columns:repeat(auto-fill,minmax(390px,1fr))]">
          {list.map((w) => (
            <div className="flex flex-col gap-[13px] bg-panel border border-line rounded-lg p-[18px] shadow-panel transition-[border-color,transform] duration-150 hover:border-[#2e3744] hover:-translate-y-0.5" key={w.id}>
              <div className="flex gap-[13px] items-center">
                <div className="w-11 h-11 rounded-[11px] bg-accent-dim text-accent-2 grid place-items-center shrink-0"><Icon name={w.icon} size={22} /></div>
                <div className="flex-1 min-w-0">
                  <div className="text-base font-semibold">{w.name}</div>
                </div>
                <button className="btn ghost icon shrink-0" title="Workflow details" onClick={() => setModal({ kind: 'details', wf: w })}><Icon name="info" /></button>
              </div>
              <p className="text-[13px] text-tx-2 leading-[1.5]">{w.desc}</p>
              {w.agents.length > 0 && (
                <div>
                  <div className="text-[10.5px] uppercase tracking-[0.07em] text-tx-3 mb-2">Agent sequence</div>
                  <AgentSequence agents={w.agents} />
                </div>
              )}
              {w.cmds.length > 0 && (
                <div>
                  <div className="text-[10.5px] uppercase tracking-[0.07em] text-tx-3 mb-2">Example commands</div>
                  <div className="flex flex-col gap-1.5">
                    {w.cmds.map((c, i) => (
                      <div className="font-mono text-[11.5px] text-tx-3 bg-bg border border-line-soft rounded-[7px] px-2.5 py-1.5 truncate" key={i}>{c}</div>
                    ))}
                  </div>
                </div>
              )}
              <div className="flex items-center gap-2 mt-auto pt-1">
                <button className="btn ghost" onClick={() => setModal({ kind: 'history', wf: w })}><Icon name="clock" /> History</button>
                <span className="flex-1" />
                {w.source === 'custom' && (
                  <>
                    <button className="btn ghost icon" title="Edit workflow" onClick={() => setModal({ kind: 'edit', wf: w })}><Icon name="edit" /></button>
                    <button className="btn ghost icon danger" title="Delete workflow" onClick={() => setModal({ kind: 'delete', wf: w })}><Icon name="trash" /></button>
                  </>
                )}
                <button className="btn primary" onClick={() => setModal({ kind: 'run', wf: w })}><Icon name="play" /> Run workflow</button>
              </div>
            </div>
          ))}
        </div>
      )}
      {modal?.kind === 'details' && <DetailsModal wf={modal.wf} onClose={close} />}
      {modal?.kind === 'run' && <RunModal wf={modal.wf} onClose={close} onStarted={() => setModal({ kind: 'history', wf: modal.wf })} />}
      {modal?.kind === 'history' && <HistoryModal wf={modal.wf} onClose={close} />}
      {modal?.kind === 'edit' && <EditModal wf={modal.wf} onClose={close} onSaved={() => { close(); reload() }} />}
      {modal?.kind === 'delete' && <DeleteModal wf={modal.wf} onClose={close} onDeleted={() => { close(); reload() }} />}
      {creating && <WorkflowBuilder autoGenerate={creating === 'ai'} onClose={() => setCreating(null)} onSaved={() => { setCreating(null); reload() }} />}
      {phase === 'ready' && rows.length === 0 && (
        <div className="px-[22px] pb-5">
          <button className="btn ghost" onClick={() => goSettings('ai-config')}><Icon name="gear" /> Configure AI models</button>
        </div>
      )}
    </>
  )
}

/* ---------------- Workflow action modals ---------------- */

/** dark-themed labeled text field for the modal forms */
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

function errMsg(e: unknown): string {
  const r = e as { response?: { data?: { detail?: string } }; message?: string }
  return r?.response?.data?.detail || r?.message || 'Something went wrong'
}

/** Labeled text input with a styled, type-to-filter suggestion dropdown
    (uses the redesign's .drop-menu, not the native datalist chrome). */
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

interface WfDetail {
  tools_used?: string[]
  body?: string
}

/** Full read-only details — agent sequence, tools used, and the rendered
    workflow body (the proper description), fetched from GET /workflows/{id}. */
function DetailsModal({ wf, onClose }: { wf: Workflow; onClose: () => void }) {
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

  const tools = detail?.tools_used || []

  return (
    <Popup open onClose={onClose} title={wf.name} width={820}>
      <div className="flex flex-col gap-4">
        {wf.agents.length > 0 && (
          <div className="flex flex-col gap-2">
            <span className="text-[10.5px] uppercase tracking-[0.07em] text-tx-3">Agent sequence</span>
            <AgentSequence agents={wf.agents} />
          </div>
        )}

        {phase === 'ready' && tools.length > 0 && (
          <div className="flex flex-col gap-2">
            <span className="text-[10.5px] uppercase tracking-[0.07em] text-tx-3">Tools used</span>
            <div className="flex flex-wrap gap-1.5">
              {tools.map((t) => (
                <span key={t} className="font-mono text-[11.5px] text-tx-2 bg-bg border border-line-soft rounded-[6px] px-2 py-1">{t}</span>
              ))}
            </div>
          </div>
        )}

        <div className="flex flex-col gap-2">
          <span className="text-[10.5px] uppercase tracking-[0.07em] text-tx-3">Description</span>
          {phase === 'loading' && <div className="muted text-[12.5px]">Loading…</div>}
          {phase === 'error' && <p className="text-[13px] text-tx-2 leading-[1.55]">{wf.desc || 'No description available.'}</p>}
          {phase === 'ready' && (
            detail?.body
              ? <div className="text-[13px] text-tx-2 leading-[1.6] [&_h1]:text-[15px] [&_h1]:font-semibold [&_h2]:text-[13.5px] [&_h2]:font-semibold [&_h1]:mt-1 [&_h2]:mt-2"><Markdown>{detail.body}</Markdown></div>
              : <p className="text-[13px] text-tx-2 leading-[1.55]">{wf.desc || 'No description available.'}</p>
          )}
        </div>

        {wf.cmds.length > 0 && (
          <div className="flex flex-col gap-2">
            <span className="text-[10.5px] uppercase tracking-[0.07em] text-tx-3">Example commands</span>
            <div className="flex flex-col gap-1.5">
              {wf.cmds.map((c, i) => (
                <div className="font-mono text-[11.5px] text-tx-3 bg-bg border border-line-soft rounded-[7px] px-2.5 py-1.5" key={i}>{c}</div>
              ))}
            </div>
          </div>
        )}

        <div className="flex items-center gap-2 text-[11.5px] text-tx-3">
          <span className="mono">{wf.id}</span>
          <span className="chip" style={{ fontSize: 11, padding: '1px 8px' }}>{wf.source === 'custom' ? 'custom' : 'built-in'}</span>
        </div>
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
  /** exact, heuristic, zero or unknown — how confidently the model's rate resolved. */
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

function HypothesisPreview({ text }: { text: string }) {
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
        {beliefs.map((belief, at) => (
          <li key={at} className={looksUnfinished(belief) ? 'suspect' : undefined}>
            <span className="hyp-preview-n">H{at + 1}</span>
            <span>{belief}</span>
          </li>
        ))}
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

/** Run a workflow — collects a target, starts it on the agent layer, then hands
    off to History, which reports phases, beliefs and anything the run waits on. */
export function RunModal({ wf, onStarted, onClose }: { wf: Workflow; onStarted: () => void; onClose: () => void }) {
  const [findingId, setFindingId] = useState('')
  const [caseId, setCaseId] = useState('')
  const [context, setContext] = useState('')
  const [hypothesis, setHypothesis] = useState('')
  const [approve, setApprove] = useState(false)
  const [iterations, setIterations] = useState('')
  const [maxCost, setMaxCost] = useState('')
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [startedId, setStartedId] = useState<string | null>(null) // watched in place, rather than closing
  const [limits, setLimits] = useState<WfLimits | null>(null)
  // Suggestions for the ID fields, fetched from the live findings/cases lists.
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
      const list = (r.data?.cases || []) as { case_id: string; title?: string }[]
      setCaseOpts(list.map((c) => ({ id: c.case_id, label: c.title || '' })))
    }).catch(() => {})
    workflowApi.get(wf.id).then((r) => {
      if (!cancelled) setLimits(r.data as WfLimits)
    }).catch(() => {})
    return () => { cancelled = true }
  }, [wf.id])

  const isHunt = wf.runKind === 'hunt'
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
  // A turn count says how long to run, never what to run on, so it is not a target.
  const withTurns = {
    ...params,
    ...(isHunt && !turnsBad && iterations.trim() && { iterations: turns }),
    ...(isHunt && !costBad && maxCost.trim() && { max_cost_usd: cost }),
    ...(isHunt && approve && { approve_hypotheses: true }),
  }
  // Checked on Run, not per keystroke: a button dead through a sentence reads as an argument.
  const needsHypothesis = isHunt && hypothesis.trim() === ''
  const canRun = Object.keys(params).length > 0 && !turnsBad && !costBad && !starting

  const run = async () => {
    if (needsHypothesis) {
      setError('A hunt tests a claim you state. Put at least one in Hypothesis — the benign account is added for you.')
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
        <p className="text-[12.5px] text-tx-3 leading-[1.5]">Provide at least one target, then start the run — the agents work it on the server and History reports where it got to. A finding or case gives the run something to work from, and the report comes back onto the case you pick. A hunt tests what you state: each line of Hypothesis goes on the board as its own belief, and the benign explanation goes up beside them as the claim to beat.</p>
        {error && <div className="text-[12.5px] leading-[1.5]" style={{ color: 'var(--crit)' }}>{error}</div>}
        {isHunt && <Unpriced pricing={limits?.pricing} />}
        {isHunt && <Blindness unbound={limits?.capabilities?.unbound ?? []} />}
        <ComboField label="Finding ID" value={findingId} onChange={setFindingId} placeholder="f-20260614-3b5c585e" options={findingOpts} hint={findingOpts.length ? `${findingOpts.length} recent findings — start typing to filter.` : undefined} />
        <ComboField label="Case ID" value={caseId} onChange={setCaseId} placeholder="case-2026-0142" options={caseOpts} />
        <Field label="Context" value={context} onChange={setContext} placeholder="Active ransomware on HOST-42…" textarea />
        <Field
          label="Hypothesis"
          value={hypothesis}
          onChange={setHypothesis}
          placeholder="Credentials taken from HOST-42 were reused on another host…"
          textarea
          hint={isHunt
            ? 'One belief per line, each a claim the hunt can argue against. The benign account is added for you as the claim to beat.'
            : undefined}
        />
        {isHunt && <HypothesisPreview text={hypothesis} />}
        {isHunt && (
          <Field
            label="Iterations"
            value={iterations}
            onChange={setIterations}
            placeholder={String(limits?.budgets?.max_iterations ?? 8)}
            hint={turnsBad ? 'A whole number of turns between 1 and 40.' : turnsHint(iterations, maxCost, limits)}
          />
        )}
        {isHunt && (
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
        {isHunt && (
          <label className="flex items-start gap-2 text-[12.5px] leading-[1.5] text-tx-2 cursor-pointer">
            <input type="checkbox" className="mt-0.5" checked={approve} onChange={(e) => setApprove(e.target.checked)} />
            <span>
              Ask me before it starts. The hunt puts its board up and waits for approval in
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

interface WfRun {
  run_id: string
  status: string
  triggered_by?: string
  started_at?: string | null
  duration_ms?: number | null
  total_cost_usd?: number
  error?: string | null
}

function fmtDuration(ms?: number | null): string {
  if (!ms) return '—'
  if (ms < 1000) return `${ms}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.round(ms / 60000)}m`
}

function fmtStarted(iso?: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString()
}

/** Past executions of a workflow (workflow_runs, newest first). */
function HistoryModal({ wf, onClose }: { wf: Workflow; onClose: () => void }) {
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
              {runs.map((r) => <RunRow key={r.run_id} run={r} onRemoved={load} />)}
            </tbody>
          </table>
        </div>
      )}
    </Popup>
  )
}

interface WfPhase {
  phase_id: string
  phase_order: number
  agent_id: string
  status: string
  duration_ms?: number | null
  cost_usd?: number | null
  error?: string | null
}
/** A hunt reports beliefs and where each stands; it has no phases to report against. */
interface HuntStanding {
  hypothesis_id: string
  statement: string
  status: string
  attack_technique?: string | null
  /** Techniques cited by evidence bearing on this belief — earned, not declared. */
  techniques_cited?: string[]
  resolution_reason?: string | null
  /** hunt_spec, operator or base_rate — which belief the operator put up themselves. */
  provenance?: string
}
/** One record the hunt gathered. */
interface HuntEvidence {
  evidence_id: string
  iteration: number
  source_system: string
  summary: string
  why_notable?: string
  salience?: string
  attack_technique?: string | null
  attacker_influenceable?: boolean
  /** Whether anything the finding rests on was attested by the telemetry rather than
   *  authored by the adversary. The projection computes it, so the console shows the
   *  rule a verdict is gated on rather than a second opinion about it. */
  sensor_attested?: boolean
  rests_on?: { field: string; authored: 'sensor' | 'adversary' | 'third_party' }[]
  instruction_like?: boolean
  provenance?: string
  is_gap?: boolean
  /** Why the hunt could not look — kept out of the summary so plumbing is not read
   *  as telemetry, which makes this the only place an operator sees it. */
  gap_detail?: string | null
  bears_on?: { hypothesis_id: string; relation: string }[]
}

/** What the hunt could not answer. A blind spot, not a finding. */
interface HuntGap {
  evidence_id: string
  iteration: number
  summary: string
  query_intent?: string
  hypothesis_id?: string | null
}
interface HuntCheckpoint {
  checkpoint_id: string
  class: string
  raised_iteration?: number
  question: string
  resolution?: { answer: string; actor: string; text?: string } | null
}
/** A lead opened and not yet taken; an operator pins one with a boost directive. */
interface HuntQuestion {
  question_id: string
  question: string
  entity_key?: string | null
  hypothesis_id?: string | null
  spawned_iteration?: number
}

/** One move the Hunt Lead made. The rationale is the whole of why a hunt did what
 *  it did, and rejected_attempts is the only account of a turn that stalled. */
interface HuntMove {
  decision_id: string
  iteration: number
  action: string
  rationale: string
  target_entity?: string | null
  target_hypothesis_id?: string | null
  query_intent?: string
  worker_agent_id?: string | null
  cost_usd?: number
  rejected_attempts?: string[]
}
/** The account of the run, as data. next_steps arrive already normalised to
 *  strings, so this side never has two shapes to read. */
interface HuntNarrative {
  summary: string
  what_happened: string
  next_steps: string[]
  model_id: string
  written_at: string
}

interface HuntHandoff {
  case_id: string
  hypothesis_id: string
  iteration: number
  rationale: string
}
interface HuntStrength {
  corroborating_sources: number
  contradicting_records: number
  open_gaps: number
  attacker_influenceable_only: boolean
  survived_disconfirmation: boolean
}
/** The derived deliverable. Null until the hunt writes one, so the panel reads
 *  the live fields until it exists and the report itself afterwards. */
interface HuntReport {
  gaps: HuntGap[]
  checkpoints: HuntCheckpoint[]
  hypotheses: { hypothesis_id: string; evidence_strength?: HuntStrength | null }[]
  unruled?: number
}
interface HuntBudgets {
  max_iterations: number
  max_cost_usd: number
}
interface HuntView {
  run_id?: string
  status: string
  /** Why it ended, which is not whether it succeeded: a hunt stopped at its ceiling
   *  finalises as completed. */
  outcome?: string | null
  /** Which arm of the budget bound, or what an operator did. Not an error. */
  reason?: string | null
  iteration: number
  evidence_count: number
  /** Capped by the projection; evidence_count stays the untruncated total. */
  evidence?: HuntEvidence[]
  cost_usd?: number
  /** What this run was granted, extensions included — not the shipped default. */
  budgets?: HuntBudgets
  hypotheses: HuntStanding[]
  open_checkpoint?: {
    checkpoint_id: string
    checkpoint_class?: string
    question: string
    raised_at?: string
    context?: Record<string, unknown>
  } | null
  report?: HuntReport | null
  report_markdown?: string | null
  narrative?: HuntNarrative | null
  handoffs?: HuntHandoff[]
  moves?: HuntMove[]
  open_questions?: HuntQuestion[]
}
interface WfRunDetail extends WfRun {
  result_summary?: string | null
  phases?: WfPhase[]
  hunt?: HuntView | null
}

const RUN_POLL_MS = 5_000
const IN_FLIGHT = ['running', 'paused', 'pending']

/** One run's detail, refreshed while it is in flight. Shared by the start modal and
 *  the history row so one run has one poller. */
export function useRunDetail(runId: string, watching: boolean, seed?: string) {
  const [detail, setDetail] = useState<WfRunDetail | null>(null)
  const [dphase, setDphase] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle')

  const load = useCallback(
    () =>
      workflowApi
        .getRun(runId)
        .then((res) => { setDetail(res.data as WfRunDetail); setDphase('ready') })
        .catch(() => setDphase((p) => (p === 'ready' ? p : 'error'))),
    [runId],
  )

  // Stops itself at a terminal status rather than polling for the session.
  const live = watching && IN_FLIGHT.includes(detail?.status ?? seed ?? 'running')
  useEffect(() => {
    if (!live) return
    const timer = setInterval(() => { void load() }, RUN_POLL_MS)
    return () => clearInterval(timer)
  }, [live, load])

  return { detail, dphase, setDphase, load }
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
function RunRow({ run, onRemoved }: { run: WfRun; onRemoved: () => void }) {
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
          {run.error && <span className="ml-2" style={{ color: 'var(--crit)' }} title={run.error}>⚠</span>}
        </td>
        <td className="muted">{fmtStarted(run.started_at)}</td>
        <td className="muted">{fmtDuration(run.duration_ms)}</td>
        <td className="muted">{run.triggered_by || '—'}</td>
        <td className="muted">{run.total_cost_usd ? `$${run.total_cost_usd.toFixed(3)}` : '—'}</td>
        <td className="tight" onClick={(e) => e.stopPropagation()}><RemoveRun run={run} onRemoved={onRemoved} /></td>
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
  return (
    <div className="run-detail">
      <RunBar d={d} hunt={hunt} onSteered={onSteered} />
      {hunt && <OpenCheckpoint hunt={hunt} />}
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
      {hunt ? <HuntTabs d={d} hunt={hunt} /> : <ComposeDetail d={d} />}
      {IN_FLIGHT.includes(d.status) && <Steer runId={d.run_id} hunt={hunt !== null} onSteered={onSteered} />}
    </div>
  )
}

/** What the run is doing, and Stop. It sits with the status rather than among the
 *  steering directives, which are only notes the lead reads at its next turn. */
function RunBar({ d, hunt, onSteered }: { d: WfRunDetail; hunt: HuntView | null; onSteered: () => void }) {
  const cost = hunt?.cost_usd ?? d.total_cost_usd
  const budgets = hunt?.budgets
  const ceiling = budgets?.max_cost_usd
  const spent = typeof cost === 'number' && ceiling !== undefined && ceiling > 0
    ? Math.min(100, (cost / ceiling) * 100)
    : null
  return (
    <div className="run-bar">
      <span className="pill" style={{ color: runStatusColor(d.status) }}><span className="dot" />{d.status}</span>
      {hunt?.outcome && !IN_FLIGHT.includes(d.status) && (
        <span className="muted text-[11.5px]" title={hunt.reason ?? undefined}>{hunt.outcome}</span>
      )}
      <span className="mono text-[11.5px] text-tx-3">{d.run_id.slice(0, 13)}</span>
      <span className="flex-1" />
      <div className="meta">
        {hunt && <span>Iteration <b>{hunt.iteration}</b>{budgets && ` of ${budgets.max_iterations}`}</span>}
        {typeof cost === 'number' && <span>$<b>{cost.toFixed(2)}</b>{ceiling !== undefined && ` of $${ceiling.toFixed(2)}`}</span>}
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

type HuntTab = 'hyp' | 'evidence' | 'moves' | 'frontier' | 'gaps' | 'esc' | 'report' | 'next'

/** Views of one run as tabs rather than stacked tables. A view with nothing in it
 *  is not offered rather than offered empty. */
function HuntTabs({ d, hunt }: { d: WfRunDetail; hunt: HuntView }) {
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
  const tabs: [HuntTab, string, number | null][] = [
    ['hyp', 'Hypotheses', hunt.hypotheses.length],
    ...(hunt.evidence_count > 0 ? ([['evidence', 'Evidence', hunt.evidence_count]] as [HuntTab, string, number][]) : []),
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
      {shown === 'hyp' && <HuntStandings hunt={hunt} />}
      {shown === 'evidence' && <HuntEvidenceTable found={found} total={hunt.evidence_count} />}
      {shown === 'moves' && <HuntMoves moves={moves} />}
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
        : <HuntAccount hunt={hunt} report={report} gaps={gaps.length} onGo={setTab} />)}
      {shown === 'next' && <HuntActions hunt={hunt} />}
    </HypLabels.Provider>
  )
}

/** The account, laid out — what happened, with the tabs carrying the evidence and
 *  metadata the report also lists. The markdown itself is untouched. */
function HuntAccount({ hunt, report, gaps, onGo }: { hunt: HuntView; report: string; gaps: number; onGo: (tab: HuntTab) => void }) {
  const account = hunt.narrative ?? null
  if (account === null) { // no narrative written: show the whole report rather than nothing
    return (
      <>
        <div className="hunt-account-foot" style={{ marginTop: 0, borderTop: 'none', paddingTop: 0 }}>
          <span className="muted text-[11.5px]">No account was written for this run.</span>
          <span className="flex-1" />
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
      <Incidents md={account.what_happened} />
      <div className="hunt-account-foot">
        <span className="muted text-[11.5px]">
          {hunt.evidence_count} record(s) gathered · {gaps} blind spot(s)
        </span>
        <button className="btn ghost text-[11px]" onClick={() => onGo('evidence')}>Evidence ▸</button>
        {gaps > 0 && <button className="btn ghost text-[11px]" onClick={() => onGo('gaps')}>Gaps ▸</button>}
        <span className="flex-1" />
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

/** Every move the lead made and why — the only account of what a turn decided, and
 *  of a turn that stalled. */
function HuntMoves({ moves }: { moves: HuntMove[] }) {
  return (
    <div style={{ marginTop: 12 }}>
      <div className="muted text-[11.5px] mb-2">Newest first. One decision per turn; a turn may re-ask after a refused emission.</div>
      <div className="table-wrap">
        <table className="tbl">
          <thead><tr><th className="tight">Turn</th><th className="tight">Move</th><th>Why</th><th className="tight">On</th></tr></thead>
          <tbody>
            {moves.map((m) => (
              <tr key={m.decision_id}>
                <td className="muted tight">{m.iteration}</td>
                <td className="tight mono text-[11px]">{m.action}</td>
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
            ))}
          </tbody>
        </table>
      </div>
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

/** A run that walks phases has steps and a summary; there is nothing to tab between. */
function ComposeDetail({ d }: { d: WfRunDetail }) {
  const agentMeta = useAgentMeta()
  if (!d.result_summary && !d.phases?.length) {
    return d.error ? null : <div className="muted" style={{ padding: '10px 4px' }}>No additional detail recorded for this run.</div>
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
                    <td className="muted tight">{p.cost_usd ? `$${p.cost_usd.toFixed(3)}` : '—'}</td>
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

/** Steer a run that is still going. Queued for the worker holding the ledger, so
 *  nothing here is instant and the panel re-reads rather than claiming it obeyed. */
/** What one press of extend buys. Sent as a typed grant rather than as prose the run
 *  has to parse: `extend` with an empty note parsed to nothing, journaled a note saying
 *  so, and left the hunt parked at the ceiling it was asking to be let past. */
const GRANTS: [string, { iterations: number; cost_usd: number; wall_ms: number }][] = [
  ['+3 turns', { iterations: 3, cost_usd: 0, wall_ms: 0 }],
  ['+$5', { iterations: 0, cost_usd: 5, wall_ms: 0 }],
  ['+30 min', { iterations: 0, cost_usd: 0, wall_ms: 30 * 60_000 }],
]

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

function runStatusColor(s: string): string {
  if (s === 'completed') return 'var(--ok)'
  if (s === 'failed' || s === 'cancelled') return 'var(--crit)'
  if (s === 'paused') return 'var(--high)'
  return 'var(--med)' // running
}

/** Edit a custom workflow's metadata (name / description / use case / triggers). */
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

/** Delete a custom workflow (with confirmation). */
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

/* ---------------- Agents tab ---------------- */
function AgentsTab() {
  const { rows, phase, error, reload } = useAgents()
  const [busy, setBusy] = useState<string | null>(null)
  const [editId, setEditId] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [deleteAgent, setDeleteAgent] = useState<AgentTemplate | null>(null)

  const builtins = rows.filter((a) => !a.custom)
  const customs = rows.filter((a) => a.custom)

  // Fork → create the editable copy, refresh, then open its editor (mirrors
  // the old Agent Builder "fork → opening editor" flow).
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

  return (
    <>
      <div className="flex items-start gap-4 flex-wrap px-[22px] pt-5 pb-[6px]">
        <div className="flex-1 min-w-[200px]"><h2 className="text-[19px]">SOC Agents</h2>
          <p className="text-[13px] text-tx-3 mt-[5px] max-w-[640px] leading-[1.5]">Built-in agents are read-only templates. Fork one to create an editable custom copy, or start from scratch with “New Agent”.</p></div>
        <div className="flex items-center gap-2.5 flex-wrap">
          <button className="btn primary" onClick={() => setCreating(true)}><Icon name="plus" /> New Agent</button>
          <button className="btn ghost icon" title="Refresh" onClick={reload}><Icon name="refresh" /></button>
        </div>
      </div>

      {phase === 'loading' && <StateMsg><EmptyState loading compact icon="brain" title="Loading agents…" /></StateMsg>}
      {phase === 'error' && <StateMsg><EmptyState error icon="alert" title="Couldn’t load agents" body={error} primary={{ label: 'Retry', onClick: reload, icon: 'refresh' }} /></StateMsg>}
      {phase === 'ready' && rows.length === 0 && <StateMsg><EmptyState icon="brain" title="No agents yet" body="Create a custom SOC agent or refresh to load built-in templates." primary={{ label: 'New agent', onClick: () => setCreating(true), icon: 'plus' }} secondary={{ label: 'Refresh', onClick: reload, icon: 'refresh' }} /></StateMsg>}

      {phase === 'ready' && rows.length > 0 && (
        // Two-up (Custom | Built-in) when forked copies exist; otherwise the
        // built-in table goes full width.
        <div
          className="grid gap-x-6 gap-y-2 px-[22px] pb-[22px] items-start"
          style={{ gridTemplateColumns: customs.length > 0 ? 'repeat(auto-fit, minmax(440px, 1fr))' : '1fr' }}
        >
          {customs.length > 0 && (
            <AgentSection title={`Custom agents (${customs.length})`} agents={customs} renderActions={(a) => (
              <span className="row-act">
                <button title="Edit" onClick={() => setEditId(a.handle)}><Icon name="edit" /></button>
                <button title="Fork into a new copy" disabled={busy !== null} onClick={() => fork(a.handle)}><Icon name={busy === a.handle ? 'refresh' : 'copy'} /></button>
                <button title="Delete" onClick={() => setDeleteAgent(a)}><Icon name="trash" /></button>
              </span>
            )} />
          )}
          <AgentSection title={`Built-in templates (${builtins.length})`} agents={builtins} template renderActions={(a) => (
            <span className="row-act">
              <button title="Fork to editable copy" disabled={busy !== null} onClick={() => fork(a.handle)}><Icon name={busy === a.handle ? 'refresh' : 'fork'} /></button>
            </span>
          )} />
        </div>
      )}

      {(creating || editId) && (
        <AgentEditModal
          agentId={editId}
          onClose={() => { setEditId(null); setCreating(false) }}
          onSaved={() => { setEditId(null); setCreating(false); reload() }}
        />
      )}
      {deleteAgent && <AgentDeleteModal agent={deleteAgent} onClose={() => setDeleteAgent(null)} onDeleted={() => { setDeleteAgent(null); reload() }} />}
    </>
  )
}

/** A titled agents section (column header + table) used in the two-up grid. */
function AgentSection({ title, agents, template, renderActions }: {
  title: string
  agents: AgentTemplate[]
  template?: boolean
  renderActions: (a: AgentTemplate) => React.ReactNode
}) {
  return (
    <div className="min-w-0">
      <div className="pt-[14px] pb-2.5 text-[11px] font-semibold tracking-[0.07em] uppercase text-tx-3">{title}</div>
      <AgentTable agents={agents} template={template} renderActions={renderActions} />
    </div>
  )
}

/** Shared agents table; `template` toggles the read-only Template badge. */
function AgentTable({ agents, template, renderActions }: {
  agents: AgentTemplate[]
  template?: boolean
  renderActions: (a: AgentTemplate) => React.ReactNode
}) {
  return (
    <div className="table-wrap border border-line rounded-lg overflow-hidden">
      <table className="tbl agents-tbl">
        <thead><tr>
          <th>Name</th><th>Specialization</th>
          <th className="ag-c">Tools</th><th className="ag-c">Actions</th>
        </tr></thead>
        <tbody>
          {agents.map((a) => (
            <tr key={a.handle}>
              <td>
                <div className="flex items-center gap-3">
                  <span className="ag-avatar" style={{ background: a.color }}>{a.ini}</span>
                  <div className="ag-meta">
                    <div className="text-[13.5px] font-semibold flex items-center gap-2.5">{a.name} {template && <span className="tmpl-badge"><Icon name="lock" /> Template</span>}</div>
                    <div className="text-[11.5px] text-tx-3 mt-[3px] mono">{a.handle}</div>
                  </div>
                </div>
              </td>
              <td>{a.spec}</td>
              <td className="muted ag-c">{a.tools ?? '—'}</td>
              <td className="ag-c">{renderActions(a)}</td>
            </tr>
          ))}
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
}

const BLANK_AGENT_FORM: AgentForm = {
  name: '', specialization: '', description: '', icon: '', color: '#7d74f3', role: '',
  extra_principles: '', methodology: '', system_prompt_override: '', recommended_tools: '',
  max_tokens: '', enable_thinking: false,
}

/** Create or edit a custom agent — full field set + AI-assisted drafting,
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

  // AI assist (agentsApi.generateCustom) — describe → draft → iterative refine.
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
          <p className="text-[11.5px] text-tx-3 -mt-2">Rendered into the Vigil base prompt (preserves mempalace + entity-recognition directives).</p>
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

/** Delete a custom agent (the source template, if any, is unaffected). */
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

/* ---------------- Skills tab ---------------- */
function SkillsTab() {
  const { rows, phase, error, reload, toggleActive } = useSkills()
  const [building, setBuilding] = useState(false)
  const [toDelete, setToDelete] = useState<Skill | null>(null)
  const [importErr, setImportErr] = useState<string | null>(null)
  const [importing, setImporting] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  const onImport = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    e.target.value = '' // allow re-selecting the same file
    if (!file) return
    setImporting(true)
    setImportErr(null)
    skillsApi
      .importZip(file)
      .then(() => reload())
      .catch((err) => setImportErr(errMsg(err)))
      .finally(() => setImporting(false))
  }

  return (
    <>
      <div className="flex items-start gap-4 flex-wrap px-[22px] pt-5 pb-[6px]">
        <div className="flex-1 min-w-[200px]"><h2 className="text-[19px]">Skills</h2>
          <p className="text-[13px] text-tx-3 mt-[5px] max-w-[640px] leading-[1.5]">Reusable, parameterized capabilities agents and workflows can invoke.</p></div>
        <div className="flex items-center gap-2.5 flex-wrap">
          <button className="btn ghost" onClick={reload}><Icon name="refresh" /> Refresh</button>
          <button className="btn ghost" disabled={importing} onClick={() => fileRef.current?.click()}><Icon name="upload" /> {importing ? 'Importing…' : 'Import Zip'}</button>
          <input ref={fileRef} type="file" accept=".zip,application/zip" hidden onChange={onImport} />
          <button className="btn primary" onClick={() => setBuilding(true)}><Icon name="sparkle" /> Build Skill</button>
        </div>
      </div>
      {importErr && <div className="px-[22px] text-[12.5px]" style={{ color: 'var(--crit)' }}>Import failed: {importErr}</div>}
      {phase === 'loading' && <StateMsg><EmptyState loading compact icon="sparkle" title="Loading skills…" /></StateMsg>}
      {phase === 'error' && <StateMsg><EmptyState error icon="alert" title="Couldn’t load skills" body={error} primary={{ label: 'Retry', onClick: reload, icon: 'refresh' }} /></StateMsg>}
      {phase === 'ready' && rows.length === 0 && <StateMsg><EmptyState icon="sparkle" title="No skills yet" body="Build or import reusable capabilities that agents and workflows can invoke." primary={{ label: 'Build skill', onClick: () => setBuilding(true), icon: 'sparkle' }} secondary={{ label: 'Import Zip', onClick: () => fileRef.current?.click(), icon: 'upload' }} /></StateMsg>}
      {phase === 'ready' && rows.length > 0 && (
        <div className="grid gap-4 px-[22px] pt-[14px] pb-6 [grid-template-columns:repeat(auto-fill,minmax(360px,1fr))]">
          {rows.map((s) => (
            <div className="flex flex-col gap-[9px] bg-panel border border-line rounded-lg p-[18px] shadow-panel transition-[border-color,transform] duration-150 hover:border-[#2e3744] hover:-translate-y-0.5" key={s.id}>
              <div className="flex items-start gap-2.5">
                <h3 className="text-base flex-1 min-w-0">{s.name}</h3>
                <span className={`sk-tag ${s.cat}`}>{s.cat === 'custom' ? 'custom' : 'built-in'}</span>
              </div>
              <div className="text-[11.5px] text-tx-3 mono">{s.id} · {s.v}</div>
              <p className="text-[13px] text-tx-2 leading-[1.5] flex-1">{s.desc}</p>
              <div className="flex items-center gap-2.5 mt-1.5">
                <span
                  className={`sk-toggle${s.active ? ' on' : ''}`}
                  role="switch"
                  aria-checked={s.active}
                  aria-label={`${s.active ? 'Deactivate' : 'Activate'} ${s.name}`}
                  tabIndex={0}
                  onClick={() => toggleActive(s.id)}
                  onKeyDown={activateOnKey(() => toggleActive(s.id))}
                ><span className="kn" /></span>
                <span className="text-[12.5px] text-tx-2">{s.active ? 'Active' : 'Inactive'}</span>
                <button className="sk-del" title="Delete skill" onClick={() => setToDelete(s)}><Icon name="trash" /></button>
              </div>
            </div>
          ))}
        </div>
      )}
      {building && <BuildSkillModal onClose={() => setBuilding(false)} onCreated={() => { setBuilding(false); reload() }} />}
      {toDelete && <SkillDeleteModal skill={toDelete} onClose={() => setToDelete(null)} onDeleted={() => { setToDelete(null); reload() }} />}
    </>
  )
}

/** Delete a skill (with confirmation). */
function SkillDeleteModal({ skill, onClose, onDeleted }: { skill: Skill; onClose: () => void; onDeleted: () => void }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const del = () => {
    setBusy(true)
    setError(null)
    skillsApi.remove(skill.id).then(onDeleted).catch((e) => { setError(errMsg(e)); setBusy(false) })
  }
  return (
    <Popup open onClose={onClose} title="Delete skill" width={460}>
      <div className="flex flex-col gap-3.5">
        <p className="text-[13px] text-tx-2 leading-[1.5]">Delete <strong>{skill.name}</strong>? This permanently removes the skill.</p>
        {error && <div className="text-[12.5px]" style={{ color: 'var(--crit)' }}>{error}</div>}
        <div className="flex justify-end gap-2.5 pt-1">
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn danger" disabled={busy} style={{ opacity: busy ? 0.5 : 1 }} onClick={del}><Icon name="trash" /> {busy ? 'Deleting…' : 'Delete'}</button>
        </div>
      </div>
    </Popup>
  )
}

/** Build a skill with AI — describe it, answer any clarifying question, then
    review the generated draft and save it. Wraps skillsApi.generate + create. */
function BuildSkillModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [description, setDescription] = useState('')
  const [category, setCategory] = useState<SkillCategory>('custom')
  const [history, setHistory] = useState<{ role: string; content: string }[] | null>(null)
  const [clarify, setClarify] = useState<string | null>(null) // pending question from the AI
  const [answer, setAnswer] = useState('')
  const [draft, setDraft] = useState<SkillDraft | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const runGenerate = (userResponse?: string) => {
    setBusy(true)
    setError(null)
    skillsApi
      .generate({
        description: description.trim(),
        category,
        conversation_history: history,
        user_response: userResponse ?? null,
      })
      .then((res) => {
        if (!res.success) { setError(res.error || res.message || 'Generation failed'); return }
        setHistory(res.conversation_history || history)
        if (res.needs_clarification) {
          setClarify(res.message || 'The builder needs more detail.')
          setDraft(null)
        } else if (res.skill) {
          setClarify(null)
          setAnswer('')
          setDraft(res.skill)
        }
      })
      .catch((e) => setError(errMsg(e)))
      .finally(() => setBusy(false))
  }

  const save = () => {
    if (!draft) return
    setBusy(true)
    setError(null)
    skillsApi.create(draft).then(onCreated).catch((e) => { setError(errMsg(e)); setBusy(false) })
  }

  return (
    <Popup open onClose={onClose} title="Build skill" width={620}>
      <div className="flex flex-col gap-3.5">
        <Field label="Describe the skill" value={description} onChange={setDescription} textarea placeholder="e.g. Enrich an IP with reputation, WHOIS and passive DNS, returning a normalized verdict." />
        <label className="flex flex-col gap-1.5">
          <span className="text-[11px] uppercase tracking-[0.06em] text-tx-3">Category</span>
          <select className="w-full bg-bg border border-line rounded-[7px] px-2.5 py-2 text-[13px] text-tx outline-none focus:border-accent-line" value={category} onChange={(e) => setCategory(e.target.value as SkillCategory)}>
            {SKILL_CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </label>

        {clarify && (
          <div className="flex flex-col gap-2 border border-line rounded-[8px] p-3 bg-bg">
            <span className="text-[11px] uppercase tracking-[0.06em] text-tx-3 flex items-center gap-1.5"><Icon name="reason" size={13} /> The builder needs more detail</span>
            <p className="text-[13px] text-tx-2 leading-[1.5]">{clarify}</p>
            <Field label="Your answer" value={answer} onChange={setAnswer} textarea />
            <div className="flex justify-end">
              <button className="btn primary" disabled={!answer.trim() || busy} style={{ opacity: !answer.trim() || busy ? 0.5 : 1 }} onClick={() => runGenerate(answer.trim())}>{busy ? 'Thinking…' : 'Send answer'}</button>
            </div>
          </div>
        )}

        {draft && (
          <div className="flex flex-col gap-2 border border-line rounded-[8px] p-3 bg-bg">
            <div className="flex items-center gap-2">
              <span className="text-[14px] font-semibold flex-1">{draft.name}</span>
              <span className="sk-tag custom">{draft.category}</span>
            </div>
            {draft.description && <p className="text-[13px] text-tx-2 leading-[1.5]">{draft.description}</p>}
            {draft.required_tools?.length > 0 && (
              <div className="flex flex-wrap gap-1.5">
                {draft.required_tools.map((t) => <span key={t} className="font-mono text-[11px] text-tx-2 bg-panel border border-line-soft rounded-[6px] px-2 py-0.5">{t}</span>)}
              </div>
            )}
          </div>
        )}

        {error && <div className="text-[12.5px]" style={{ color: 'var(--crit)' }}>{error}</div>}
        <div className="flex justify-end gap-2.5 pt-1">
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          {draft ? (
            <button className="btn primary" disabled={busy} style={{ opacity: busy ? 0.5 : 1 }} onClick={save}><Icon name="check2" /> {busy ? 'Saving…' : 'Create skill'}</button>
          ) : (
            <button className="btn primary" disabled={!description.trim() || busy || !!clarify} style={{ opacity: !description.trim() || busy || !!clarify ? 0.5 : 1 }} onClick={() => runGenerate()}><Icon name="sparkle" /> {busy ? 'Generating…' : 'Generate'}</button>
          )}
        </div>
      </div>
    </Popup>
  )
}
