import { useCallback, useEffect, useMemo, useState } from 'react'
import { parseDocument } from 'yaml'
import { Icon } from '../../shared/icons'
import { ConfirmDialog, OverlayPortal } from '../../shared/ui'
import { TabStrip } from '../../shared/TabStrip'
import { agentsApi } from '../../services/api'
import { skillsApi, type ApiSkill } from '../../services/skillsApi'
import type { ToolChange } from '../../data/appData'
import { errMsg } from './runRead'

const PREFIX = 'custom-'
const BLANK = `id: ${PREFIX}\ndescription: ""\nsystem: ""\nneeds: []\nconclusion: prose\n`
const RECORD_MODES = ['off', 'available', 'required'] as const
// what Structured starts from; the schema itself is edited in YAML
const STRUCTURED = { schema: { type: 'object', properties: { summary: { type: 'string' } }, required: ['summary'] }, summary_field: 'summary' }
const GUARD = { field: '', guarded: [], fallback: '', reviewer_prompt: '' }

/** One row of GET /agents/custom/_meta/tools' `catalog`: "vigil" serves Vigil's own tools. */
interface CatalogTool {
  name: string
  server: string
  description: string
}
const CAPABILITY_SERVER = 'Capabilities'

type Doc = Record<string, unknown>
const asText = (v: unknown) => (typeof v === 'string' ? v : v == null ? '' : String(v))
const asList = (v: unknown) => (Array.isArray(v) ? v.map(asText) : [])
const asMap = (v: unknown): Doc | null => (v && typeof v === 'object' && !Array.isArray(v) ? (v as Doc) : null)

/** The manifest as fields, or why it cannot be read as any. */
function readText(text: string): { doc: Doc; error: null } | { doc: null; error: string } {
  const parsed = parseDocument(text)
  if (parsed.errors.length) return { doc: null, error: parsed.errors[0].message.split('\n')[0] }
  const doc = parsed.toJS()
  if (doc != null && !asMap(doc)) return { doc: null, error: 'the manifest is not a mapping' }
  return { doc: (doc ?? {}) as Doc, error: null }
}

