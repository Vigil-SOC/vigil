import { useEffect, useMemo, useState } from 'react'
import { Icon } from '../../shared/icons'
import { agentsApi, aiConfigApi, type AIModelInfo, type GeneratedAgentDraft } from '../../services/api'
import type { ToolChange } from '../../data/appData'
import { errMsg } from './runRead'

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
  /** a built-in's whole prompt; it has no role, principles or method parts */
  system_prompt?: string | null
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
  tools: string[]
  max_tokens: number
  enable_thinking: boolean
  model: string
  fallback_model: string
}

// Icon and colour are no longer fields: a new agent keeps these unless the AI draft supplies its own.
const BLANK_FORM: AgentForm = {
  name: '', specialization: '', description: '', icon: '', color: '#7d74f3', role: '',
  extra_principles: '', methodology: '', system_prompt_override: '', tools: [],
  max_tokens: 4096, enable_thinking: false, model: '', fallback_model: '',
}

const TOKEN_CHOICES = [4096, 8192, 16384, 32000]
const READ_SKILL = 'read_skill'

const fmtTokens = (n: number) => `${n.toLocaleString('en-US')} tokens`

/** Select options: the listed ones, then a saved value the list does not carry so Save never drops it. */
function withSaved<T extends string | number>(listed: T[], saved: T | ''): T[] {
  return saved !== '' && !listed.includes(saved as T) ? [...listed, saved as T] : listed
}

function ToolMark({ change }: { change: ToolChange | null | undefined }) {
  if (change === null) return null
  if (!change) return <span className="vg-side-mark none">Not connected</span>
  if (change === 'asks_first') return <span className="vg-side-mark asks_first">Asks you</span>
  return (
    <span className={`vg-side-mark ${change}`}>
      On its own{change === 'on_its_own' && <span className="poor"> changes things</span>}
    </span>
  )
}

/** The drawer over the Agents tab. `agentId` null builds a new agent; `describe` opens it with the describe box.
 *  `builtIn` shows a built-in's values: nothing exists until Save, which makes one copy. */
