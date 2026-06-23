/* ============================================================
   Workflows screen — Workflows · Agents · Skills tabs
   Wired to the real backend (workflowApi / agentsApi / skillsApi)
   via useWorkflows / useAgents / useSkills, with loading / empty /
   error states. See REDESIGN_GAPS.md §9.
   ============================================================ */
import { Fragment, useEffect, useRef, useState } from 'react'
import { Icon } from '../../shared/icons'
import { Popup, activateOnKey } from '../../shared/ui'
import { Markdown } from '../../shared/Markdown'
import { AGENT_META, prettyHandle, type Workflow, type AgentTemplate } from '../../data/appData'
import { useWorkflows, useAgents, useSkills } from './useWorkflowsData'
import { workflowApi, agentsApi, findingsApi, casesApi, type GeneratedAgentDraft } from '../../../services/api'
import { skillsApi, SKILL_CATEGORIES, type SkillCategory, type SkillDraft } from '../../../services/skillsApi'
import WorkflowBuilder from './WorkflowBuilder'
import type { Skill } from '../../data/appData'
import type { ScreenProps } from '../../shared/types'

type WfTab = 'workflows' | 'agents' | 'skills'

export default function WorkflowsScreen({ openChat }: ScreenProps) {
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
      {tab === 'workflows' && <WorkflowCatalog openChat={openChat} />}
      {tab === 'agents' && <AgentsTab />}
      {tab === 'skills' && <SkillsTab />}
    </>
  )
}

/* ---- shared empty/loading/error row ---- */
function StateMsg({ children }: { children: React.ReactNode }) {
  return (
    <div className="muted" style={{ padding: '40px 22px', textAlign: 'center' }}>
      {children}
    </div>
  )
}

