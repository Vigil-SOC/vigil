// Build a workflow by hand: drag agents in as steps, put them in order, say what
// each one does and which tools it may use, and mark where a person approves.
// The same screen edits a saved workflow and finishes an AI draft. Nothing is
// saved until Save, and the server checks the result as it checks any other.
import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent, type ReactNode } from 'react'
import { Icon } from '../../shared/icons'
import { agentsApi, workflowApi } from '../../services/api'
import ToolPicker from './ToolPicker'
import { useToolCatalog } from './toolCatalog'
import {
  EMPTY_DRAFT, blankStep, checkDraft, draftFrom, newKey, sameDraft, toPayload,
  type BuilderAgent, type BuilderDraft, type BuilderStep, type Check,
} from './workflowDraft'
import './builder.css'

function errMsg(e: unknown): string {
  const r = e as { response?: { data?: { detail?: unknown } }; message?: string }
  const detail = r?.response?.data?.detail
  const text = Array.isArray(detail) ? detail.map((d) => d?.msg).filter(Boolean).join('; ') : detail
  return (typeof text === 'string' && text) || r?.message || 'Something went wrong'
}

const agentColor = (a?: BuilderAgent) => a?.color || 'var(--ac)'

type Drag = { kind: 'agent'; agentId: string } | { kind: 'blank' } | { kind: 'move'; key: string }
const DRAG_TYPE = 'application/x-vigil-step'

interface ApiAgentRow { id: string; name?: string; description?: string; color?: string | null; recommended_tools?: string[]; enabled?: boolean }

function useBuilderAgents() {
  const [agents, setAgents] = useState<BuilderAgent[] | null>(null)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    let cancelled = false
    Promise.resolve().then(() => agentsApi.listAgents()).then((r) => {
      if (cancelled) return
      const rows = ((r?.data?.agents ?? []) as ApiAgentRow[]).map((a) => ({
        id: a.id,
        name: a.name || a.id,
        description: a.description || '',
        color: a.color ?? null,
        recommended_tools: a.recommended_tools ?? [],
        enabled: a.enabled !== false,
      }))
      setAgents(rows)
    }).catch(() => { if (!cancelled) setFailed(true) })
    return () => { cancelled = true }
  }, [])
  return { agents, failed }
}

/** One instruction per row; Enter adds a row under this one, Backspace on an empty row removes it. */
function LinesEditor({ lines, onChange, placeholder, label }: { lines: string[]; onChange: (l: string[]) => void; placeholder: string; label: string }) {
  const refs = useRef<(HTMLInputElement | null)[]>([])
  const [focus, setFocus] = useState<number | null>(null)
  useEffect(() => {
    if (focus !== null) { refs.current[focus]?.focus(); setFocus(null) }
  }, [focus, lines.length])
  const shown = lines.length ? lines : ['']
  const set = (i: number, v: string) => onChange(shown.map((l, j) => (j === i ? v : l)))
  return (
    <ol className="wb-lines" aria-label={label}>
      {shown.map((line, i) => (
        <li key={i}>
          <span className="wb-lines-n">{i + 1}</span>
          <input
            ref={(el) => { refs.current[i] = el }}
            value={line}
            placeholder={i === 0 ? placeholder : 'Next instruction'}
            aria-label={`${label} ${i + 1}`}
            onChange={(e) => set(i, e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault()
                onChange([...shown.slice(0, i + 1), '', ...shown.slice(i + 1)])
                setFocus(i + 1)
              } else if (e.key === 'Backspace' && line === '' && shown.length > 1) {
                e.preventDefault()
                onChange(shown.filter((_, j) => j !== i))
                setFocus(Math.max(0, i - 1))
              }
            }}
          />
          {shown.length > 1 && (
            <button type="button" className="wb-icon" aria-label={`Remove ${label.toLowerCase()} ${i + 1}`} onClick={() => onChange(shown.filter((_, j) => j !== i))}>
              <Icon name="close" size={11} />
            </button>
          )}
        </li>
      ))}
      <li className="wb-lines-add">
        <button type="button" className="tp-link" onClick={() => { onChange([...shown, '']); setFocus(shown.length) }}><Icon name="plus" size={11} /> Add instruction</button>
      </li>
    </ol>
  )
}