/** The text with one top-level key set (or removed when `value` is undefined), comments and order kept. */
function withKey(text: string, key: string, value: unknown): string {
  const parsed = parseDocument(text)
  if (value === undefined) parsed.delete(key)
  else parsed.set(key, parsed.createNode(value))
  // no folding: a long line stays one line, and a multi-line string is a block
  return parsed.toString({ lineWidth: 0 })
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

/** Find a capability or tool by name, server or description, under what serves it.
 *  Groups start folded; a search unfolds every group with a match, and Enter adds the first. */
function ToolFinder({ catalog, markOf, onAdd }: {
  catalog: CatalogTool[] | null
  markOf: (tool: string) => ToolChange | null | undefined
  onAdd: (tool: string) => void
}) {
  const [query, setQuery] = useState('')
  const [unfolded, setUnfolded] = useState<Set<string>>(new Set())
  const needle = query.trim().toLowerCase()
  const groups = useMemo(() => {
    const words = needle.split(/\s+/).filter(Boolean)
    const out = new Map<string, CatalogTool[]>()
    for (const t of catalog ?? []) {
      const text = `${t.name} ${t.server} ${t.description}`.toLowerCase()
      if (words.every((w) => text.includes(w))) out.set(t.server, [...(out.get(t.server) ?? []), t])
    }
    return [...out]
  }, [catalog, needle])
  const first = groups[0]?.[1][0]
  const fold = (server: string) => setUnfolded((s) => {
    const next = new Set(s)
    if (!next.delete(server)) next.add(server)
    return next
  })

  return (
    <div className="vg-side-finder">
      <input
        className="vg-side-input"
        aria-label="Find a capability or tool"
        value={query}
        autoComplete="off"
        disabled={!catalog?.length}
        placeholder={catalog === null ? 'Add a need…' : catalog.length ? `Find a capability or tool among ${catalog.length}` : 'Nothing more to add'}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter' && needle && first) { onAdd(first.name); setQuery('') } }}
      />
      {!!catalog?.length && (
        <div className="vg-side-finder-list" role="list" aria-label="Needs to add">
          {groups.length === 0 && <span className="vg-side-hint">Nothing matches “{query.trim()}”.</span>}
          {groups.map(([server, tools]) => {
            const open = needle !== '' || unfolded.has(server)
            return (
              <div key={server} role="listitem">
                <button type="button" className="vg-side-finder-group" aria-expanded={open} onClick={() => fold(server)}>
                  <span style={{ transform: open ? 'rotate(90deg)' : 'none', display: 'inline-flex' }}><Icon name="chevR" size={12} /></span>
                  {server === 'vigil' ? 'Vigil' : server}
                  <span className="vg-side-finder-n">{tools.length}</span>
                </button>
                {open && tools.map((t) => (
                  <button key={t.name} type="button" className="vg-side-finder-tool" title={t.description || t.name} aria-label={`Add ${t.name}`} onClick={() => onAdd(t.name)}>
                    <span className="vg-side-finder-main">
                      <span className="vg-side-tool-name">{t.name}</span>
                      {t.description && <span className="vg-side-finder-desc">{t.description}</span>}
                    </span>
                    <ToolMark change={markOf(t.name)} />
                  </button>
                ))}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

function Chip({ name, mark, onRemove }: { name: string; mark?: React.ReactNode; onRemove: () => void }) {
  return (
    <div className="vg-side-tool">
      <span className="vg-side-tool-name">{name}</span>
      {mark}
      <button type="button" className="vg-side-x" aria-label={`Remove ${name}`} onClick={onRemove}>
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M6.5 6.5l11 11M17.5 6.5l-11 11" />
        </svg>
      </button>
    </div>
  )
}

/** A whole number, or the key left out when blank. */
function intOrNone(raw: string): number | undefined {
  return raw.trim() === '' ? undefined : Number(raw)
}

/** A built-in, read only: what it is, and a fork into a manifest of your own. */
function BuiltIn({ agentId, onForked }: { agentId: string; onForked: (id: string) => void }) {
  const [agent, setAgent] = useState<{ name?: string; description?: string; manifest?: string; recommended_tools?: string[] } | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    agentsApi.getAgent(agentId).then((r) => setAgent(r.data)).catch((e) => setErr(errMsg(e)))
  }, [agentId])
  const fork = () => {
    setBusy(true)
    setErr(null)
    agentsApi.forkAgent(agentId).then((r) => onForked(r.data.id)).catch((e) => { setErr(errMsg(e)); setBusy(false) })
  }
  if (!agent) return err ? <div className="vg-side-err">Couldn’t load agent: {err}</div> : <p className="text-[13px] text-tx-3">Loading agent…</p>
  return (
    <>
      {agent.description && <p className="text-[13px] leading-[1.5] text-tx-1">{agent.description}</p>}
      <div className="vg-side-field">
        <span className="vg-side-label">Manifest (YAML)</span>
        <pre className="vg-side-pre">{agent.manifest}</pre>
      </div>
      <div className="flex flex-col gap-2">
        <span className="vg-side-title">Tools it may use</span>
        {(agent.recommended_tools ?? []).length === 0 && <span className="vg-side-hint">No tools.</span>}
        {(agent.recommended_tools ?? []).map((t) => <div key={t} className="vg-side-tool"><span className="vg-side-tool-name">{t}</span></div>)}
      </div>
      {err && <div className="vg-side-err">{err}</div>}
      <div className="vg-side-foot">
        <button type="button" className="vg-side-btn primary" disabled={busy} onClick={fork}>{busy ? 'Forking…' : 'Fork into my own agent'}</button>
      </div>
    </>
  )
}

/** The drawer over the Agents tab. It edits one operator manifest: the YAML text is its only state, and the
 *  fields read from and write into that text. `agentId` null builds a new agent; `describe` opens it with the
 *  describe box; `seedFrom` starts a new one from a copy of that agent's manifest. `builtIn` shows a built-in. */
export function AgentDrawer({
  agentId,
  builtIn = false,
  describe = false,
  seedFrom = null,
  toolChanges = {},
  onClose,
  onSaved,
}: {
  agentId: string | null
  builtIn?: boolean
  describe?: boolean
  seedFrom?: string | null
  /** marks from the agent's list row, shown until the connected-tools list arrives */
  toolChanges?: Record<string, ToolChange>
  onClose: () => void
  /** gets the saved agent's id, so the screen can reopen on it */
  onSaved: (saved: { id?: string }) => void
}) {
  const isCreate = agentId === null
  const [text, setText] = useState(BLANK)
  const [opened, setOpened] = useState(BLANK)
  const [phase, setPhase] = useState<'loading' | 'ready' | 'error'>(isCreate && !seedFrom ? 'ready' : 'loading')
  const [loadErr, setLoadErr] = useState<string | null>(null)
  const [view, setView] = useState<'fields' | 'yaml'>('fields')
  const [confirmClose, setConfirmClose] = useState(false)
  // null until /_meta/tools answers, or when it fails
  const [connected, setConnected] = useState<Record<string, ToolChange> | null>(null)
  const [catalog, setCatalog] = useState<CatalogTool[] | null>(null)
  const [capabilities, setCapabilities] = useState<Set<string>>(new Set())
  const [skills, setSkills] = useState<ApiSkill[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [aiText, setAiText] = useState('')
  const [aiFeedback, setAiFeedback] = useState('')
  const [drafted, setDrafted] = useState(false)
  const [aiBusy, setAiBusy] = useState(false)
  const [aiErr, setAiErr] = useState<string | null>(null)

  const dirty = aiText.trim() !== '' || text !== opened
  const close = useCallback(() => (dirty ? setConfirmClose(true) : onClose()), [dirty, onClose])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [close])

  useEffect(() => {
    if (builtIn) return
    let cancelled = false
    agentsApi.getAvailableTools().then((r) => {
      if (cancelled) return
      const caps = (r.data?.capabilities || []) as { name: string; tools: string[] }[]
      setConnected((r.data?.changes || {}) as Record<string, ToolChange>)
      setCapabilities(new Set(caps.map((c) => c.name)))
      setCatalog([
        ...caps.map((c) => ({ name: c.name, server: CAPABILITY_SERVER, description: `Whichever of ${c.tools.join(', ')} is connected` })),
        ...((r.data?.catalog || []) as CatalogTool[]),
      ])
    }).catch(() => {})
    skillsApi.list().then((rows) => !cancelled && setSkills(rows)).catch(() => {})
    return () => { cancelled = true }
  }, [builtIn])

  // an agent opens on its stored text; a copy opens on the source's, under a new id
  useEffect(() => {
    const source = agentId ?? seedFrom
    if (builtIn || source === null) return
    let cancelled = false
    agentsApi.getManifest(source)
      .then((res) => {
        if (cancelled) return
        const stored = res.data.manifest
        const loaded = agentId ? stored : withKey(stored, 'id', `${source}-copy`)
        setText(loaded)
        setOpened(agentId ? loaded : BLANK)
        setPhase('ready')
      })
      .catch((e) => { if (!cancelled) { setLoadErr(errMsg(e)); setPhase('error') } })
    return () => { cancelled = true }
  }, [agentId, seedFrom, builtIn])

  const read = useMemo(() => readText(text), [text])
  const doc = read.doc ?? {}
  const set = (key: string, value: unknown) => setText((t) => withKey(t, key, value))
  const id = asText(doc.id)
  const needs = asList(doc.needs)
  const chosenSkills = asList(doc.skills)
  const conclusion = asMap(doc.conclusion)
  const guard = asMap(doc.guard)

  // A tool is connected when the list knows it. Before the list arrives, or if it fails, the row's marks
  // stand in, and a tool with none is unknown (null) rather than not connected. A capability has no mark.
  const markOf = (tool: string): ToolChange | null | undefined =>
    capabilities.has(tool) ? null : connected ? connected[tool] : toolChanges[tool] ?? null
  const addable = useMemo(() => catalog && catalog.filter((t) => !needs.includes(t.name)), [catalog, needs])

  // Creating: the box is the description and a refinement is feedback on the draft. Editing: the box is the
  // change asked for, against the manifest as it stands. A draft the check refuses is not applied.
  const generate = (feedback?: string) => {
    const ask = (feedback ?? aiText).trim()
    if (!ask) return
    setAiBusy(true)
    setAiErr(null)
    const req = isCreate
      ? agentsApi.generateManifest({ description: aiText.trim(), current_draft: drafted ? text : null, feedback: feedback?.trim() || undefined })
      : agentsApi.generateManifest({ description: asText(doc.description) || id, current_draft: text, feedback: ask })
    req
      .then((res) => {
        setText(res.data.manifest)
        setDrafted(true)
        setAiFeedback('')
        if (!isCreate) setAiText('')
      })
      .catch((e) => setAiErr(errMsg(e)))
      .finally(() => setAiBusy(false))
  }

  const target = agentId ?? (id || PREFIX)
  const save = async () => {
    setBusy(true)
    setError(null)
    try {
      // a PUT replaces: a new agent must not land on an id that is taken
      if (isCreate) {
        const taken = await agentsApi.getManifest(target).then(() => true, () => false)
        if (taken) throw new Error(`${target} already exists. Choose another id.`)
      }
      const res = await agentsApi.putManifest(target, text)
      onSaved({ id: res.data.id })
    } catch (e) {
      setError(errMsg(e))
      setBusy(false)
    }
  }

  const title = builtIn ? `Agent · ${agentId}` : isCreate ? 'New agent' : `Edit agent · ${agentId!.replace(PREFIX, '')}`
  const skillOptions = (skills ?? []).filter((s) => !chosenSkills.includes(s.name))

  return (
    <OverlayPortal>
      <div className="vg-side-scrim" onMouseDown={close}>
        <aside
          className="vg-side-panel"
          role="dialog"
          aria-label={isCreate ? 'New agent' : builtIn ? 'Built-in agent' : 'Edit agent'}
          onMouseDown={(event) => event.stopPropagation()}
        >
          <div className="flex items-start gap-3">
            <span className="flex flex-col gap-[3px] grow min-w-0">
              <span className="text-[20px] font-bold leading-[1.25] tracking-[-0.2px] text-tx break-words">{title}</span>
              <span className="vg-side-hint">
                {builtIn
                  ? 'Built in. Forking stores it as a manifest of your own to edit; the original stays available.'
                  : 'An agent is its manifest. The fields and the YAML edit the same text.'}
              </span>
            </span>
            <button type="button" className="vg-side-close" aria-label="Close" onClick={close}>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M6.5 6.5l11 11M17.5 6.5l-11 11" />
              </svg>
            </button>
          </div>
          {builtIn && agentId && <BuiltIn agentId={agentId} onForked={(newId) => onSaved({ id: newId })} />}
          {!builtIn && phase === 'loading' && <p className="text-[13px] text-tx-3">Loading agent…</p>}
          {!builtIn && phase === 'error' && <div className="vg-side-err">Couldn’t load agent: {loadErr}</div>}
          {!builtIn && phase === 'ready' && (
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
                      <Icon name="sparkle" size={13} />&nbsp;{aiBusy ? 'Drafting…' : drafted ? 'Draft again' : 'Draft manifest'}
                    </button>
                  </div>
                  {drafted && (
                    <div className="vg-side-assist">
                      <Icon name="sparkle" size={16} />
                      <label htmlFor="vg-a-refine" className="vg-side-assist-text">Draft checked and applied below. Refine it: “add memory-forensics tools”</label>
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
              ) : !isCreate && (
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

              <TabStrip
                label="Manifest views"
                tabs={[{ id: 'fields', label: 'Fields' }, { id: 'yaml', label: 'YAML' }] as const}
                active={view}
                onChange={setView}
              />

              {view === 'yaml' && (
                <div className="vg-side-field">
                  <label htmlFor="vg-a-yaml" className="vg-side-label">Manifest (YAML)</label>
                  <textarea id="vg-a-yaml" className="vg-side-input mono" rows={24} spellCheck={false} value={text} onChange={(e) => setText(e.target.value)} />
                </div>
              )}

              {view === 'fields' && read.error !== null && (
                <div className="vg-side-err">
                  The YAML does not read as fields ({read.error}).{' '}
                  <button type="button" className="vg-side-link" onClick={() => setView('yaml')}>Fix it in YAML</button>
                </div>
              )}

              {view === 'fields' && read.error === null && (
                <>
                  <div className="vg-side-field">
                    <label htmlFor="vg-a-id" className="vg-side-label">Id</label>
                    <div className="flex items-center gap-2">
                      <span className="mono text-[12px] text-tx-2">{PREFIX}</span>
                      <input
                        id="vg-a-id"
                        className="vg-side-input mono"
                        value={id.startsWith(PREFIX) ? id.slice(PREFIX.length) : id}
                        autoComplete="off"
                        spellCheck={false}
                        disabled={!isCreate}
                        placeholder="phishing-triage"
                        onChange={(e) => set('id', `${PREFIX}${e.target.value}`)}
                      />
                    </div>
                    {!isCreate && <span className="vg-side-hint">The id names the agent; fork it to use another.</span>}
                  </div>
                  <div className="vg-side-field">
                    <label htmlFor="vg-a-name" className="vg-side-label">Name</label>
                    <input
                      id="vg-a-name"
                      className="vg-side-input"
                      value={asText(doc.name)}
                      autoComplete="off"
                      placeholder={id.startsWith(PREFIX) && id.length > PREFIX.length ? id.slice(PREFIX.length) : 'The id, without custom-'}
                      onChange={(e) => set('name', e.target.value === '' ? undefined : e.target.value)}
                    />
                  </div>
                  <div className="vg-side-field">
                    <label htmlFor="vg-a-description" className="vg-side-label">Description</label>
                    <textarea id="vg-a-description" className="vg-side-input" rows={3} value={asText(doc.description)} onChange={(e) => set('description', e.target.value)} />
                  </div>
                  <div className="vg-side-field">
                    <label htmlFor="vg-a-system" className="vg-side-label">System prompt</label>
                    <textarea id="vg-a-system" className="vg-side-input mono" rows={10} value={asText(doc.system)} onChange={(e) => set('system', e.target.value)} />
                  </div>

                  <div className="flex flex-col gap-2">
                    <span className="vg-side-title">Needs</span>
                    <span className="vg-side-hint">A capability binds whichever tool this deployment has for it; a tool is granted by name.</span>
                    {needs.length === 0 && <span className="vg-side-hint">No needs yet.</span>}
                    {needs.map((need) => (
                      <Chip
                        key={need}
                        name={need}
                        mark={capabilities.has(need) ? <span className="vg-side-mark none">Capability</span> : <ToolMark change={markOf(need)} />}
                        onRemove={() => set('needs', needs.filter((n) => n !== need))}
                      />
                    ))}
                    <ToolFinder catalog={addable} markOf={markOf} onAdd={(tool) => set('needs', [...needs, tool])} />
                  </div>

                  <div className="flex flex-col gap-2">
                    <span className="vg-side-title">Skills</span>
                    {chosenSkills.length === 0 && <span className="vg-side-hint">No skills.</span>}
                    {chosenSkills.map((s) => (
                      <Chip key={s} name={s} onRemove={() => set('skills', chosenSkills.length > 1 ? chosenSkills.filter((x) => x !== s) : undefined)} />
                    ))}
                    <select
                      className="vg-side-input"
                      aria-label="Add a skill"
                      value=""
                      disabled={!skillOptions.length}
                      onChange={(e) => e.target.value && set('skills', [...chosenSkills, e.target.value])}
                    >
                      <option value="">{skills === null ? 'Loading skills…' : skillOptions.length ? 'Add a skill…' : 'No more skills in the library'}</option>
                      {skillOptions.map((s) => <option key={s.name} value={s.name}>{s.name}</option>)}
                    </select>
                  </div>

                  <div className="vg-side-card">
                    <span className="vg-side-title">How it ends</span>
                    <div className="vg-side-grid">
                      <div className="vg-side-field">
                        <label htmlFor="vg-a-conclusion" className="vg-side-label">Conclusion</label>
                        <select
                          id="vg-a-conclusion"
                          className="vg-side-input"
                          value={conclusion ? 'structured' : asText(doc.conclusion) === 'prose' ? 'prose' : ''}
                          onChange={(e) => set('conclusion', e.target.value === 'prose' ? 'prose' : e.target.value === 'structured' ? STRUCTURED : undefined)}
                        >
                          {!conclusion && asText(doc.conclusion) !== 'prose' && <option value="">Not set</option>}
                          <option value="prose">Prose</option>
                          <option value="structured">Structured</option>
                        </select>
                      </div>
                      <div className="vg-side-field">
                        <label htmlFor="vg-a-record" className="vg-side-label">Hypothesis record</label>
                        <select id="vg-a-record" className="vg-side-input" value={asText(doc.record)} onChange={(e) => set('record', e.target.value || undefined)}>
                          <option value="">Default</option>
                          {RECORD_MODES.map((m) => <option key={m} value={m}>{m}</option>)}
                          {doc.record != null && !RECORD_MODES.includes(asText(doc.record) as typeof RECORD_MODES[number]) && <option value={asText(doc.record)}>{asText(doc.record)}</option>}
                        </select>
                      </div>
                      <div className="vg-side-field">
                        <label htmlFor="vg-a-turns" className="vg-side-label">Most turns</label>
                        <input id="vg-a-turns" className="vg-side-input" type="number" min={1} value={asText(doc.max_turns)} placeholder="The run's limit" onChange={(e) => set('max_turns', intOrNone(e.target.value))} />
                      </div>
                      <div className="vg-side-field">
                        <label htmlFor="vg-a-min" className="vg-side-label">Fewest results</label>
                        <input id="vg-a-min" className="vg-side-input" type="number" min={1} value={asText(doc.min_results)} placeholder="No floor" onChange={(e) => set('min_results', intOrNone(e.target.value))} />
                      </div>
                    </div>
                    <span className="vg-side-hint">
                      {conclusion
                        ? <>A structured conclusion’s schema and fields are edited in <button type="button" className="vg-side-link" onClick={() => setView('yaml')}>YAML</button>.</>
                        : 'Prose ends on what the agent last says.'}{' '}
                      The record defaults to available.
                    </span>
                  </div>

                  <div className="vg-side-card">
                    <div className="flex items-center gap-2">
                      <span className="vg-side-title grow">Guard</span>
                      <button type="button" className="vg-side-btn small line" onClick={() => set('guard', guard ? undefined : GUARD)}>
                        {guard ? 'Remove guard' : 'Add a guard'}
                      </button>
                    </div>
                    {!guard && <span className="vg-side-hint">A reviewer that holds a conclusion field’s chosen values for a second look. Needs a structured conclusion.</span>}
                    {guard && (
                      <>
                        <div className="vg-side-grid">
                          <div className="vg-side-field">
                            <label htmlFor="vg-a-gfield" className="vg-side-label">Field</label>
                            <input id="vg-a-gfield" className="vg-side-input mono" value={asText(guard.field)} autoComplete="off" onChange={(e) => set('guard', { ...guard, field: e.target.value })} />
                          </div>
                          <div className="vg-side-field">
                            <label htmlFor="vg-a-gfallback" className="vg-side-label">Held values become</label>
                            <input id="vg-a-gfallback" className="vg-side-input mono" value={asText(guard.fallback)} autoComplete="off" onChange={(e) => set('guard', { ...guard, fallback: e.target.value })} />
                          </div>
                        </div>
                        <div className="vg-side-field">
                          <label htmlFor="vg-a-guarded" className="vg-side-label">Values it holds (comma separated)</label>
                          <input
                            id="vg-a-guarded"
                            className="vg-side-input mono"
                            value={asList(guard.guarded).join(', ')}
                            autoComplete="off"
                            onChange={(e) => set('guard', { ...guard, guarded: e.target.value === '' ? [] : e.target.value.split(/,\s*/) })}
                          />
                        </div>
                        <div className="vg-side-field">
                          <label htmlFor="vg-a-greview" className="vg-side-label">Reviewer prompt</label>
                          <textarea id="vg-a-greview" className="vg-side-input" rows={3} value={asText(guard.reviewer_prompt)} onChange={(e) => set('guard', { ...guard, reviewer_prompt: e.target.value })} />
                        </div>
                      </>
                    )}
                  </div>
                </>
              )}

              {error && <div className="vg-side-err" role="alert">{error}</div>}
              <div className="vg-side-foot">
                <button type="button" className="vg-side-btn" onClick={close}>Cancel</button>
                <button type="button" className="vg-side-btn primary" disabled={busy || (!isCreate && text === opened)} onClick={save}>
                  {busy ? (isCreate ? 'Creating…' : 'Saving…') : isCreate ? 'Create agent' : 'Save'}
                </button>
              </div>
            </>
          )}
        </aside>
      </div>
      <ConfirmDialog
        open={confirmClose}
        title="Discard changes?"
        body={`Your changes to ${agentId ? agentId.replace(PREFIX, '') : 'this agent'} are not saved.`}
        confirmLabel="Discard changes"
        onConfirm={onClose}
        onClose={() => setConfirmClose(false)}
      />
    </OverlayPortal>
  )
}
