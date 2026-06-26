/* ============================================================
   Full-screen workflow builder for the redesign — a graph canvas
   (phases as draggable/connectable nodes, double-click an edge to
   set a branch condition), a metadata strip, and a phase-editor
   dialog. Mirrors the old src/pages/WorkflowBuilder.tsx, restyled
   to the redesign's dark theme (Tailwind + CSS vars, no MUI).
   Creates a custom workflow via workflowApi.createCustom; can edit
   an existing custom workflow when given workflowId + initial.
   ============================================================ */
import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  addEdge,
  applyNodeChanges,
  applyEdgeChanges,
  MarkerType,
  type Node,
  type Edge,
  type Connection,
  type NodeChange,
  type EdgeChange,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { Icon } from '../../shared/icons'
import { Popup, activateOnKey } from '../../shared/ui'
import { agentsApi, workflowApi, type WorkflowPhase } from '../../../services/api'

type AgentOption = { id: string; label: string }
const FALLBACK_AGENT_OPTIONS: AgentOption[] = [{ id: 'investigator', label: 'Investigation Agent' }]

const emptyPhase = (order: number): WorkflowPhase => ({
  phase_id: `phase-${order}`,
  order,
  agent_id: 'triage',
  name: `Phase ${order}`,
  purpose: '',
  tools: [],
  steps: [],
  expected_output: '',
  timeout_seconds: 300,
  approval_required: false,
})

interface EditorState {
  workflow_id: string | null
  name: string
  description: string
  use_case: string
  trigger_examples: string[]
  phases: WorkflowPhase[]
}

const emptyEditor = (): EditorState => ({
  workflow_id: null,
  name: '',
  description: '',
  use_case: '',
  trigger_examples: [],
  phases: [emptyPhase(1)],
})

function errMsg(e: unknown): string {
  const r = e as { response?: { data?: { detail?: string } }; message?: string }
  return r?.response?.data?.detail || r?.message || 'Something went wrong'
}

/* ---------------- shared dark-theme inputs ---------------- */
const inputCls = 'w-full bg-bg border border-line rounded-[7px] px-2.5 py-2 text-[13px] text-tx outline-none focus:border-accent-line'

function Labeled({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-[11px] uppercase tracking-[0.06em] text-tx-3">{label}</span>
      {children}
      {hint && <span className="text-[11px] text-tx-3">{hint}</span>}
    </label>
  )
}