function Field({ label, htmlFor, hint, children }: { label: string; htmlFor?: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <div className="wb-field">
      <label className="wb-label" htmlFor={htmlFor}>{label}</label>
      {children}
      {hint && <span className="wb-hint">{hint}</span>}
    </div>
  )
}

function StepInspector({ step, index, agents, onChange, onRemove, onDuplicate }: {
  step: BuilderStep
  index: number
  agents: BuilderAgent[] | null
  onChange: (s: BuilderStep) => void
  onRemove: () => void
  onDuplicate: () => void
}) {
  const catalog = useToolCatalog()
  const agent = agents?.find((a) => a.id === step.agent_id)
  const set = <K extends keyof BuilderStep>(k: K, v: BuilderStep[K]) => onChange({ ...step, [k]: v })
  const pickAgent = (id: string) => {
    const next = agents?.find((a) => a.id === id)
    // A step still holding the old agent's usual tools takes the new agent's; a step whose tools were chosen keeps them.
    const usual = (a?: BuilderAgent) => (a?.recommended_tools ?? []).filter((t) => t !== 'read_skill')
    const untouched = step.tools.length === 0 || JSON.stringify(step.tools) === JSON.stringify(usual(agent))
    const oldDefaultName = agent ? agent.name.replace(/ agent$/i, '') : ''
    onChange({
      ...step,
      agent_id: id,
      tools: untouched ? usual(next) : step.tools,
      name: !step.name.trim() || step.name === oldDefaultName ? (next ? next.name.replace(/ agent$/i, '') : '') : step.name,
    })
  }
  return (
    <div className="wb-insp" aria-label={`Step ${index + 1} settings`}>
      <div className="wb-insp-head">
        <span className="wb-insp-n" style={{ background: agentColor(agent) }}>{index + 1}</span>
        <span className="wb-insp-title">{step.name.trim() || `Step ${index + 1}`}</span>
        <button type="button" className="wb-btn ghost small" onClick={onDuplicate}><Icon name="copy" size={12} /> Duplicate</button>
        <button type="button" className="wb-btn ghost small danger" onClick={onRemove}><Icon name="trash" size={12} /> Remove</button>
      </div>

      <Field label="Step name" htmlFor="wb-s-name">
        <input id="wb-s-name" className="wb-input" value={step.name} placeholder="e.g. Enrich the indicators" onChange={(e) => set('name', e.target.value)} />
      </Field>

      <Field label="Agent that runs it" htmlFor="wb-s-agent" hint={agent ? agent.description : 'Its prompt and model are the agent’s; this step adds the instructions below.'}>
        <select id="wb-s-agent" className="wb-input" value={step.agent_id} onChange={(e) => pickAgent(e.target.value)}>
          <option value="">Choose an agent…</option>
          {(agents ?? []).map((a) => <option key={a.id} value={a.id}>{a.name}{a.enabled ? '' : ' (off)'}</option>)}
          {step.agent_id && agents && !agent && <option value={step.agent_id}>{step.agent_id} (not found)</option>}
        </select>
      </Field>

      <Field label="Goal" htmlFor="wb-s-purpose">
        <textarea id="wb-s-purpose" className="wb-input" rows={2} value={step.purpose} placeholder="What this step is for, in one sentence" onChange={(e) => set('purpose', e.target.value)} />
      </Field>

      <Field label="Instructions" hint="In order. Enter adds the next one.">
        <LinesEditor label="Instruction" lines={step.steps} placeholder="e.g. Look up every hash and domain" onChange={(l) => set('steps', l)} />
      </Field>

      <ToolPicker
        label="Tools this step may use"
        hint="Only these. Anything not listed is refused while the step runs."
        value={step.tools}
        onChange={(t) => set('tools', t)}
        catalog={catalog}
        suggested={(agent?.recommended_tools ?? []).filter((t) => t !== 'read_skill')}
        suggestedLabel={agent ? `${agent.name}’s usual tools` : 'Suggested'}
      />

      <Field label="What it hands on" htmlFor="wb-s-out">
        <textarea id="wb-s-out" className="wb-input" rows={2} value={step.expected_output} placeholder="e.g. A verdict with the evidence for it" onChange={(e) => set('expected_output', e.target.value)} />
      </Field>

      <div className="wb-gate">
        <label className="wb-check">
          <input type="checkbox" checked={step.approval_required} onChange={(e) => set('approval_required', e.target.checked)} />
          <span>
            <b>A person approves before this step runs</b>
            <span className="wb-hint">The run waits in the approvals queue. A rejection stops the workflow there.</span>
          </span>
        </label>
      </div>

    </div>
  )
}