export function AgentDrawer({
  agentId,
  builtIn = false,
  describe = false,
  toolChanges = {},
  skillCount = null,
  onClose,
  onSaved,
}: {
  agentId: string | null
  builtIn?: boolean
  describe?: boolean
  /** marks from the agent's list row, shown until the connected-tools list arrives */
  toolChanges?: Record<string, ToolChange>
  /** size of the skill library; null while it is not known */
  skillCount?: number | null
  onClose: () => void
  /** gets the saved agent, so the screen can reopen on a new copy */
  onSaved: (saved: { id?: string }) => void
}) {
  const isCreate = agentId === null
  const [agent, setAgent] = useState<CustomAgentDetail | null>(null)
  const [phase, setPhase] = useState<'loading' | 'ready' | 'error'>(isCreate ? 'ready' : 'loading')
  const [loadErr, setLoadErr] = useState<string | null>(null)
  const [form, setForm] = useState<AgentForm>({ ...BLANK_FORM })
  const [advanced, setAdvanced] = useState(false)
  const [showPreview, setShowPreview] = useState(false)
  const [models, setModels] = useState<AIModelInfo[]>([])
  // null until /_meta/tools answers, or when it fails
  const [connected, setConnected] = useState<Record<string, ToolChange> | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [aiText, setAiText] = useState('')
  const [aiFeedback, setAiFeedback] = useState('')
  const [aiDraft, setAiDraft] = useState<GeneratedAgentDraft | null>(null)
  const [aiBusy, setAiBusy] = useState(false)
  const [aiErr, setAiErr] = useState<string | null>(null)

  const set = <K extends keyof AgentForm>(k: K, v: AgentForm[K]) => setForm((f) => ({ ...f, [k]: v }))

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  useEffect(() => {
    let cancelled = false
    agentsApi.getAvailableTools().then((r) => !cancelled && setConnected((r.data?.changes || {}) as Record<string, ToolChange>)).catch(() => {})
    aiConfigApi.listModels().then((r) => !cancelled && setModels(r.data?.models || [])).catch(() => {})
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    if (agentId === null) return
    let cancelled = false
    ;(builtIn ? agentsApi.getAgent(agentId) : agentsApi.getCustom(agentId))
      .then((res) => {
        if (cancelled) return
        const a = res.data as CustomAgentDetail
        // a built-in's prompt is its whole prompt, shown in the Advanced box
        if (builtIn) a.system_prompt_override = a.system_prompt || ''
        if (builtIn) a.recommended_tools = a.recommended_tools || []
        setAgent(a)
        setForm({
          name: a.name || '',
          specialization: a.specialization || '',
          description: a.description || '',
          icon: a.icon || '',
          color: a.color || '', // a stored null stays null
          role: a.role || '',
          extra_principles: a.extra_principles || '',
          methodology: a.methodology || '',
          system_prompt_override: a.system_prompt_override || '',
          tools: a.recommended_tools || [],
          max_tokens: a.max_tokens || BLANK_FORM.max_tokens,
          enable_thinking: !!a.enable_thinking,
          model: a.model || '',
          fallback_model: a.fallback_model || '',
        })
        setAdvanced(!!a.system_prompt_override)
        setPhase('ready')
      })
      .catch((e) => { if (!cancelled) { setLoadErr(errMsg(e)); setPhase('error') } })
    return () => { cancelled = true }
  }, [agentId, builtIn])

  // A tool is connected when the list knows it. Before the list arrives, or if it fails, the row's marks
  // stand in, and a tool with none is unknown (null) rather than not connected.
  const markOf = (tool: string): ToolChange | null | undefined => (connected ? connected[tool] : toolChanges[tool] ?? null)
  const addable = useMemo(
    () => Object.keys(connected ?? {}).filter((t) => !form.tools.includes(t)).sort(),
    [connected, form.tools],
  )

  const modelOptions = withSaved(models.map((m) => m.model_id), form.model)
  const fallbackOptions = withSaved(models.map((m) => m.model_id), form.fallback_model)
  const modelLabel = (id: string) => {
    const m = models.find((x) => x.model_id === id)
    return m ? `${m.display_name} (${id})` : id
  }

  // merge an AI draft into the form, keeping a name already typed; the draft's icon and colour only matter for a new agent
  const mergeDraft = (d: GeneratedAgentDraft) =>
    setForm((f) => ({
      ...f,
      name: f.name.trim() ? f.name : d.name,
      specialization: d.specialization || f.specialization,
      description: d.description || f.description,
      icon: isCreate ? d.icon || f.icon : f.icon,
      color: isCreate ? d.color || f.color : f.color,
      role: d.role || f.role,
      extra_principles: d.extra_principles || f.extra_principles,
      methodology: d.methodology || f.methodology,
      tools: d.recommended_tools?.length ? d.recommended_tools : f.tools,
      max_tokens: d.max_tokens || f.max_tokens,
      enable_thinking: typeof d.enable_thinking === 'boolean' ? d.enable_thinking : f.enable_thinking,
    }))

  const draftOf = (): GeneratedAgentDraft => ({
    name: form.name, description: form.description, specialization: form.specialization, icon: form.icon,
    color: form.color, role: form.role, extra_principles: form.extra_principles, methodology: form.methodology,
    recommended_tools: form.tools, max_tokens: form.max_tokens, enable_thinking: form.enable_thinking,
  })

  // Creating: the box is the description and a refinement is feedback on the draft. Editing: the box is the
  // change asked for, against the form as it stands.
  const generate = (feedback?: string) => {
    const text = (feedback ?? aiText).trim()
    if (!text) return
    setAiBusy(true)
    setAiErr(null)
    const req = isCreate
      ? agentsApi.generateCustom({ description: aiText.trim(), current_draft: aiDraft, feedback: feedback?.trim() || undefined })
      : agentsApi.generateCustom({ description: form.description || form.specialization || form.name, current_draft: draftOf(), feedback: text })
    req
      .then((res) => {
        const d = res.data?.draft
        if (d) { setAiDraft(d); mergeDraft(d); setAiFeedback(''); if (!isCreate) setAiText('') }
      })
      .catch((e) => setAiErr(errMsg(e)))
      .finally(() => setAiBusy(false))
  }

  const canSave = !busy && phase === 'ready' && !!form.name.trim() && (builtIn ? !!form.system_prompt_override.trim() : !!form.role.trim())

  const save = () => {
    if (!canSave) return
    setBusy(true)
    setError(null)
    const shown = {
      name: form.name.trim(),
      specialization: form.specialization.trim(),
      description: form.description.trim(),
      recommended_tools: form.tools,
      max_tokens: form.max_tokens,
      enable_thinking: form.enable_thinking,
      model: form.model || null,
      fallback_model: form.fallback_model || null,
    }
    const payload = {
      ...shown,
      icon: form.icon.trim() || null,
      color: form.color || null,
      role: form.role.trim(),
      extra_principles: form.extra_principles.trim(),
      methodology: form.methodology.trim(),
      // Advanced override replaces the base template; clear it when closed.
      system_prompt_override: advanced ? form.system_prompt_override.trim() || null : null,
    }
    // A copy of a built-in sends only what the drawer shows; the server fills in the rest from the built-in.
    const req = builtIn
      ? agentsApi.forkAgent(agentId!, { ...shown, system_prompt_override: form.system_prompt_override.trim() })
      : isCreate ? agentsApi.createCustom(payload) : agentsApi.updateCustom(agentId!, payload)
    req.then((res) => onSaved((res?.data ?? {}) as { id?: string })).catch((e) => { setError(errMsg(e)); setBusy(false) })
  }

  const title = isCreate ? 'New agent' : `Edit agent · ${agent?.name || agentId}`
  const holdsSkills = form.tools.includes(READ_SKILL)

  return (
    <div className="vg-side-scrim" onMouseDown={onClose}>
      <aside
        className="vg-side-panel"
        role="dialog"
        aria-label={isCreate ? 'New agent' : 'Edit agent'}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="flex items-start gap-3">
          <span className="flex flex-col gap-[3px] grow min-w-0">
            <span className="text-[20px] font-bold leading-[1.25] tracking-[-0.2px] text-tx break-words">{title}</span>
            {builtIn
              ? <span className="vg-side-hint">Built in. Saving creates your own editable copy; the original stays available.</span>
              : agent?.forked_from && <span className="vg-side-hint">Forked from <span className="mono">{agent.forked_from}</span></span>}
          </span>
          <button type="button" className="vg-side-close" aria-label="Close" onClick={onClose}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M6.5 6.5l11 11M17.5 6.5l-11 11" />
            </svg>
          </button>
        </div>
        {phase === 'loading' && <p className="text-[13px] text-tx-3">Loading agent…</p>}
        {phase === 'error' && <div className="vg-side-err">Couldn’t load agent: {loadErr}</div>}
        {phase === 'ready' && (
          <>
            {isCreate && describe ? (
              <div className="vg-side-field">
                <label htmlFor="vg-a-desc" className="vg-side-label">Describe the agent</label>
                <textarea
                  id="vg-a-desc"
                  className="vg-side-input"
                  rows={2}
                  autoFocus
                  value={aiText}
                  placeholder="e.g. Triages cloud IAM misconfigurations and privilege-escalation paths in AWS/GCP."
                  onChange={(e) => setAiText(e.target.value)}
                />
                <div className="flex items-center justify-end gap-2.5">
                  {aiErr && <span className="vg-side-err mr-auto">{aiErr}</span>}
                  <button type="button" className="vg-side-btn small line" disabled={aiBusy || !aiText.trim()} onClick={() => generate()}>
                    <Icon name="sparkle" size={13} />&nbsp;{aiBusy ? 'Generating…' : aiDraft ? 'Regenerate draft' : 'Generate draft'}
                  </button>
                </div>
                {aiDraft && (
                  <div className="vg-side-assist">
                    <Icon name="sparkle" size={16} />
                    <label htmlFor="vg-a-refine" className="vg-side-assist-text">Draft applied below. Refine it: “add memory-forensics tools”</label>
                    <input
                      id="vg-a-refine"
                      value={aiFeedback}
                      autoComplete="off"
                      placeholder="What should change?"
                      onChange={(e) => setAiFeedback(e.target.value)}
                      onKeyDown={(e) => { if (e.key === 'Enter') generate(aiFeedback) }}
                    />
                    <button type="button" className="vg-side-btn small line" disabled={aiBusy || !aiFeedback.trim()} onClick={() => generate(aiFeedback)}>Refine</button>
                  </div>
                )}
              </div>
            ) : !isCreate && !builtIn && (
              <div>
                <div className="vg-side-assist">
                  <Icon name="sparkle" size={16} />
                  <label htmlFor="vg-a-change" className="vg-side-assist-text">
                    Describe a change and Vigil drafts it: “be more careful with finance hosts”
                  </label>
                  <input
                    id="vg-a-change"
                    value={aiText}
                    autoComplete="off"
                    placeholder="What should change?"
                    onChange={(e) => setAiText(e.target.value)}
                    onKeyDown={(e) => { if (e.key === 'Enter') generate() }}
                  />
                  <button type="button" className="vg-side-btn small line" disabled={aiBusy || !aiText.trim()} onClick={() => generate()}>
                    {aiBusy ? 'Drafting…' : 'Draft change'}
                  </button>
                </div>
                {aiErr && <div className="vg-side-err mt-1.5">{aiErr}</div>}
              </div>
            )}

            <div className="vg-side-grid">
              <div className="vg-side-field">
                <label htmlFor="vg-a-name" className="vg-side-label">Name</label>
                <input id="vg-a-name" className="vg-side-input" value={form.name} autoComplete="off" onChange={(e) => set('name', e.target.value)} />
              </div>
              <div className="vg-side-field">
                <label htmlFor="vg-a-spec" className="vg-side-label">Specialization</label>
                <input id="vg-a-spec" className="vg-side-input" value={form.specialization} autoComplete="off" onChange={(e) => set('specialization', e.target.value)} />
              </div>
            </div>

            <div className="flex flex-col gap-3">
              <span className="vg-side-title">Instructions</span>
              {builtIn ? (
                <div className="vg-side-field">
                  <label htmlFor="vg-a-override" className="vg-side-label">Prompt</label>
                  <textarea id="vg-a-override" className="vg-side-input mono" rows={10} value={form.system_prompt_override} onChange={(e) => set('system_prompt_override', e.target.value)} />
                </div>
              ) : (
                <>
                  <div className="vg-side-field">
                    <label htmlFor="vg-a-role" className="vg-side-label">Role</label>
                    <input id="vg-a-role" className="vg-side-input" value={form.role} autoComplete="off" placeholder="e.g. investigator" onChange={(e) => set('role', e.target.value)} />
                  </div>
                  <div className="vg-side-field">
                    <label htmlFor="vg-a-principles" className="vg-side-label">Principles</label>
                    <textarea id="vg-a-principles" className="vg-side-input" rows={3} value={form.extra_principles} onChange={(e) => set('extra_principles', e.target.value)} />
                  </div>
                  <div className="vg-side-field">
                    <label htmlFor="vg-a-method" className="vg-side-label">Method</label>
                    <textarea id="vg-a-method" className="vg-side-input" rows={3} value={form.methodology} onChange={(e) => set('methodology', e.target.value)} />
                    <span className="vg-side-hint">
                      Role, principles and method.{' '}
                      {advanced ? 'Advanced: replacing the whole prompt.' : (
                        <>
                          <button type="button" className="vg-side-link" onClick={() => setAdvanced(true)}>Advanced</button>: replace the whole prompt.
                        </>
                      )}
                    </span>
                  </div>
                </>
              )}
              {!builtIn && advanced && (
                <div className="vg-side-field">
                  <label htmlFor="vg-a-override" className="vg-side-label">Whole prompt (replaces the three parts above)</label>
                  <textarea id="vg-a-override" className="vg-side-input mono" rows={10} value={form.system_prompt_override} onChange={(e) => set('system_prompt_override', e.target.value)} />
                  <button type="button" className="vg-side-link self-start text-[12px]" onClick={() => setAdvanced(false)}>Use the three parts instead</button>
                </div>
              )}
              {!builtIn && advanced && agent?.effective_prompt && (
                <div className="vg-side-field">
                  <button type="button" className="vg-side-fold" aria-expanded={showPreview} onClick={() => setShowPreview((v) => !v)}>
                    <span style={{ transform: showPreview ? 'rotate(90deg)' : 'none', transition: 'transform .12s', display: 'inline-flex' }}><Icon name="chevR" size={13} /></span>
                    Preview effective prompt
                  </button>
                  {showPreview && (
                    <>
                      <pre className="vg-side-pre">{agent.effective_prompt}</pre>
                      <span className="vg-side-hint">This is the exact system prompt Claude receives. Re-save to refresh.</span>
                    </>
                  )}
                </div>
              )}
            </div>

            <div className="vg-side-card">
              <span className="vg-side-title">Model</span>
              <div className="vg-side-grid">
                <div className="vg-side-field">
                  <label htmlFor="vg-a-model" className="vg-side-label">Model</label>
                  <select id="vg-a-model" className="vg-side-input" value={form.model} onChange={(e) => set('model', e.target.value)}>
                    <option value="">Use the default</option>
                    {modelOptions.map((id) => <option key={id} value={id}>{modelLabel(id)}</option>)}
                  </select>
                </div>
                <div className="vg-side-field">
                  <label htmlFor="vg-a-fb" className="vg-side-label">If it is unavailable, use</label>
                  <select id="vg-a-fb" className="vg-side-input" value={form.fallback_model} onChange={(e) => set('fallback_model', e.target.value)}>
                    <option value="">No fallback</option>
                    {fallbackOptions.map((id) => <option key={id} value={id}>{modelLabel(id)}</option>)}
                  </select>
                </div>
                <div className="vg-side-field">
                  <label htmlFor="vg-a-th" className="vg-side-label">Thinking</label>
                  <select id="vg-a-th" className="vg-side-input" value={form.enable_thinking ? 'on' : 'off'} onChange={(e) => set('enable_thinking', e.target.value === 'on')}>
                    <option value="off">Off</option>
                    <option value="on">On</option>
                  </select>
                </div>
                <div className="vg-side-field">
                  <label htmlFor="vg-a-mt" className="vg-side-label">Longest answer</label>
                  <select id="vg-a-mt" className="vg-side-input" value={form.max_tokens} onChange={(e) => set('max_tokens', Number(e.target.value))}>
                    {[...withSaved(TOKEN_CHOICES, form.max_tokens)].sort((a, b) => a - b).map((n) => <option key={n} value={n}>{fmtTokens(n)}</option>)}
                  </select>
                </div>
              </div>
              <span className="vg-side-hint">Uses Settings › AI models by default. A model chosen here wins over the default for this agent only.</span>
            </div>

            <div className="flex flex-col gap-2">
              <span className="vg-side-title">Skills</span>
              <span className="vg-side-hint" style={{ fontSize: 13 }}>
                {holdsSkills
                  ? `Offered every skill in the library${skillCount === null ? '' : ` (${skillCount})`}. Choosing single skills comes later.`
                  : 'Not offered skills.'}
              </span>
            </div>

            <div className="flex flex-col gap-2">
              <span className="vg-side-title">Tools it may use</span>
              {form.tools.length === 0 && <span className="vg-side-hint">No tools yet.</span>}
              {form.tools.map((tool) => (
                <div key={tool} className="vg-side-tool">
                  <span className="vg-side-tool-name">{tool}</span>
                  <ToolMark change={markOf(tool)} />
                  <button type="button" className="vg-side-x" aria-label={`Remove ${tool}`} onClick={() => set('tools', form.tools.filter((t) => t !== tool))}>
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                      <path d="M6.5 6.5l11 11M17.5 6.5l-11 11" />
                    </svg>
                  </button>
                </div>
              ))}
              <select
                className="vg-side-input"
                aria-label="Add tool"
                value=""
                disabled={addable.length === 0}
                onChange={(e) => e.target.value && set('tools', [...form.tools, e.target.value])}
              >
                <option value="">{connected === null ? 'Add tool…' : addable.length ? 'Add tool…' : 'No more connected tools'}</option>
                {addable.map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            </div>

            {error && <div className="vg-side-err">{error}</div>}
            <div className="vg-side-foot">
              <button type="button" className="vg-side-btn" onClick={onClose}>Cancel</button>
              <button type="button" className="vg-side-btn primary" disabled={!canSave} onClick={save}>
                {busy ? (isCreate ? 'Creating…' : 'Saving…') : isCreate ? 'Create agent' : builtIn ? 'Save my copy' : 'Save'}
              </button>
            </div>
          </>
        )}
      </aside>
    </div>
  )
}
