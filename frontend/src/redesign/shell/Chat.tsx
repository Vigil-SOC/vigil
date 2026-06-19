/* ============================================================
   Vigil chat dock — Cursor-style, wired to the real Claude stream.
   POSTs /api/claude/chat/stream and renders the SSE thinking/text
   events live. Agent list comes from agentsApi. Styling uses the
   Tailwind-authored chat-* component classes in styles.css plus
   utilities for one-offs.
   ============================================================ */
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { format } from 'date-fns'
import { Markdown } from '../shared/Markdown'
import { Icon } from '../shared/icons'
import { basePath } from '../../config/basePath'
import { agentsApi, claudeApi, mcpApi } from '../../services/api'
import { Popup, Select } from '../shared/ui'

interface ChatAgent {
  id: string
  name: string
  specialization?: string
  description?: string
  icon?: string
  color?: string
}
type Role = 'user' | 'vigil' | 'error'
interface ChatMsg {
  role: Role
  text: string
  thinking?: string
  ms?: number
}

const MODEL = 'claude-sonnet-4-6'
const CONTEXT_WINDOW = 200000
// shown only until the live model list arrives from GET /claude/models
const MODEL_FALLBACK = [{ id: MODEL, name: 'Claude Sonnet 4.6' }]
const newSessionId = () =>
  typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `sess-${Date.now()}-${Math.floor(Math.random() * 1e6)}`

/* ---------- conversation history (localStorage-backed) ---------- */
interface Conversation {
  id: string
  title: string
  ts: number
  messages: ChatMsg[]
}
const HISTORY_KEY = 'soc.chat.history'
const HISTORY_MAX = 30
function loadHistory(): Conversation[] {
  try {
    const raw = JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]')
    return Array.isArray(raw) ? raw : []
  } catch {
    return []
  }
}
function saveHistory(list: Conversation[]) {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(list.slice(0, HISTORY_MAX)))
  } catch {
    /* localStorage unavailable / full — keep the in-memory list only */
  }
}

/* ---------- chat settings (persisted, mirrors the classic drawer) ---------- */
interface ChatSettings {
  model: string
  maxTokens: number
  enableThinking: boolean
  thinkingBudget: number
  systemPrompt: string
}
const SETTINGS_KEY = 'soc.chat.settings'
const DEFAULT_SETTINGS: ChatSettings = { model: MODEL, maxTokens: 4096, enableThinking: false, thinkingBudget: 10000, systemPrompt: '' }
function loadSettings(): ChatSettings {
  try {
    return { ...DEFAULT_SETTINGS, ...JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}') }
  } catch {
    return DEFAULT_SETTINGS
  }
}


function VigilMessage({ text, thinking, ms }: { text: string; thinking?: string; ms?: number }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="msg vigil">
      {thinking && (
        <div className="thought toggle" onClick={() => setOpen((o) => !o)}>
          {`Reasoned${ms != null ? ` for ${(ms / 1000).toFixed(1)}s` : ''} ${open ? '▾' : '▸'}`}
        </div>
      )}
      {thinking && open && <div className="thinking-body">{thinking}</div>}
      <div className="body"><Markdown>{text}</Markdown></div>
      <div className="msg-actions">
        <button title="Copy" onClick={() => navigator.clipboard?.writeText(text)}><Icon name="copy" size={15} /></button>
        <button title="More"><Icon name="more" size={15} /></button>
      </div>
    </div>
  )
}