/* ---- agent sequence ---- */
function AgentSequence({ agents }: { agents: string[] }) {
  return (
    <div className="agent-seq">
      {agents.map((a, i) => {
        const meta = AGENT_META[a]
        return (
          <Fragment key={i}>
            <span className="agent-chip">
              <span className="ad" style={{ background: meta?.color || 'var(--accent)' }} />
              {meta?.label || prettyHandle(a)}
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

function WorkflowCatalog({ openChat }: { openChat: (prompt?: string) => void }) {
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
      {phase === 'loading' && <StateMsg>Loading workflows…</StateMsg>}
      {phase === 'error' && <StateMsg>Couldn’t load workflows: {error}</StateMsg>}
      {phase === 'ready' && list.length === 0 && (
        <StateMsg>{q ? 'No workflows match your search.' : 'No workflows available.'}</StateMsg>
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
      {modal?.kind === 'run' && <RunModal wf={modal.wf} openChat={openChat} onClose={close} />}
      {modal?.kind === 'history' && <HistoryModal wf={modal.wf} onClose={close} />}
      {modal?.kind === 'edit' && <EditModal wf={modal.wf} onClose={close} onSaved={() => { close(); reload() }} />}
      {modal?.kind === 'delete' && <DeleteModal wf={modal.wf} onClose={close} onDeleted={() => { close(); reload() }} />}
      {creating && <WorkflowBuilder autoGenerate={creating === 'ai'} onClose={() => setCreating(null)} onSaved={() => { setCreating(null); reload() }} />}
    </>
  )
}

/* ---------------- Workflow action modals ---------------- */

/** dark-themed labeled text field for the modal forms */
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
  const cls = `w-full bg-bg border border-line rounded-[7px] px-2.5 py-2 text-[13px] text-tx outline-none focus:border-accent-line${mono ? ' font-mono' : ''}`
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
          className="w-full bg-bg border border-line rounded-[7px] px-2.5 py-2 text-[13px] text-tx font-mono outline-none focus:border-accent-line"
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

/** Compose the chat prompt that runs a workflow (mirrors the old app's
    buildSkillPrompt — the run happens as a streamed chat conversation). */
function buildRunPrompt(wf: Workflow, p: { finding_id?: string; case_id?: string; context?: string; hypothesis?: string }): string {
  const seq = wf.agents.map((a) => AGENT_META[a]?.label || prettyHandle(a)).join(' → ')
  let prompt = `Please execute the **${wf.name}** workflow.\n\n`
  if (seq) prompt += `**Agent sequence:** ${seq}\n\n`
  if (p.finding_id) prompt += `**Target Finding:** ${p.finding_id}\n`
  if (p.case_id) prompt += `**Target Case:** ${p.case_id}\n`
  if (p.hypothesis) prompt += `**Hunt Hypothesis:** ${p.hypothesis}\n`
  if (p.context) prompt += `**Context:** ${p.context}\n`
  return prompt.trim()
}

/** Run a workflow — collects a target, then sends it to the Vigil chat where
    the response streams (matching the old app's "launch in chat" behavior). */
function RunModal({ wf, openChat, onClose }: { wf: Workflow; openChat: (prompt?: string) => void; onClose: () => void }) {
  const [findingId, setFindingId] = useState('')
  const [caseId, setCaseId] = useState('')
  const [context, setContext] = useState('')
  const [hypothesis, setHypothesis] = useState('')
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
    return () => { cancelled = true }
  }, [])

  const params = {
    ...(findingId.trim() && { finding_id: findingId.trim() }),
    ...(caseId.trim() && { case_id: caseId.trim() }),
    ...(context.trim() && { context: context.trim() }),
    ...(hypothesis.trim() && { hypothesis: hypothesis.trim() }),
  }
  const canRun = Object.keys(params).length > 0

  const run = () => {
    openChat(buildRunPrompt(wf, params))
    onClose()
  }

  return (
    <Popup open onClose={onClose} title={`Run · ${wf.name}`}>
      <div className="flex flex-col gap-3.5">
        <p className="text-[12.5px] text-tx-3 leading-[1.5]">Provide at least one target, then run it in the Vigil chat — the agents stream their work there. Findings and cases drive the investigation; context and hypothesis steer hunts.</p>
        <ComboField label="Finding ID" value={findingId} onChange={setFindingId} placeholder="f-20260614-3b5c585e" options={findingOpts} hint={findingOpts.length ? `${findingOpts.length} recent findings — start typing to filter.` : undefined} />
        <ComboField label="Case ID" value={caseId} onChange={setCaseId} placeholder="case-2026-0142" options={caseOpts} />
        <Field label="Context" value={context} onChange={setContext} placeholder="Active ransomware on HOST-42…" textarea />
        <Field label="Hypothesis" value={hypothesis} onChange={setHypothesis} placeholder="Lateral movement in the finance subnet…" textarea />
        <div className="flex justify-end gap-2.5 pt-1">
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={!canRun} style={{ opacity: canRun ? 1 : 0.5 }} onClick={run}>
            <Icon name="send" /> Run in chat
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

  useEffect(() => {
    let cancelled = false
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

  return (
    <Popup open onClose={onClose} title={`History · ${wf.name}`} width={720}>
      {phase === 'loading' && <div className="muted" style={{ padding: '24px 0', textAlign: 'center' }}>Loading run history…</div>}
      {phase === 'error' && <div className="muted" style={{ padding: '24px 0', textAlign: 'center' }}>Couldn’t load history: {error}</div>}
      {phase === 'ready' && runs.length === 0 && <div className="muted" style={{ padding: '24px 0', textAlign: 'center' }}>No runs yet for this workflow.</div>}
      {phase === 'ready' && runs.length > 0 && (
        <div className="table-wrap">
          <table className="tbl">
            <thead><tr><th /><th>Status</th><th>Started</th><th>Duration</th><th>Trigger</th><th>Cost</th></tr></thead>
            <tbody>
              {runs.map((r) => <RunRow key={r.run_id} run={r} />)}
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
interface WfRunDetail extends WfRun {
  result_summary?: string | null
  phases?: WfPhase[]
}

/** A run row that lazily fetches its full detail (getRun) when expanded. */
function RunRow({ run }: { run: WfRun }) {
  const [open, setOpen] = useState(false)
  const [detail, setDetail] = useState<WfRunDetail | null>(null)
  const [dphase, setDphase] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle')

  const toggle = () => {
    const next = !open
    setOpen(next)
    if (next && dphase === 'idle') {
      setDphase('loading')
      workflowApi
        .getRun(run.run_id)
        .then((res) => { setDetail(res.data as WfRunDetail); setDphase('ready') })
        .catch(() => setDphase('error'))
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
      </tr>
      {open && (
        <tr className="run-detail-row">
          <td colSpan={6}>
            {dphase === 'loading' && <div className="muted" style={{ padding: '10px 4px' }}>Loading run detail…</div>}
            {dphase === 'error' && <div className="muted" style={{ padding: '10px 4px' }}>Couldn’t load run detail.</div>}
            {dphase === 'ready' && detail && <RunDetail d={detail} />}
          </td>
        </tr>
      )}
    </>
  )
}

function RunDetail({ d }: { d: WfRunDetail }) {
  return (
    <div className="run-detail">
      {d.error && (
        <div className="modal-section" style={{ marginTop: 4 }}>
          <h4 style={{ color: 'var(--crit)' }}>Error</h4>
          <pre className="font-mono text-[11.5px] leading-[1.5] whitespace-pre-wrap m-0" style={{ color: 'var(--crit)' }}>{d.error}</pre>
        </div>
      )}
      {d.result_summary && (
        <div className="modal-section" style={{ marginTop: 4 }}>
          <h4>Result summary</h4>
          <div className="text-[12.5px] text-tx-2 leading-[1.55]"><Markdown>{d.result_summary}</Markdown></div>
        </div>
      )}
      {!!d.phases?.length && (
        <div className="modal-section" style={{ marginTop: 12 }}>
          <h4>Phases</h4>
          <table className="tbl">
            <thead><tr><th>#</th><th>Agent</th><th>Status</th><th>Duration</th><th>Cost</th></tr></thead>
            <tbody>
              {d.phases.map((p) => (
                <tr key={p.phase_id}>
                  <td className="muted">{p.phase_order}</td>
                  <td>{AGENT_META[p.agent_id]?.label || prettyHandle(p.agent_id)}{p.error && <span className="ml-2" style={{ color: 'var(--crit)' }} title={p.error}>⚠</span>}</td>
                  <td><span style={{ color: runStatusColor(p.status) }}>{p.status}</span></td>
                  <td className="muted">{fmtDuration(p.duration_ms)}</td>
                  <td className="muted">{p.cost_usd ? `$${p.cost_usd.toFixed(3)}` : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {!d.error && !d.result_summary && !d.phases?.length && (
        <div className="muted" style={{ padding: '10px 4px' }}>No additional detail recorded for this run.</div>
      )}
    </div>
  )
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

      {phase === 'loading' && <StateMsg>Loading agents…</StateMsg>}
      {phase === 'error' && <StateMsg>Couldn’t load agents: {error}</StateMsg>}
      {phase === 'ready' && rows.length === 0 && <StateMsg>No agents available.</StateMsg>}

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
      {phase === 'loading' && <StateMsg>Loading skills…</StateMsg>}
      {phase === 'error' && <StateMsg>Couldn’t load skills: {error}</StateMsg>}
      {phase === 'ready' && rows.length === 0 && <StateMsg>No skills available.</StateMsg>}
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