function ChecksList({ checks, onPick }: { checks: Check[]; onPick: (key?: string) => void }) {
  if (checks.length === 0) return <p className="wb-ok"><Icon name="check" size={13} /> Ready to save. Every step has an agent and every tool is connected.</p>
  return (
    <ul className="wb-checks">
      {checks.map((c, i) => (
        <li key={i} className={c.level}>
          <button type="button" onClick={() => onPick(c.step)}>
            <Icon name={c.level === 'error' ? 'alert' : 'info'} size={13} />
            <span>{c.text}</span>
          </button>
        </li>
      ))}
    </ul>
  )
}

function WorkflowSettings({ draft, set, checks, onPick }: {
  draft: BuilderDraft
  set: <K extends keyof BuilderDraft>(k: K, v: BuilderDraft[K]) => void
  checks: Check[]
  onPick: (key?: string) => void
}) {
  return (
    <div className="wb-insp" aria-label="Workflow settings">
      <div className="wb-insp-head"><span className="wb-insp-title">Checks</span></div>
      <ChecksList checks={checks} onPick={onPick} />
      <div className="wb-insp-head"><span className="wb-insp-title">When to use it</span></div>
      <Field label="Use case" htmlFor="wb-w-use" hint="Shown in the catalog, and read by Ask Vigil when it picks a workflow.">
        <textarea id="wb-w-use" className="wb-input" rows={2} value={draft.use_case} placeholder="e.g. A user reports a phishing email" onChange={(e) => set('use_case', e.target.value)} />
      </Field>
      <Field label="Example requests" hint="How people ask for it in Ask Vigil. One per row.">
        <LinesEditor label="Example request" lines={draft.trigger_examples} placeholder="e.g. Run phishing response on this alert" onChange={(l) => set('trigger_examples', l)} />
      </Field>
    </div>
  )
}

/** The definition as JSON, editable. Applying a valid edit replaces the flow. */
function CodeView({ draft, onApply, onPending }: { draft: BuilderDraft; onApply: (d: BuilderDraft) => void; onPending: (pending: boolean) => void }) {
  const text = useMemo(() => JSON.stringify(toPayload(draft), null, 2), [draft])
  const [edit, setEdit] = useState(text)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => { setEdit(text); setErr(null) }, [text])
  // Edits not applied yet are unsaved work too: Save waits for them, leaving asks first.
  useEffect(() => { onPending(edit !== text) }, [edit, text, onPending])
  useEffect(() => () => onPending(false), [onPending])
  const apply = () => {
    try {
      const parsed = JSON.parse(edit)
      if (!parsed || typeof parsed !== 'object' || !Array.isArray(parsed.phases)) throw new Error('It needs a "phases" list.')
      onApply(draftFrom(parsed))
      setErr(null)
    } catch (e) {
      setErr((e as Error).message)
    }
  }
  return (
    <div className="wb-code">
      <textarea className="wb-input mono" value={edit} spellCheck={false} aria-label="Workflow definition" onChange={(e) => setEdit(e.target.value)} />
      <div className="wb-code-foot">
        {err ? <span className="wb-err">Not applied: {err}</span> : <span className="wb-hint">This is exactly what Save sends. Paste a definition to import it.</span>}
        <button type="button" className="wb-btn ghost small" onClick={() => { navigator.clipboard?.writeText(edit).catch(() => {}) }}><Icon name="copy" size={12} /> Copy</button>
        <button type="button" className="wb-btn small" disabled={edit === text} onClick={apply}>Apply</button>
      </div>
    </div>
  )
}