export default function Chat({
  open,
  onClose,
  seed,
  onSeedConsumed,
}: {
  open: boolean
  onClose: () => void
  /** when set, auto-send this prompt (e.g. "Investigate finding …") */
  seed?: string | null
  onSeedConsumed?: () => void
}) {
  const [messages, setMessages] = useState<ChatMsg[]>([])
  const [draft, setDraft] = useState('')
  const [loading, setLoading] = useState(false)
  const [streamText, setStreamText] = useState('')
  const [streamThinking, setStreamThinking] = useState('')
  const [isThinking, setIsThinking] = useState(false)
  const [agents, setAgents] = useState<ChatAgent[]>([])
  const [agentId, setAgentId] = useState('')
  const [menuOpen, setMenuOpen] = useState(false)
  const [agentsInfoOpen, setAgentsInfoOpen] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [history, setHistory] = useState<Conversation[]>(() => loadHistory())
  // chat settings — mirror the classic drawer; persisted across sessions
  const [savedSettings] = useState(loadSettings)
  const [model, setModel] = useState(savedSettings.model)
  const [maxTokens, setMaxTokens] = useState(savedSettings.maxTokens)
  const [enableThinking, setEnableThinking] = useState(savedSettings.enableThinking)
  const [thinkingBudget, setThinkingBudget] = useState(savedSettings.thinkingBudget)
  const [systemPrompt, setSystemPrompt] = useState(savedSettings.systemPrompt)
  const [models, setModels] = useState<{ id: string; name: string }[]>([])
  const [mcpStatus, setMcpStatus] = useState<{ available: number; total: number } | null>(null)

  const sessionRef = useRef<string>(newSessionId())
  const bodyRef = useRef<HTMLDivElement>(null)
  const taRef = useRef<HTMLTextAreaElement>(null)
  const abortRef = useRef<AbortController | null>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const openerRef = useRef<HTMLElement | null>(null)

  // focus the composer when the dock opens; return focus to the opener on close
  useEffect(() => {
    if (open) {
      openerRef.current = document.activeElement as HTMLElement | null
      taRef.current?.focus()
    } else {
      openerRef.current?.focus?.()
      openerRef.current = null
    }
  }, [open])

  // Esc closes the agent menu first, then the dock
  useEffect(() => {
    if (!open) return
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key !== 'Escape') return
      if (menuOpen) setMenuOpen(false)
      else onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, menuOpen, onClose])

  // load the agent roster once
  useEffect(() => {
    agentsApi
      .listAgents()
      .then((res) => {
        const raw = (res.data?.agents || []) as ChatAgent[]
        const list = raw.map((a) => ({ id: a.id, name: a.name, specialization: a.specialization, description: a.description, icon: a.icon, color: a.color }))
        setAgents(list)
        const corr = list.find((a) => a.id === 'correlator' || /correlat/i.test(a.name))
        if (corr) setAgentId(corr.id)
      })
      .catch(() => {})
  }, [])

  // model list + MCP tool status — fetched once, the first time the dock opens
  const metaLoadedRef = useRef(false)
  useEffect(() => {
    if (!open || metaLoadedRef.current) return
    metaLoadedRef.current = true
    claudeApi
      .getModels()
      .then((r) => setModels((r.data?.models || []) as { id: string; name: string }[]))
      .catch(() => {})
    mcpApi
      .getStatuses()
      .then((r) => {
        const statuses = (r.data?.statuses || []) as { status?: string }[]
        const available = statuses.filter((s) => s.status && s.status !== 'error' && s.status !== 'not found').length
        setMcpStatus({ available, total: statuses.length })
      })
      .catch(() => {})
  }, [open])

  // persist settings on change ("automatically saved", like the classic drawer)
  useEffect(() => {
    try {
      localStorage.setItem(SETTINGS_KEY, JSON.stringify({ model, maxTokens, enableThinking, thinkingBudget, systemPrompt }))
    } catch {
      /* ignore — settings stay in memory for the session */
    }
  }, [model, maxTokens, enableThinking, thinkingBudget, systemPrompt])

  // autoscroll on new content
  useEffect(() => {
    const el = bodyRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages, streamText, streamThinking, loading])

  // autosize the composer
  useEffect(() => {
    const ta = taRef.current
    if (!ta) return
    ta.style.height = 'auto'
    ta.style.height = Math.min(ta.scrollHeight, 130) + 'px'
  }, [draft])

  // close the agent menu on outside click
  useEffect(() => {
    if (!menuOpen) return
    const onDocClick = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false)
    }
    document.addEventListener('mousedown', onDocClick)
    return () => document.removeEventListener('mousedown', onDocClick)
  }, [menuOpen])

  const agentName = agents.find((a) => a.id === agentId)?.name || 'Default agent'

  // rough client-side context estimate (~chars/4) for the Status gauge — the
  // exact server count_tokens isn't worth a round-trip in the preview dock
  const estimatedTokens = useMemo(() => {
    const chars =
      messages.reduce((n, m) => n + m.text.length, 0) +
      streamText.length + streamThinking.length + systemPrompt.length
    return Math.round(chars / 4)
  }, [messages, streamText, streamThinking, systemPrompt])
  const ctxPct = Math.min((estimatedTokens / CONTEXT_WINDOW) * 100, 100)
  const ctxState = estimatedTokens > 150000 ? 'danger' : estimatedTokens > 100000 ? 'warn' : 'ok'

  const send = async (override?: string) => {
    const text = (override ?? draft).trim()
    if (!text || loading) return
    const history = messages.filter((m) => m.role !== 'error')
    const next: ChatMsg[] = [...history, { role: 'user', text }]
    setMessages(next)
    setDraft('')
    setLoading(true)
    setStreamText('')
    setStreamThinking('')
    setIsThinking(false)
    const start = Date.now()

    const ac = new AbortController()
    abortRef.current = ac
    try {
      const res = await fetch(`${basePath}/api/claude/chat/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
        body: JSON.stringify({
          messages: next.map((m) => ({ role: m.role === 'vigil' ? 'assistant' : 'user', content: m.text })),
          model,
          max_tokens: maxTokens,
          enable_thinking: enableThinking,
          thinking_budget: enableThinking ? thinkingBudget : undefined,
          system_prompt: systemPrompt || undefined,
          agent_id: agentId || undefined,
          session_id: sessionRef.current,
        }),
        signal: ac.signal,
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const reader = res.body?.getReader()
      const decoder = new TextDecoder()
      let curText = ''
      let curThinking = ''
      let buf = ''
      if (reader) {
        for (;;) {
          const { done, value } = await reader.read()
          if (done) break
          buf += decoder.decode(value, { stream: true })
          const lines = buf.split('\n')
          buf = lines.pop() || ''
          for (const line of lines) {
            if (!line.startsWith('data: ')) continue
            const data = line.slice(6).trim()
            if (!data) continue
            let ev: { type?: string; content?: string; error?: string }
            try {
              ev = JSON.parse(data)
            } catch {
              continue
            }
            if (ev.error) throw new Error(ev.error)
            if (ev.type === 'thinking_start') {
              setIsThinking(true)
              curThinking = ''
            } else if (ev.type === 'thinking') {
              curThinking += ev.content || ''
              setStreamThinking(curThinking)
            } else if (ev.type === 'thinking_end') {
              setIsThinking(false)
            } else if (ev.type === 'text') {
              curText += ev.content || ''
              setStreamText(curText)
            }
            // tool_processing / context_summarized: ignored in the dock
          }
        }
      }
      const ms = Date.now() - start
      setMessages((m) => [...m, { role: 'vigil', text: curText || '_(no response)_', thinking: curThinking || undefined, ms }])
    } catch (e) {
      const err = e as { name?: string; message?: string }
      if (err?.name !== 'AbortError') {
        setMessages((m) => [...m, { role: 'error', text: `Could not reach Vigil: ${err?.message || e}. Is the backend running?` }])
      }
    } finally {
      setLoading(false)
      setStreamText('')
      setStreamThinking('')
      setIsThinking(false)
      abortRef.current = null
    }
  }

  const stop = () => abortRef.current?.abort()

  // snapshot the current conversation into history (most-recent first, de-duped)
  const archiveCurrent = () => {
    if (messages.length === 0) return
    const firstUser = messages.find((m) => m.role === 'user')
    const convo: Conversation = {
      id: sessionRef.current,
      title: (firstUser?.text || 'Conversation').replace(/\s+/g, ' ').trim().slice(0, 70) || 'Conversation',
      ts: Date.now(),
      messages,
    }
    setHistory((h) => {
      const next = [convo, ...h.filter((c) => c.id !== convo.id)].slice(0, HISTORY_MAX)
      saveHistory(next)
      return next
    })
  }

  // clear chat — archives the current thread first, then starts a fresh session
  const reset = () => {
    if (loading) return
    archiveCurrent()
    setMessages([])
    sessionRef.current = newSessionId()
  }

  const loadConversation = (c: Conversation) => {
    if (loading) return
    archiveCurrent()
    setMessages(c.messages)
    sessionRef.current = c.id
    setHistoryOpen(false)
  }

  const deleteConversation = (id: string) => {
    setHistory((h) => {
      const next = h.filter((c) => c.id !== id)
      saveHistory(next)
      return next
    })
  }

  // auto-send a seeded prompt (e.g. "Investigate finding …") when the dock is
  // opened from an "Investigate with Vigil" affordance
  const seedRef = useRef<string | null>(null)
  useEffect(() => {
    // reset once the parent clears the seed, so the same finding can be
    // investigated again later
    if (!seed) {
      seedRef.current = null
      return
    }
    // guard against StrictMode's double-invoke firing the same seed twice
    if (open && seed !== seedRef.current && !loading) {
      seedRef.current = seed
      send(seed)
      onSeedConsumed?.()
    }
    // send/loading intentionally omitted: we fire once per new seed
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, seed])
  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }

  return (
    <>
    <aside
      className={`chat${open ? ' open' : ''}`}
      role="dialog"
      aria-label="Vigil Assistant"
      aria-hidden={!open}
    >
      <div className="chat-head">
        <span className="ch-ico"><Icon name="brain" /></span>
        <h3 className="ch-title">Vigil Assistant</h3>
        <div className="hbtns">
          <button title="History" onClick={() => setHistoryOpen(true)}><Icon name="clock" /></button>
          <button title="SOC Agents" onClick={() => setAgentsInfoOpen(true)}><Icon name="note" /></button>
          <button title="Chat settings" onClick={() => setSettingsOpen(true)}><Icon name="gear" /></button>
          <button title="Clear chat" onClick={reset} disabled={loading || messages.length === 0}><Icon name="trash" /></button>
          <button title="Close panel" onClick={onClose}><Icon name="close" /></button>
        </div>
      </div>

      <div className="chat-body" ref={bodyRef}>
        {messages.length === 0 && !loading && (
          <div className="chat-empty">Ask Vigil to investigate a finding, correlate activity, or summarize a case.</div>
        )}
        {messages.map((m, i) =>
          m.role === 'user' ? (
            <div className="msg user" key={i}><div className="body">{m.text}</div></div>
          ) : m.role === 'error' ? (
            <div className="msg vigil err" key={i}><div className="body">{m.text}</div></div>
          ) : (
            <VigilMessage key={i} text={m.text} thinking={m.thinking} ms={m.ms} />
          )
        )}
        {loading && (
          <div className="msg vigil">
            {/* always-on processing indicator so the user knows Vigil is still
                working — the phase label tracks reasoning → responding */}
            <div className="vigil-status" aria-live="polite">
              <span className="vs-dots" aria-hidden="true"><i /><i /><i /></span>
              <span className="vs-label">
                {isThinking
                  ? 'Vigil is reasoning'
                  : streamText
                    ? 'Vigil is responding'
                    : 'Vigil is working on it'}
                …
              </span>
            </div>
            {isThinking && streamThinking && <div className="thinking-body">{streamThinking}</div>}
            {streamText && <div className="body"><Markdown>{streamText}</Markdown></div>}
          </div>
        )}
      </div>

      <div className="chat-foot">
        <div className="chat-input">
          <textarea
            ref={taRef}
            rows={1}
            placeholder="Ask Vigil, / for commands, @ for context"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={onKeyDown}
          />
          <div className="ci-row">
            <div className="model-wrap" ref={menuRef}>
              <button className="model-sel" onClick={() => setMenuOpen((o) => !o)}>
                <span className="m-ico"><Icon name="infinity" /></span>
                {agentName} <span className="dd"><Icon name="chevD" size={12} /></span>
              </button>
              {menuOpen && (
                <div className="agent-menu">
                  <button className={agentId === '' ? 'sel' : ''} onClick={() => { setAgentId(''); setMenuOpen(false) }}>
                    Default agent<span className="am-spec">No specific agent</span>
                  </button>
                  {agents.map((a) => (
                    <button key={a.id} className={a.id === agentId ? 'sel' : ''} onClick={() => { setAgentId(a.id); setMenuOpen(false) }}>
                      <span className="am-name">
                        {a.icon && <span className="am-ico" style={{ color: a.color }}>{a.icon}</span>}
                        {a.name}
                      </span>
                      {a.specialization && <span className="am-spec">{a.specialization}</span>}
                      {a.description && <span className="am-desc">{a.description}</span>}
                    </button>
                  ))}
                </div>
              )}
            </div>
            <div className="ci-grow" />
            {loading ? (
              <button className="ci-send busy" title="Stop" onClick={stop}><Icon name="x2" size={15} /></button>
            ) : (
              <button className="ci-send" title="Send" onClick={() => send()} disabled={!draft.trim()}><Icon name="send" /></button>
            )}
          </div>
        </div>
      </div>
    </aside>

    {/* Conversation history — cleared/loaded threads live here (localStorage) */}
    <Popup open={historyOpen} onClose={() => setHistoryOpen(false)} title="Conversation history" width={460}>
      {history.length === 0 ? (
        <div className="muted">No past conversations yet. When you clear the chat, the thread is saved here so you can reopen it.</div>
      ) : (
        <div className="chat-history">
          {history.map((c) => (
            <div key={c.id} className={`chist-row${c.id === sessionRef.current ? ' current' : ''}`}>
              <button className="chist-main" onClick={() => loadConversation(c)}>
                <span className="chist-title">{c.title}</span>
                <span className="chist-meta">{c.messages.length} message{c.messages.length === 1 ? '' : 's'} · {format(c.ts, 'MMM d, HH:mm')}</span>
              </button>
              <button className="chist-del" title="Delete" onClick={() => deleteConversation(c.id)}><Icon name="trash" size={14} /></button>
            </div>
          ))}
        </div>
      )}
    </Popup>

    {/* Chat settings — Status / Model settings / Advanced (mirrors the classic drawer) */}
    <Popup open={settingsOpen} onClose={() => setSettingsOpen(false)} title="Chat settings" width={440}>
      <div className="chat-settings">
        {/* Status */}
        <section className="cs-sec">
          <div className="cs-head">Status</div>
          <div className="cs-stat-row">
            <span className="cs-name">MCP Tools</span>
            {mcpStatus ? (
              <span className={`cs-chip ${mcpStatus.available > 0 ? 'ok' : 'danger'}`}>
                {mcpStatus.available}/{mcpStatus.total}
              </span>
            ) : (
              <span className="muted">checking…</span>
            )}
          </div>
          <div className="cs-ctx">
            <span className={`cs-ctx-label ${ctxState}`}>
              Context ~{estimatedTokens.toLocaleString()} / {CONTEXT_WINDOW.toLocaleString()} tokens
            </span>
            <div className="cs-bar"><span className={`cs-bar-fill ${ctxState}`} style={{ width: `${ctxPct}%` }} /></div>
            <span className="cs-ctx-sub">Output max {maxTokens.toLocaleString()} tokens</span>
          </div>
        </section>

        {/* Model settings */}
        <section className="cs-sec">
          <div className="cs-head">Model settings</div>
          <div className="cs-field">
            <span className="cs-name">Model</span>
            <Select
              value={model}
              onSelect={setModel}
              options={(models.length ? models : MODEL_FALLBACK).map((m) => ({ value: m.id, label: m.name }))}
            />
          </div>
          <div className="cs-field">
            <span className="cs-name">Max tokens</span>
            <input
              className="cs-input"
              type="number"
              min={256}
              max={64000}
              value={maxTokens}
              onChange={(e) => setMaxTokens(parseInt(e.target.value, 10) || 4096)}
            />
          </div>
          <div className="cs-row">
            <span className="cs-text">
              <span className="cs-name">Extended thinking</span>
              <span className="cs-help">Stream Vigil’s reasoning before each answer.</span>
            </span>
            <button
              type="button"
              role="switch"
              aria-checked={enableThinking}
              aria-label="Extended thinking"
              className={`cs-switch${enableThinking ? ' on' : ''}`}
              onClick={() => setEnableThinking((v) => !v)}
            >
              <span className="cs-knob" />
            </button>
          </div>
          {enableThinking && (
            <div className="cs-field">
              <span className="cs-name">Thinking budget (tokens)</span>
              <input
                className="cs-input"
                type="number"
                min={1024}
                max={maxTokens}
                value={thinkingBudget}
                onChange={(e) => setThinkingBudget(parseInt(e.target.value, 10) || 10000)}
              />
              <span className="cs-help">Max tokens Vigil can use for reasoning.</span>
            </div>
          )}
        </section>

        {/* Advanced */}
        <section className="cs-sec">
          <div className="cs-head">Advanced</div>
          <div className="cs-field">
            <span className="cs-name">System prompt <span className="cs-opt">(optional)</span></span>
            <textarea
              className="cs-input cs-area"
              rows={3}
              value={systemPrompt}
              placeholder="Override default system prompt…"
              onChange={(e) => setSystemPrompt(e.target.value)}
            />
            <span className="cs-help">Leave empty to use the default prompt. Settings are saved automatically.</span>
          </div>
        </section>
      </div>
    </Popup>

    {/* SOC Agents reference — rendered outside the transformed .chat aside so
        the fixed overlay positions against the viewport */}
    <Popup open={agentsInfoOpen} onClose={() => setAgentsInfoOpen(false)} title="SOC Agents" width={460}>
      {agents.length === 0 ? (
        <div className="muted">No agents available.</div>
      ) : (
        <div className="agent-cards">
          {agents.map((a) => (
            <div key={a.id} className="agent-card" style={{ borderLeftColor: a.color || 'var(--accent)' }}>
              <div className="ac-head">
                {a.icon && <span className="ac-ico" style={{ color: a.color }}>{a.icon}</span>}
                <span className="ac-name">{a.name}</span>
                {a.specialization && <span className="ac-spec">{a.specialization}</span>}
              </div>
              {a.description && <p className="ac-desc">{a.description}</p>}
            </div>
          ))}
        </div>
      )}
    </Popup>
    </>
  )
}