/* ---------------- graph ---------------- */
function timeoutLabel(s?: number): string | null {
  if (!s || s <= 0) return null
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.round(s / 60)}m`
  return `${(s / 3600).toFixed(1)}h`
}

function buildGraph(phases: WorkflowPhase[], agents: AgentOption[]): { nodes: Node[]; edges: Edge[] } {
  const NODE_WIDTH = 270
  const GAP = 60
  const agentLabel = (id: string) => agents.find((a) => a.id === id)?.label || id

  const nodes: Node[] = phases.map((phase, i) => {
    const tools = phase.tools || []
    const steps = (phase.steps || []).filter((s) => s.trim())
    const tl = timeoutLabel(phase.timeout_seconds ?? 300)
    return {
      id: phase.phase_id || `phase-${i + 1}`,
      position: { x: i * (NODE_WIDTH + GAP), y: 0 },
      data: {
        label: (
          <div style={{ textAlign: 'left', color: 'var(--tx)' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8, marginBottom: 4 }}>
              <span style={{ fontSize: 10, fontWeight: 700, letterSpacing: 0.5, color: 'var(--tx-3)' }}>PHASE {i + 1}</span>
              {phase.approval_required && (
                <span style={{ fontSize: 9.5, fontWeight: 700, padding: '1px 6px', borderRadius: 5, background: 'var(--high-dim)', color: 'var(--high)', border: '1px solid var(--high)' }}>APPROVAL</span>
              )}
            </div>
            <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 2, lineHeight: 1.25 }}>{phase.name || '(unnamed)'}</div>
            <div style={{ fontSize: 11, color: 'var(--tx-3)', marginBottom: phase.purpose ? 4 : 0 }}>{agentLabel(phase.agent_id)}</div>
            {phase.purpose && (
              <div style={{ fontSize: 11, lineHeight: 1.35, color: 'var(--tx-2)', display: '-webkit-box', WebkitLineClamp: 3, WebkitBoxOrient: 'vertical', overflow: 'hidden', marginBottom: 6 }}>{phase.purpose}</div>
            )}
            {tools.length > 0 && (
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 3, marginBottom: 4 }}>
                {(tools[0] === '*' ? ['all tools'] : tools.slice(0, 4)).map((t) => (
                  <span key={t} style={{ fontSize: 9.5, padding: '1px 5px', borderRadius: 4, background: 'var(--bg-3)', border: '1px solid var(--line)', color: 'var(--tx-2)' }}>{t}</span>
                ))}
                {tools[0] !== '*' && tools.length > 4 && <span style={{ fontSize: 9.5, color: 'var(--tx-3)' }}>+{tools.length - 4}</span>}
              </div>
            )}
            <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', fontSize: 10, color: 'var(--tx-3)' }}>
              {steps.length > 0 && <span>{steps.length} step{steps.length === 1 ? '' : 's'}</span>}
              {tl && <span>⏱ {tl}</span>}
              {phase.expected_output && <span style={{ maxWidth: '100%', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>→ {phase.expected_output}</span>}
            </div>
          </div>
        ),
      },
      style: {
        width: NODE_WIDTH,
        padding: 11,
        borderRadius: 11,
        background: 'var(--panel)',
        border: phase.approval_required ? '2px solid var(--high)' : '1px solid var(--line)',
        textAlign: 'left' as const,
      },
    }
  })

  const edges: Edge[] = []
  for (let i = 0; i < phases.length - 1; i++) {
    const from = phases[i].phase_id || `phase-${i + 1}`
    const to = phases[i + 1].phase_id || `phase-${i + 2}`
    edges.push({ id: `${from}->${to}`, source: from, target: to, markerEnd: { type: MarkerType.ArrowClosed } })
  }
  return { nodes, edges }
}

/* ---------------- main overlay ---------------- */
export default function WorkflowBuilder({ initial, autoGenerate, onClose, onSaved }: {
  initial?: EditorState
  /** open the "Generate from a description" dialog immediately on mount */
  autoGenerate?: boolean
  onClose: () => void
  onSaved: () => void
}) {
  const [editor, setEditor] = useState<EditorState>(initial || emptyEditor())
  const [agentOptions, setAgentOptions] = useState<AgentOption[]>(FALLBACK_AGENT_OPTIONS)
  const [availableTools, setAvailableTools] = useState<string[]>([])
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const initialGraph = useMemo(() => buildGraph(editor.phases, agentOptions), []) // eslint-disable-line react-hooks/exhaustive-deps
  const [nodes, setNodes] = useState<Node[]>(initialGraph.nodes)
  const [edges, setEdges] = useState<Edge[]>(initialGraph.edges)
  const [edgeEdit, setEdgeEdit] = useState<{ edgeId: string; label: string } | null>(null)

  const [editPhaseIdx, setEditPhaseIdx] = useState<number | null>(null)
  const [phaseSnapshot, setPhaseSnapshot] = useState<WorkflowPhase | null>(null)
  const [phaseIsNew, setPhaseIsNew] = useState(false)

  const [genOpen, setGenOpen] = useState(!!autoGenerate)
  const [genPrompt, setGenPrompt] = useState('')
  const [generating, setGenerating] = useState(false)

  useEffect(() => {
    agentsApi.getAvailableTools().then((r) => setAvailableTools(r.data?.tools || [])).catch(() => {})
    agentsApi.listAgents().then((r) => {
      const agents = r.data?.agents || []
      if (agents.length) setAgentOptions(agents.map((a: { id: string; name: string }) => ({ id: a.id, label: a.name })))
    }).catch(() => {})
  }, [])

  // Keep nodes/edges in sync with phases; preserve dragged positions + custom edges.
  useEffect(() => {
    setNodes((prev) => {
      const byId = new Map(prev.map((n) => [n.id, n]))
      return buildGraph(editor.phases, agentOptions).nodes.map((n) => {
        const ex = byId.get(n.id)
        return ex ? { ...n, position: ex.position, selected: ex.selected } : n
      })
    })
    setEdges((prev) => {
      const valid = new Set(editor.phases.map((p, i) => p.phase_id || `phase-${i + 1}`))
      const keep = prev.filter((e) => valid.has(e.source) && valid.has(e.target))
      return keep.length === 0 ? buildGraph(editor.phases, agentOptions).edges : keep
    })
  }, [editor.phases, agentOptions])

  const onNodesChange = useCallback((c: NodeChange[]) => setNodes((nds) => applyNodeChanges(c, nds)), [])
  const onEdgesChange = useCallback((c: EdgeChange[]) => setEdges((eds) => applyEdgeChanges(c, eds)), [])
  const onConnect = useCallback((conn: Connection) => setEdges((eds) => addEdge({ ...conn, markerEnd: { type: MarkerType.ArrowClosed } }, eds)), [])
  const onEdgeDoubleClick = useCallback((_: unknown, edge: Edge) => setEdgeEdit({ edgeId: edge.id, label: String(edge.label || '') }), [])
  const commitEdge = () => {
    if (!edgeEdit) return
    setEdges((eds) => eds.map((e) => (e.id === edgeEdit.edgeId ? { ...e, label: edgeEdit.label || undefined, data: { ...(e.data || {}), condition: edgeEdit.label || undefined } } : e)))
    setEdgeEdit(null)
  }

  const updatePhase = (idx: number, patch: Partial<WorkflowPhase>) =>
    setEditor((e) => { const next = [...e.phases]; next[idx] = { ...next[idx], ...patch }; return { ...e, phases: next } })
  const movePhase = (idx: number, delta: number) =>
    setEditor((e) => {
      const t = idx + delta
      if (t < 0 || t >= e.phases.length) return e
      const next = [...e.phases]
      const [item] = next.splice(idx, 1)
      next.splice(t, 0, item)
      return { ...e, phases: next.map((p, i) => ({ ...p, order: i + 1 })) }
    })
  const removePhase = (idx: number) =>
    setEditor((e) => ({ ...e, phases: e.phases.filter((_, i) => i !== idx).map((p, i) => ({ ...p, order: i + 1, phase_id: p.phase_id || `phase-${i + 1}` })) }))

  const openPhase = (idx: number, isNew: boolean) => {
    setPhaseSnapshot(JSON.parse(JSON.stringify(editor.phases[idx] ?? null)))
    setPhaseIsNew(isNew)
    setEditPhaseIdx(idx)
  }
  const handleNodeClick = useCallback((_: unknown, node: Node) => {
    const idx = editor.phases.findIndex((p, i) => (p.phase_id || `phase-${i + 1}`) === node.id)
    if (idx >= 0) openPhase(idx, false)
  }, [editor.phases]) // eslint-disable-line react-hooks/exhaustive-deps
  const addAndEditPhase = () =>
    setEditor((e) => {
      const newPhase = emptyPhase(e.phases.length + 1)
      const next = [...e.phases, newPhase]
      setTimeout(() => { setPhaseSnapshot(JSON.parse(JSON.stringify(newPhase))); setPhaseIsNew(true); setEditPhaseIdx(next.length - 1) }, 0)
      return { ...e, phases: next }
    })
  const closePhaseEditor = (commit: boolean) => {
    if (!commit && editPhaseIdx !== null) {
      if (phaseIsNew) removePhase(editPhaseIdx)
      else if (phaseSnapshot) {
        const restore = phaseSnapshot, idx = editPhaseIdx
        setEditor((e) => { const next = [...e.phases]; next[idx] = restore; return { ...e, phases: next } })
      }
    }
    setEditPhaseIdx(null); setPhaseSnapshot(null); setPhaseIsNew(false)
  }

  const generate = () => {
    if (!genPrompt.trim()) return
    setGenerating(true)
    setError(null)
    workflowApi.generate(genPrompt.trim())
      .then((res) => {
        const d = res.data?.draft
        if (!d) return
        setEditor({
          workflow_id: null,
          name: d.name || '',
          description: d.description || '',
          use_case: d.use_case || '',
          trigger_examples: d.trigger_examples || [],
          phases: (d.phases || []).map((p: WorkflowPhase, i: number) => ({ ...emptyPhase(i + 1), ...p, order: i + 1 })),
        })
        setGenOpen(false); setGenPrompt('')
      })
      .catch((e) => setError(errMsg(e)))
      .finally(() => setGenerating(false))
  }

  const save = () => {
    if (!editor.name.trim() || !editor.description.trim()) { setError('Name and description are required.'); return }
    if (editor.phases.length === 0) { setError('Add at least one phase.'); return }
    setSaving(true)
    setError(null)
    const payload = {
      name: editor.name.trim(),
      description: editor.description.trim(),
      use_case: editor.use_case.trim(),
      trigger_examples: editor.trigger_examples.filter((t) => t.trim()),
      phases: editor.phases.map((p, i) => ({ ...p, order: i + 1, phase_id: p.phase_id || `phase-${i + 1}` })),
    }
    const req = editor.workflow_id
      ? workflowApi.updateCustom(editor.workflow_id, payload)
      : workflowApi.createCustom(payload)
    req.then(onSaved).catch((e) => { setError(errMsg(e)); setSaving(false) })
  }

  return (
    <div className="fixed inset-0 z-[80] bg-bg flex flex-col">
      {/* toolbar */}
      <div className="flex items-center gap-3 px-5 py-3 border-b border-line bg-panel shrink-0">
        <div className="min-w-0">
          <div className="text-[16px] font-semibold leading-tight">{editor.workflow_id ? 'Edit workflow' : 'New workflow'}</div>
          <div className="text-[11.5px] text-tx-3 truncate mono">{editor.workflow_id || 'Draft — not yet saved'}</div>
        </div>
        <div className="flex-1" />
        <button className="btn ghost" onClick={() => setGenOpen(true)}><Icon name="sparkle" /> Generate</button>
        <button className="btn ghost" onClick={onClose} disabled={saving}>Cancel</button>
        <button className="btn primary" onClick={save} disabled={saving}><Icon name="check2" /> {saving ? 'Saving…' : 'Save workflow'}</button>
      </div>

      {/* metadata strip */}
      <div className="px-5 py-3 border-b border-line bg-panel shrink-0">
        <div className="grid gap-3 items-end [grid-template-columns:repeat(auto-fit,minmax(200px,1fr))]">
          <Labeled label="Name *"><input className={inputCls} value={editor.name} onChange={(e) => setEditor((x) => ({ ...x, name: e.target.value }))} placeholder="Ransomware Response" /></Labeled>
          <Labeled label="Description *"><input className={inputCls} value={editor.description} onChange={(e) => setEditor((x) => ({ ...x, description: e.target.value }))} /></Labeled>
          <Labeled label="Use case"><input className={inputCls} value={editor.use_case} onChange={(e) => setEditor((x) => ({ ...x, use_case: e.target.value }))} /></Labeled>
          <Labeled label="Trigger examples (one per line)"><textarea className={`${inputCls} resize-y max-w-full`} rows={1} value={editor.trigger_examples.join('\n')} onChange={(e) => setEditor((x) => ({ ...x, trigger_examples: e.target.value.split('\n') }))} /></Labeled>
          <button className="btn primary h-[38px]" onClick={addAndEditPhase}><Icon name="plus" /> Add phase</button>
        </div>
        {error && <div className="text-[12.5px] mt-2" style={{ color: 'var(--crit)' }}>{error}</div>}
      </div>

      {/* canvas */}
      <div className="flex-1 min-h-0 relative">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          fitView
          nodesDraggable
          nodesConnectable
          elementsSelectable
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          onNodeClick={handleNodeClick}
          onEdgeDoubleClick={onEdgeDoubleClick}
          proOptions={{ hideAttribution: true }}
          colorMode="dark"
        >
          <Background />
          <MiniMap pannable zoomable />
          <Controls showInteractive={false} />
        </ReactFlow>
        {editor.phases.length === 0 && (
          <div className="absolute inset-0 grid place-items-center pointer-events-none">
            <span className="text-[13px] text-tx-3">Click <strong>Add phase</strong> to start building your workflow.</span>
          </div>
        )}
      </div>

      {/* phase editor */}
      {editPhaseIdx !== null && editor.phases[editPhaseIdx] && (
        <PhaseEditor
          idx={editPhaseIdx}
          total={editor.phases.length}
          phase={editor.phases[editPhaseIdx]}
          agents={agentOptions}
          tools={availableTools}
          onChange={(patch) => updatePhase(editPhaseIdx, patch)}
          onMove={(d) => { movePhase(editPhaseIdx, d); setEditPhaseIdx(editPhaseIdx + d) }}
          onDelete={() => { removePhase(editPhaseIdx); setEditPhaseIdx(null); setPhaseSnapshot(null); setPhaseIsNew(false) }}
          onCancel={() => closePhaseEditor(false)}
          onDone={() => closePhaseEditor(true)}
        />
      )}

      {/* edge condition */}
      {edgeEdit && (
        <Popup open onClose={() => setEdgeEdit(null)} title="Edge condition" width={460}>
          <div className="flex flex-col gap-3.5">
            <p className="text-[12px] text-tx-3 leading-[1.5]">Optional condition — the edge is taken only if the predicate evaluates true against the previous phase's output. Leave blank for an unconditional transition. Enables branches and loops.</p>
            <Labeled label="Condition"><input className={`${inputCls} font-mono`} autoFocus placeholder='e.g. output.severity == "high"' value={edgeEdit.label} onChange={(e) => setEdgeEdit((p) => (p ? { ...p, label: e.target.value } : p))} /></Labeled>
            <div className="flex justify-end gap-2.5 pt-1">
              <button className="btn ghost" onClick={() => setEdgeEdit(null)}>Cancel</button>
              <button className="btn primary" onClick={commitEdge}>Save</button>
            </div>
          </div>
        </Popup>
      )}

      {/* generate */}
      {genOpen && (
        <Popup open onClose={() => setGenOpen(false)} title="Generate workflow" width={560}>
          <div className="flex flex-col gap-3.5">
            <Labeled label="Scenario description" hint="Drafts the whole workflow — review and edit before saving.">
              <textarea className={`${inputCls} resize-y max-w-full`} rows={4} placeholder="e.g. Investigate and contain a ransomware incident on an endpoint" value={genPrompt} onChange={(e) => setGenPrompt(e.target.value)} />
            </Labeled>
            {error && <div className="text-[12.5px]" style={{ color: 'var(--crit)' }}>{error}</div>}
            <div className="flex justify-end gap-2.5 pt-1">
              <button className="btn ghost" onClick={() => setGenOpen(false)}>Cancel</button>
              <button className="btn primary" disabled={!genPrompt.trim() || generating} style={{ opacity: !genPrompt.trim() || generating ? 0.5 : 1 }} onClick={generate}><Icon name="sparkle" /> {generating ? 'Generating…' : 'Generate'}</button>
            </div>
          </div>
        </Popup>
      )}
    </div>
  )
}

/* ---------------- phase editor dialog ---------------- */
function PhaseEditor({ idx, total, phase, agents, tools, onChange, onMove, onDelete, onCancel, onDone }: {
  idx: number
  total: number
  phase: WorkflowPhase
  agents: AgentOption[]
  tools: string[]
  onChange: (patch: Partial<WorkflowPhase>) => void
  onMove: (delta: number) => void
  onDelete: () => void
  onCancel: () => void
  onDone: () => void
}) {
  const allAccess = (phase.tools || []).length === 1 && phase.tools[0] === '*'
  const title = (
    <span className="flex items-center gap-2">
      Phase {idx + 1}
      <button className="btn ghost icon" title="Move up" disabled={idx === 0} onClick={() => onMove(-1)}><Icon name="arrowUp" size={14} /></button>
      <button className="btn ghost icon" title="Move down" disabled={idx >= total - 1} onClick={() => onMove(1)}><Icon name="arrowDn" size={14} /></button>
      <button className="btn ghost icon danger" title="Delete phase" onClick={onDelete}><Icon name="trash" size={14} /></button>
    </span>
  )
  return (
    <Popup open onClose={onCancel} title={title} width={620}>
      <div className="flex flex-col gap-3.5">
        <div className="grid grid-cols-2 gap-3.5">
          <Labeled label="Phase name"><input className={inputCls} value={phase.name} onChange={(e) => onChange({ name: e.target.value })} /></Labeled>
          <Labeled label="Agent">
            <select className={inputCls} value={phase.agent_id} onChange={(e) => onChange({ agent_id: e.target.value })}>
              {agents.map((a) => <option key={a.id} value={a.id}>{a.label} ({a.id})</option>)}
            </select>
          </Labeled>
        </div>
        <Labeled label="Purpose"><input className={inputCls} value={phase.purpose || ''} onChange={(e) => onChange({ purpose: e.target.value })} /></Labeled>
        <Labeled
          label="Tools (comma-separated)"
          hint={allAccess ? 'This phase has unrestricted tool access. Uncheck "All tool access" to pick specific tools.' : tools.length ? `${tools.length} tools available — free text accepted if a tool isn't in the registry yet.` : 'Tool registry unavailable — free text accepted.'}
        >
          <input
            className={`${inputCls} font-mono ${allAccess ? 'opacity-50' : ''}`}
            disabled={allAccess}
            list="phase-tool-names"
            placeholder={allAccess ? 'All tools — individual picks disabled' : 'Start typing to filter…'}
            value={allAccess ? '' : (phase.tools || []).join(', ')}
            onChange={(e) => onChange({ tools: e.target.value.split(',').map((t) => t.trim()).filter(Boolean) })}
          />
        </Labeled>
        <datalist id="phase-tool-names">{tools.map((t) => <option key={t} value={t} />)}</datalist>
        <label className="flex items-center gap-2.5 text-[12.5px] text-tx-2 cursor-pointer -mt-1">
          <span
            className={`sk-toggle${allAccess ? ' on' : ''}`}
            role="switch"
            aria-checked={allAccess}
            aria-label="All tool access (grants the agent every registered tool)"
            tabIndex={0}
            onClick={() => onChange({ tools: allAccess ? [] : ['*'] })}
            onKeyDown={activateOnKey(() => onChange({ tools: allAccess ? [] : ['*'] }))}
          ><span className="kn" /></span>
          All tool access (grants the agent every registered tool)
        </label>
        <Labeled label="Steps (one per line)"><textarea className={`${inputCls} resize-y max-w-full`} rows={3} value={(phase.steps || []).join('\n')} onChange={(e) => onChange({ steps: e.target.value.split('\n') })} /></Labeled>
        <div className="grid grid-cols-2 gap-3.5">
          <Labeled label="Expected output"><input className={inputCls} value={phase.expected_output || ''} onChange={(e) => onChange({ expected_output: e.target.value })} /></Labeled>
          <Labeled label="Timeout (s)"><input className={inputCls} type="number" value={phase.timeout_seconds ?? 300} onChange={(e) => onChange({ timeout_seconds: parseInt(e.target.value, 10) || 0 })} /></Labeled>
        </div>
        <label className="flex items-center gap-2.5 text-[12.5px] text-tx-2 cursor-pointer">
          <span
            className={`sk-toggle${phase.approval_required ? ' on' : ''}`}
            role="switch"
            aria-checked={phase.approval_required}
            aria-label="Require approval before this phase runs"
            tabIndex={0}
            onClick={() => onChange({ approval_required: !phase.approval_required })}
            onKeyDown={activateOnKey(() => onChange({ approval_required: !phase.approval_required }))}
          ><span className="kn" /></span>
          Require approval before this phase runs
        </label>
        <div className="flex justify-end gap-2.5 pt-1">
          <button className="btn ghost" onClick={onCancel}>Cancel</button>
          <button className="btn primary" onClick={onDone}>Done</button>
        </div>
      </div>
    </Popup>
  )
}