export default function WorkflowBuilder({ initial, workflowId = null, origin, initialView = 'flow', backLabel = 'Workflows', onCancel, onSaved }: {
  /** a template, an AI draft or nothing; a saved workflow is loaded by id instead */
  initial?: BuilderDraft
  workflowId?: string | null
  /** where the draft came from, said under the name */
  origin?: string
  /** 'code' opens on the Definition view, to paste one in */
  initialView?: 'flow' | 'code'
  /** where the back button goes, said on it */
  backLabel?: string
  onCancel: () => void
  onSaved: (id: string) => void
}) {
  const { agents, failed: agentsFailed } = useBuilderAgents()
  const catalog = useToolCatalog()
  const [draft, setDraft] = useState<BuilderDraft>(initial ?? EMPTY_DRAFT)
  const [saved, setSaved] = useState<BuilderDraft>(initial && !workflowId ? EMPTY_DRAFT : initial ?? EMPTY_DRAFT)
  const [loading, setLoading] = useState(!!workflowId && !initial)
  const [loadErr, setLoadErr] = useState<string | null>(null)
  const [sel, setSel] = useState<string | null>(null)
  const [view, setView] = useState<'flow' | 'code'>(initialView)
  const sideRef = useRef<HTMLElement>(null)
  // a newly picked step's settings start at the top, not where the last one was scrolled
  useEffect(() => { if (sideRef.current) sideRef.current.scrollTop = 0 }, [sel])
  const [busy, setBusy] = useState(false)
  const [saveErr, setSaveErr] = useState<string | null>(null)
  const [confirmLeave, setConfirmLeave] = useState(false)
  const [drag, setDrag] = useState<Drag | null>(null)
  const [over, setOver] = useState<number | null>(null)
  const [paletteQ, setPaletteQ] = useState('')

  useEffect(() => {
    if (!workflowId || initial) return
    let cancelled = false
    // GET /workflows/{id} carries a custom workflow's stored phases as saved
    workflowApi.get(workflowId).then((r) => {
      if (cancelled) return
      const d = draftFrom(r.data)
      setDraft(d); setSaved(d); setLoading(false)
    }).catch((e) => { if (!cancelled) { setLoadErr(errMsg(e)); setLoading(false) } })
    return () => { cancelled = true }
  }, [workflowId, initial])

  const [defPending, setDefPending] = useState(false)
  const dirty = !sameDraft(draft, saved) || defPending
  const checks = useMemo(
    () => checkDraft(draft, agents, catalog.phase === 'ready' ? catalog.byName : null),
    [draft, agents, catalog],
  )
  const errors = checks.filter((c) => c.level === 'error')
  const selIndex = draft.phases.findIndex((p) => p.key === sel)
  const selStep = selIndex >= 0 ? draft.phases[selIndex] : null
  const agentOf = useCallback((id: string) => agents?.find((a) => a.id === id), [agents])

  const set = <K extends keyof BuilderDraft>(k: K, v: BuilderDraft[K]) => setDraft((d) => ({ ...d, [k]: v }))
  const setPhases = (f: (p: BuilderStep[]) => BuilderStep[]) => setDraft((d) => ({ ...d, phases: f(d.phases) }))
  const insertAt = (i: number, step: BuilderStep) => { setPhases((p) => [...p.slice(0, i), step, ...p.slice(i)]); setSel(step.key) }
  const addAgent = (a: BuilderAgent | undefined, at?: number) => insertAt(at ?? (selIndex >= 0 ? selIndex + 1 : draft.phases.length), blankStep(a))
  const move = (key: string, to: number) => setPhases((p) => {
    const from = p.findIndex((x) => x.key === key)
    if (from < 0) return p
    const next = p.filter((x) => x.key !== key)
    next.splice(to > from ? to - 1 : to, 0, p[from])
    return next
  })
  const remove = (key: string) => {
    const i = draft.phases.findIndex((p) => p.key === key)
    setPhases((p) => p.filter((x) => x.key !== key))
    setSel(draft.phases[i + 1]?.key ?? draft.phases[i - 1]?.key ?? null)
  }
  const duplicate = (key: string) => {
    const i = draft.phases.findIndex((p) => p.key === key)
    if (i < 0) return
    const src = draft.phases[i]
    const { phase_id: _dropped, ...rest } = src
    void _dropped
    insertAt(i + 1, { ...rest, key: newKey(), name: `${src.name} (copy)`, steps: [...src.steps], tools: [...src.tools] })
  }

  const save = useCallback(() => {
    if (defPending) { setSaveErr('Apply or undo your edits to the definition first.'); return }
    if (busy || errors.length) return
    setBusy(true)
    setSaveErr(null)
    const body = toPayload(draft)
    const req = workflowId ? workflowApi.updateCustom(workflowId, body) : workflowApi.createCustom(body)
    req.then((r) => {
      const id = (r?.data as { id?: string; workflow_id?: string } | undefined)
      setSaved(draft)
      onSaved(workflowId ?? id?.id ?? id?.workflow_id ?? '')
    }).catch((e) => { setSaveErr(errMsg(e)); setBusy(false) })
  }, [busy, errors.length, defPending, draft, workflowId, onSaved])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') { e.preventDefault(); save() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [save])

  const leave = () => (dirty ? setConfirmLeave(true) : onCancel())

  // Closing or reloading the tab with unsaved work asks the browser to confirm.
  useEffect(() => {
    if (!dirty) return
    const onUnload = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = '' }
    window.addEventListener('beforeunload', onUnload)
    return () => window.removeEventListener('beforeunload', onUnload)
  }, [dirty])

  // Drag and drop. Every drop lands in a slot between steps; clicks do the same without a mouse.
  const startDrag = (e: DragEvent, d: Drag) => {
    e.dataTransfer.setData(DRAG_TYPE, JSON.stringify(d))
    e.dataTransfer.setData('text/plain', d.kind)
    e.dataTransfer.effectAllowed = d.kind === 'move' ? 'move' : 'copy'
    setDrag(d)
  }
  const endDrag = () => { setDrag(null); setOver(null) }
  const dropAt = (i: number, e: DragEvent) => {
    e.preventDefault()
    let d = drag
    try { d = JSON.parse(e.dataTransfer.getData(DRAG_TYPE)) as Drag } catch { /* the state copy stands */ }
    endDrag()
    if (!d) return
    if (d.kind === 'move') move(d.key, i)
    else insertAt(i, blankStep(d.kind === 'agent' ? agentOf(d.agentId) : undefined))
  }
  const slot = (i: number) => (
    <div
      key={`slot-${i}`}
      className={`wb-slot${drag ? ' live' : ''}${over === i ? ' over' : ''}`}
      onDragOver={(e) => { e.preventDefault(); e.dataTransfer.dropEffect = drag?.kind === 'move' ? 'move' : 'copy'; if (over !== i) setOver(i) }}
      onDragLeave={() => setOver((o) => (o === i ? null : o))}
      onDrop={(e) => dropAt(i, e)}
    >
      <button type="button" className="wb-slot-add" aria-label={`Add a step at position ${i + 1}`} title="Add a step here" onClick={() => insertAt(i, blankStep())}>
        <Icon name="plus" size={12} />
      </button>
      {drag && <span className="wb-slot-text">Drop here</span>}
    </div>
  )

  const shownAgents = (agents ?? []).filter((a) => !paletteQ.trim() || `${a.name} ${a.description}`.toLowerCase().includes(paletteQ.trim().toLowerCase()))
  const stepIssues = (key: string) => checks.filter((c) => c.step === key)

  if (loading) return <div className="wb-state">Loading the workflow…</div>
  if (loadErr) return <div className="wb-state">Couldn’t load this workflow: {loadErr} <button type="button" className="wb-btn ghost small" onClick={onCancel}>Back</button></div>

  return (
    <div className="wb" aria-label="Workflow builder">
      <div className="wb-top">
        <button type="button" className="wb-btn ghost" onClick={leave}><Icon name="chevL" size={13} /> {backLabel}</button>
        <div className="wb-names">
          <input className="wb-name" value={draft.name} placeholder="Name this workflow" aria-label="Workflow name" onChange={(e) => set('name', e.target.value)} />
          <input className="wb-desc" value={draft.description} placeholder="What it does, in one line" aria-label="Workflow description" onChange={(e) => set('description', e.target.value)} />
          {origin && <span className="wb-origin">{origin}</span>}
        </div>
        <button type="button" className={`wb-status ${errors.length ? 'bad' : checks.length ? 'warn' : 'good'}`} onClick={() => setSel(null)} title="Show the checks">
          <Icon name={errors.length ? 'alert' : 'check'} size={13} />
          {errors.length ? `${errors.length} to fix` : checks.length ? `${checks.length} to review` : 'Ready'}
        </button>
        <button type="button" className="wb-btn ghost" onClick={leave}>Cancel</button>
        <button type="button" className="wb-btn primary" disabled={busy || errors.length > 0 || (!!workflowId && !dirty)} onClick={save} title={errors[0]?.text}>
          {busy ? 'Saving…' : workflowId ? (dirty ? 'Save changes' : 'Saved') : 'Save workflow'}
        </button>
      </div>
      {confirmLeave && (
        <div className="wb-banner" role="alert">
          <span>Leave without saving? Your changes to this workflow are lost.</span>
          <button type="button" className="wb-btn ghost small" onClick={() => setConfirmLeave(false)}>Keep editing</button>
          <button type="button" className="wb-btn small danger" onClick={onCancel}>Discard changes</button>
        </div>
      )}
      {saveErr && <div className="wb-banner bad" role="alert"><span>Not saved: {saveErr}</span></div>}

      <div className="wb-body">
        <aside className="wb-palette" aria-label="Steps you can add">
          <span className="wb-pal-title">Add a step</span>
          <span className="wb-hint">Drag onto the flow, or click to add after the selected step.</span>
          <div
            className="wb-pal-item blank"
            draggable
            role="button"
            tabIndex={0}
            onDragStart={(e) => startDrag(e, { kind: 'blank' })}
            onDragEnd={endDrag}
            onClick={() => addAgent(undefined)}
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); addAgent(undefined) } }}
          >
            <span className="wb-pal-dot"><Icon name="plus" size={11} /></span>
            <span className="wb-pal-text"><b>Blank step</b><span>Pick the agent and tools yourself</span></span>
          </div>
          <input className="wb-input small" value={paletteQ} placeholder="Find an agent" aria-label="Find an agent" onChange={(e) => setPaletteQ(e.target.value)} />
          {agentsFailed && <span className="wb-err">Couldn’t load agents.</span>}
          {agents === null && !agentsFailed && <span className="wb-hint">Loading agents…</span>}
          <div className="wb-pal-list">
            {shownAgents.map((a) => (
              <div
                key={a.id}
                className={`wb-pal-item${a.enabled ? '' : ' off'}`}
                draggable
                role="button"
                tabIndex={0}
                aria-label={`Add a ${a.name} step`}
                title={a.description}
                onDragStart={(e) => startDrag(e, { kind: 'agent', agentId: a.id })}
                onDragEnd={endDrag}
                onClick={() => addAgent(a)}
                onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); addAgent(a) } }}
              >
                <span className="wb-pal-dot" style={{ background: agentColor(a) }} />
                <span className="wb-pal-text"><b>{a.name}{a.enabled ? '' : ' · off'}</b><span>{a.description}</span></span>
              </div>
            ))}
          </div>
        </aside>

        <section className="wb-canvas-wrap" aria-label="Flow">
          <div className="wb-views" role="tablist" aria-label="Builder view">
            <button type="button" role="tab" aria-selected={view === 'flow'} disabled={defPending} title={defPending ? 'Apply or undo your edits to the definition first' : undefined} onClick={() => setView('flow')}><Icon name="flow" size={13} /> Flow</button>
            <button type="button" role="tab" aria-selected={view === 'code'} onClick={() => setView('code')}><Icon name="doc" size={13} /> Definition</button>
            <span className="wb-views-meta">
              {draft.phases.length} step{draft.phases.length === 1 ? '' : 's'}
              {' · '}{draft.phases.filter((p) => p.approval_required).length} approval{draft.phases.filter((p) => p.approval_required).length === 1 ? '' : 's'}
              {' · '}{new Set(draft.phases.flatMap((p) => p.tools)).size} tools
            </span>
          </div>
          {view === 'code' ? (
            <CodeView draft={draft} onPending={setDefPending} onApply={(d) => { setDraft((cur) => ({ ...d, phases: d.phases.map((p, i) => ({ ...p, phase_id: p.phase_id ?? cur.phases[i]?.phase_id })) })); setView('flow') }} />
          ) : (
            <div className="wb-canvas" onClick={(e) => { if (e.target === e.currentTarget) setSel(null) }}>
              <div className="wb-node start">
                <Icon name="play" size={12} />
                <span><b>Starts</b> when a person runs it on an alert, a case or a question, or asks for it in Ask Vigil.</span>
              </div>
              {slot(0)}
              {draft.phases.length === 0 && (
                <div className="wb-empty">
                  <b>No steps yet.</b>
                  <span>Drag an agent here from the left, or</span>
                  <button type="button" className="wb-btn small" onClick={() => insertAt(0, blankStep())}><Icon name="plus" size={12} /> Add the first step</button>
                </div>
              )}
              {draft.phases.map((p, i) => {
                const a = agentOf(p.agent_id)
                const issues = stepIssues(p.key)
                const bad = issues.some((c) => c.level === 'error')
                return (
                  <div key={p.key} className="wb-node-wrap">
                    {p.approval_required && (
                      <div className="wb-gate-node"><Icon name="lock" size={11} /> A person approves before this step</div>
                    )}
                    <div
                      className={`wb-node step${sel === p.key ? ' sel' : ''}${bad ? ' bad' : issues.length ? ' warn' : ''}${drag?.kind === 'move' && drag.key === p.key ? ' moving' : ''}`}
                      draggable
                      role="button"
                      tabIndex={0}
                      aria-pressed={sel === p.key}
                      aria-label={`Step ${i + 1}: ${p.name || 'unnamed'}`}
                      onDragStart={(e) => startDrag(e, { kind: 'move', key: p.key })}
                      onDragEnd={endDrag}
                      onClick={() => setSel(p.key)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setSel(p.key) }
                        if (e.altKey && e.key === 'ArrowUp' && i > 0) { e.preventDefault(); move(p.key, i - 1) }
                        if (e.altKey && e.key === 'ArrowDown' && i < draft.phases.length - 1) { e.preventDefault(); move(p.key, i + 2) }
                      }}
                    >
                      <span className="wb-grip" aria-hidden="true">⋮⋮</span>
                      <span className="wb-node-n" style={{ background: agentColor(a) }}>{i + 1}</span>
                      <span className="wb-node-main">
                        <span className="wb-node-name">{p.name.trim() || <i>Unnamed step</i>}</span>
                        <span className="wb-node-meta">
                          <span className="wb-node-agent">{a ? a.name : p.agent_id ? `${p.agent_id} (not found)` : 'No agent yet'}</span>
                          <span>{p.tools.length} tool{p.tools.length === 1 ? '' : 's'}</span>
                          {p.steps.filter((s) => s.trim()).length > 0 && <span>{p.steps.filter((s) => s.trim()).length} instruction{p.steps.filter((s) => s.trim()).length === 1 ? '' : 's'}</span>}
                        </span>
                        {p.purpose.trim() && <span className="wb-node-purpose">{p.purpose}</span>}
                      </span>
                      {issues.length > 0 && <span className={`wb-node-flag ${bad ? 'bad' : 'warn'}`} title={issues.map((c) => c.text).join('\n')}><Icon name="alert" size={12} /> {issues.length}</span>}
                      <span className="wb-node-acts" onClick={(e) => e.stopPropagation()}>
                        <button type="button" className="wb-icon" aria-label={`Move step ${i + 1} up`} disabled={i === 0} onClick={() => move(p.key, i - 1)}><Icon name="arrowUp" size={12} /></button>
                        <button type="button" className="wb-icon" aria-label={`Move step ${i + 1} down`} disabled={i === draft.phases.length - 1} onClick={() => move(p.key, i + 2)}><Icon name="arrowDn" size={12} /></button>
                        <button type="button" className="wb-icon" aria-label={`Remove step ${i + 1}`} onClick={() => remove(p.key)}><Icon name="trash" size={12} /></button>
                      </span>
                    </div>
                    {slot(i + 1)}
                  </div>
                )
              })}
              <div className="wb-node end">
                <Icon name="doc" size={12} />
                <span><b>Ends</b> with a report on the run, and on the case when it ran on one.</span>
              </div>
            </div>
          )}
        </section>

        <aside className="wb-side" ref={sideRef}>
          {selStep ? (
            <StepInspector
              key={selStep.key}
              step={selStep}
              index={selIndex}
              agents={agents}
              onChange={(s) => setPhases((p) => p.map((x) => (x.key === s.key ? s : x)))}
              onRemove={() => remove(selStep.key)}
              onDuplicate={() => duplicate(selStep.key)}
            />
          ) : (
            <WorkflowSettings draft={draft} set={set} checks={checks} onPick={(k) => setSel(k ?? null)} />
          )}
        </aside>
      </div>
    </div>
  )
}
