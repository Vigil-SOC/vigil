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
import {
  agentsApi,
  aiConfigApi,
  analyticsApi,
  claudeApi,
  conversationsApi,
  mcpApi,
  reasoningApi,
  streamFetch,
  type CostEstimate,
  type ConversationDetail,
  type ImportConversationInput,
} from '../../services/api'
import { notificationService } from '../../services/notifications'
import { useConversations } from './useConversations'
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

/* ---------- reasoning trace (GH #79 — chain-of-thought visibility) ---------- */
interface SessionSummary {
  total_interactions: number
  total_cost_usd: number
  total_input_tokens: number
  total_output_tokens: number
}
interface TraceItem {
  interaction_id: string
  created_at?: string
  has_thinking?: boolean
  has_tools?: boolean
  agent_id?: string
  input_tokens?: number
  output_tokens?: number
  cost_usd?: number
}
interface TraceDetail {
  interaction_id: string
  model?: string
  stop_reason?: string
  duration_ms?: number
  cost_usd?: number
  thinking_content?: string
  response_content?: string
  tool_calls?: Array<{ name?: string; input?: unknown }>
  tool_results?: Array<{ tool_use_id?: string; content?: unknown; is_error?: boolean }>
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
  /** investigation dedup key (the seed prompt) when this thread was opened from
   *  an "Investigate with Vigil" affordance — lets re-opening the same finding/
   *  case restore the thread instead of starting a duplicate one */
  key?: string
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

/* Investigation dedup: key (seed prompt) → conversation/session id. The server
   conversation store has no "key" column, so this small localStorage map
   preserves the "reopen the same finding's thread" behavior. */
const KEYMAP_KEY = 'soc.chat.keymap'
function loadKeymap(): Record<string, string> {
  try {
    const m = JSON.parse(localStorage.getItem(KEYMAP_KEY) || '{}')
    return m && typeof m === 'object' ? (m as Record<string, string>) : {}
  } catch {
    return {}
  }
}
function setKeymapEntry(key: string, sid: string) {
  try {
    const m = loadKeymap()
    m[key] = sid
    localStorage.setItem(KEYMAP_KEY, JSON.stringify(m))
  } catch {
    /* ignore */
  }
}

/* one-time migration marker: localStorage history → server store */
const IMPORT_MARKER_KEY = 'soc.chat.imported'

/* a unified row for the history drawer (server summary, or offline cache) */
interface HistRow {
  id: string
  title: string
  count: number
  ts: number | null
  archived?: boolean
}
function histTime(ts: number | null): string {
  if (ts == null) return ''
  const d = new Date(ts)
  return isNaN(d.getTime()) ? '' : format(d, 'MMM d, HH:mm')
}
/* server ConversationMessage[] → the dock's ChatMsg[] (user + assistant only) */
function toChatMsgs(msgs: ConversationDetail['messages']): ChatMsg[] {
  return msgs
    .filter((m) => m.role === 'user' || m.role === 'assistant')
    .map((m) =>
      m.role === 'user'
        ? { role: 'user' as Role, text: m.content }
        : {
            role: 'vigil' as Role,
            text: m.content || '_(no response)_',
            thinking: m.thinking || undefined,
          },
    )
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


/* format an interaction timestamp for the trace list (HH:mm:ss, guarded) */
function traceTime(s?: string): string {
  if (!s) return '—'
  const d = new Date(s)
  return isNaN(d.getTime()) ? '—' : format(d, 'HH:mm:ss')
}
/* render tool input/results as readable JSON without throwing on cycles */
function safeJson(v: unknown): string {
  if (v == null) return ''
  if (typeof v === 'string') return v
  try {
    return JSON.stringify(v, null, 2)
  } catch {
    return String(v)
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
  // true between a `tool_processing` event and the next `text` chunk — the
  // backend is executing MCP tools, mirroring the classic drawer's indicator
  const [isProcessingTools, setIsProcessingTools] = useState(false)
  const [agents, setAgents] = useState<ChatAgent[]>([])
  const [agentId, setAgentId] = useState('')
  const [menuOpen, setMenuOpen] = useState(false)
  const [agentsInfoOpen, setAgentsInfoOpen] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  // Offline cache (localStorage) — server is the source of truth (useConversations
  // below); this is the fallback shown when the server list can't be reached.
  const [history, setHistory] = useState<Conversation[]>(() => loadHistory())
  const [showArchived, setShowArchived] = useState(false)
  const {
    items: serverConvos,
    phase: histPhase,
    reload: reloadHistory,
  } = useConversations(showArchived)
  const [renamingId, setRenamingId] = useState<string | null>(null)
  const [renameDraft, setRenameDraft] = useState('')
  // chat settings — mirror the classic drawer; persisted across sessions
  const [savedSettings] = useState(loadSettings)
  const [model, setModel] = useState(savedSettings.model)
  const [maxTokens, setMaxTokens] = useState(savedSettings.maxTokens)
  const [enableThinking, setEnableThinking] = useState(savedSettings.enableThinking)
  const [thinkingBudget, setThinkingBudget] = useState(savedSettings.thinkingBudget)
  const [systemPrompt, setSystemPrompt] = useState(savedSettings.systemPrompt)
  const [models, setModels] = useState<{ id: string; name: string }[]>([])
  const [mcpStatus, setMcpStatus] = useState<{ available: number; total: number } | null>(null)
  // live pre-call estimate from the backend (exact count_tokens + USD band),
  // mirroring the classic drawer; null until the first debounced estimate lands
  const [costEstimate, setCostEstimate] = useState<CostEstimate | null>(null)
  const [exactTokens, setExactTokens] = useState<number | null>(null)
  // reasoning trace — the per-interaction chain-of-thought for this session
  const [traceOpen, setTraceOpen] = useState(false)
  const [traceLoading, setTraceLoading] = useState(false)
  const [traceItems, setTraceItems] = useState<TraceItem[]>([])
  const [traceSelected, setTraceSelected] = useState<TraceDetail | null>(null)
  const [sessionSummary, setSessionSummary] = useState<SessionSummary | null>(null)

  const sessionRef = useRef<string>(newSessionId())
  const bodyRef = useRef<HTMLDivElement>(null)
  const taRef = useRef<HTMLTextAreaElement>(null)
  const abortRef = useRef<AbortController | null>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const openerRef = useRef<HTMLElement | null>(null)
  const panelRef = useRef<HTMLElement>(null)
  // the investigation key (seed prompt) of the current thread, if any
  const currentKeyRef = useRef<string | null>(null)
  // Default the model to the configured `chat_default` assignment (Settings →
  // AI Config) unless the user already has saved settings or picks a model
  // this session — then their choice wins.
  const settingsExistedRef = useRef<boolean>(
    typeof localStorage !== 'undefined' && localStorage.getItem(SETTINGS_KEY) != null,
  )
  const userPickedModelRef = useRef(false)

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

  // any of the dock's own dialogs (they own their Esc + focus handling)
  const anyPopupOpen = historyOpen || settingsOpen || agentsInfoOpen || traceOpen

  // Esc closes the agent menu first, then the dock — but never while one of the
  // dock's Popups is open (those handle their own Esc; closing the dock too
  // would dismiss both at once)
  useEffect(() => {
    if (!open) return
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key !== 'Escape' || anyPopupOpen) return
      if (menuOpen) setMenuOpen(false)
      else onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, menuOpen, onClose, anyPopupOpen])

  // focus trap — keep Tab within the dock while it's open (unless a Popup is
  // up, which traps focus itself). Completes the dialog a11y: role=dialog +
  // aria-modal + Esc + focus-return are already wired (REDESIGN_GAPS.md §10).
  const onPanelKeyDown = (e: KeyboardEvent<HTMLElement>) => {
    if (e.key !== 'Tab' || !open || anyPopupOpen) return
    const root = panelRef.current
    if (!root) return
    const f = Array.from(
      root.querySelectorAll<HTMLElement>(
        'button:not([disabled]), textarea, input, a[href], [tabindex]:not([tabindex="-1"])',
      ),
    ).filter((el) => el.offsetParent !== null)
    if (f.length === 0) return
    const first = f[0]
    const last = f[f.length - 1]
    const active = document.activeElement as HTMLElement
    if (e.shiftKey && active === first) {
      e.preventDefault()
      last.focus()
    } else if (!e.shiftKey && active === last) {
      e.preventDefault()
      first.focus()
    }
  }

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
    aiConfigApi
      .getConfig()
      .then((r) => {
        const configured = r.data?.assignments?.chat_default?.model_id
        if (configured && !settingsExistedRef.current && !userPickedModelRef.current) {
          setModel(configured)
        }
      })
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

  // Pre-call cost + exact-token estimate (debounced 400ms, abortable), mirroring
  // the classic drawer: the backend runs Anthropic count_tokens and prices the
  // call so the user sees both before sending. Best-effort — keeps the previous
  // estimate on failure and falls back to the char heuristic until the first
  // estimate lands.
  useEffect(() => {
    if (!open) return
    const ctrl = new AbortController()
    const t = setTimeout(() => {
      const payloadMsgs = [
        ...messages
          .filter((m) => m.role !== 'error')
          .map((m) => ({ role: m.role === 'vigil' ? 'assistant' : 'user', content: m.text })),
        ...(draft.trim() ? [{ role: 'user', content: draft }] : []),
      ]
      if (payloadMsgs.length === 0 && !systemPrompt) {
        setCostEstimate(null)
        setExactTokens(null)
        return
      }
      analyticsApi
        .estimateCost({
          provider_type: 'anthropic',
          model_id: model,
          messages: payloadMsgs,
          system_prompt: systemPrompt || undefined,
          max_tokens: maxTokens,
        })
        .then((r) => {
          if (ctrl.signal.aborted) return
          setCostEstimate(r.data)
          setExactTokens(r.data.input_tokens)
        })
        .catch(() => {
          /* keep the previous estimate */
        })
    }, 400)
    return () => {
      clearTimeout(t)
      ctrl.abort()
    }
  }, [open, messages, draft, systemPrompt, model, maxTokens])

  // char heuristic (~chars/4), used only until the first server estimate lands
  const heuristicTokens = useMemo(() => {
    const chars =
      messages.reduce((n, m) => n + m.text.length, 0) +
      streamText.length + streamThinking.length + systemPrompt.length + draft.length
    return Math.round(chars / 4)
  }, [messages, streamText, streamThinking, systemPrompt, draft])
  const estimatedTokens = exactTokens ?? heuristicTokens
  const ctxPct = Math.min((estimatedTokens / CONTEXT_WINDOW) * 100, 100)
  const ctxState = estimatedTokens > 150000 ? 'danger' : estimatedTokens > 100000 ? 'warn' : 'ok'
  const costTitle = costEstimate
    ? `${
        costEstimate.token_count_method === 'anthropic_count_tokens'
          ? 'Exact token count via Anthropic count_tokens.'
          : costEstimate.token_count_method === 'tiktoken'
            ? 'Token count via tiktoken.'
            : 'Approximate token count (chars ÷ 4).'
      } Pricing: ${costEstimate.pricing_source}.`
    : ''

  const send = async (override?: string, opts?: { fresh?: boolean }) => {
    const text = (override ?? draft).trim()
    if (!text || loading) return
    // `fresh` starts a clean thread (used when opening a new investigation) so
    // the seed isn't appended onto an unrelated conversation
    const base = opts?.fresh ? [] : messages.filter((m) => m.role !== 'error')
    const next: ChatMsg[] = [...base, { role: 'user', text }]
    setMessages(next)
    setDraft('')
    setLoading(true)
    setStreamText('')
    setStreamThinking('')
    setIsThinking(false)
    setIsProcessingTools(false)
    const start = Date.now()

    const ac = new AbortController()
    abortRef.current = ac
    try {
      const res = await streamFetch('/claude/chat/stream', {
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
            let ev: {
              type?: string
              content?: string
              error?: string
              windowed_messages?: number
              remaining_messages?: number
            }
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
            } else if (ev.type === 'tool_processing') {
              // backend is running MCP tools — show the live indicator and
              // separate any tool output from the prose preceding it
              setIsProcessingTools(true)
              if (curText && !curText.endsWith('\n\n')) curText += '\n\n'
            } else if (ev.type === 'context_windowed') {
              curText +=
                `_[Context compressed: ${ev.windowed_messages ?? 0} older ` +
                `messages condensed to stay within the model's limits; recent ` +
                `messages and key details are preserved.]_\n\n`
              setStreamText(curText)
            } else if (ev.type === 'text') {
              setIsProcessingTools(false)
              curText += ev.content || ''
              setStreamText(curText)
            }
          }
        }
      }
      const ms = Date.now() - start
      setMessages((m) => [...m, { role: 'vigil', text: curText || '_(no response)_', thinking: curThinking || undefined, ms }])
      // Fire a desktop notification on completion, matching the classic
      // drawer (which notifies when an investigation-seeded thread finishes).
      // Gated inside notificationService by the `show_notifications` setting +
      // browser permission, so it's a no-op when the user hasn't opted in.
      if (currentKeyRef.current && curText) {
        const summary = curText.replace(/[#*`_>[\]]/g, '').replace(/\s+/g, ' ').trim().slice(0, 140)
        notificationService.notifyInvestigationComplete({
          title: 'Vigil',
          summary: summary || 'Analysis complete',
        })
      }
      // refresh the reasoning-trace summary for this session (best-effort)
      reasoningApi
        .getSessionSummary(sessionRef.current)
        .then((s: Partial<SessionSummary> | null) =>
          setSessionSummary(
            s
              ? {
                  total_interactions: s.total_interactions ?? 0,
                  total_cost_usd: s.total_cost_usd ?? 0,
                  total_input_tokens: s.total_input_tokens ?? 0,
                  total_output_tokens: s.total_output_tokens ?? 0,
                }
              : null,
          ),
        )
        .catch(() => {})
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
      setIsProcessingTools(false)
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
      key: currentKeyRef.current || undefined,
    }
    setHistory((h) => {
      const next = [convo, ...h.filter((c) => c.id !== convo.id)].slice(0, HISTORY_MAX)
      saveHistory(next)
      return next
    })
  }

  // clear chat — caches the current thread, then starts a fresh session. The
  // server already persisted every turn (write-through), so this just resets
  // local state and refreshes the history list.
  const reset = () => {
    if (loading) return
    archiveCurrent()
    setMessages([])
    sessionRef.current = newSessionId()
    currentKeyRef.current = null
    setSessionSummary(null)
    setCostEstimate(null)
    setExactTokens(null)
    reloadHistory()
  }

  // Reopen a conversation from the server (continues the same session_id so new
  // turns append to it). Falls back to the offline cache if the server can't be
  // reached. Returns whether anything was loaded.
  const openConversation = async (id: string, key?: string | null): Promise<boolean> => {
    if (loading) return false
    archiveCurrent()
    setHistoryOpen(false)
    try {
      const res = await conversationsApi.get(id)
      const detail = res.data as ConversationDetail
      setMessages(toChatMsgs(detail.messages || []))
      sessionRef.current = id
      currentKeyRef.current = key ?? null
      setSessionSummary(null)
      setCostEstimate(null)
      setExactTokens(null)
      return true
    } catch {
      // offline fallback: the localStorage cache
      const cached = loadHistory().find((c) => c.id === id)
      if (cached) {
        setMessages(cached.messages)
        sessionRef.current = id
        currentKeyRef.current = cached.key || key || null
        setSessionSummary(null)
        return true
      }
      return false
    }
  }

  // open an investigation thread for a seed prompt: reuse the matching thread
  // (current or archived) if one exists, else archive the current and start a
  // fresh one. The seed prompt is deterministic per finding/case, so it doubles
  // as the dedup key (investigation-keyed, like the classic drawer's tabs).
  const openInvestigation = async (prompt: string) => {
    if (loading) return
    if (currentKeyRef.current === prompt && messages.length > 0) return // already here
    const mapped = loadKeymap()[prompt]
    if (mapped) {
      const opened = await openConversation(mapped, prompt)
      if (opened) return // reopened the existing thread for this finding/case
    }
    archiveCurrent()
    sessionRef.current = newSessionId()
    currentKeyRef.current = prompt
    setKeymapEntry(prompt, sessionRef.current) // so re-opening this finding restores it
    setSessionSummary(null)
    setCostEstimate(null)
    setExactTokens(null)
    send(prompt, { fresh: true })
  }

  // load the per-interaction reasoning trace for the current session
  const openReasoningTrace = () => {
    setTraceOpen(true)
    setTraceLoading(true)
    setTraceSelected(null)
    const sid = sessionRef.current
    reasoningApi
      .listInteractions(sid, { limit: 200 })
      .then((r: { interactions?: TraceItem[] }) => setTraceItems(r?.interactions || []))
      .catch(() => setTraceItems([]))
      .finally(() => setTraceLoading(false))
    reasoningApi
      .getSessionSummary(sid)
      .then((s: Partial<SessionSummary> | null) =>
        setSessionSummary(
          s
            ? {
                total_interactions: s.total_interactions ?? 0,
                total_cost_usd: s.total_cost_usd ?? 0,
                total_input_tokens: s.total_input_tokens ?? 0,
                total_output_tokens: s.total_output_tokens ?? 0,
              }
            : null,
        ),
      )
      .catch(() => {})
  }

  const loadTraceInteraction = (interactionId: string) => {
    reasoningApi
      .getInteraction(sessionRef.current, interactionId)
      .then((d: TraceDetail) => setTraceSelected(d))
      .catch(() => {})
  }

  const deleteConversation = async (id: string) => {
    try {
      await conversationsApi.delete(id)
      reloadHistory()
    } catch {
      /* best-effort — still drop it from the offline cache below */
    }
    setHistory((h) => {
      const next = h.filter((c) => c.id !== id)
      saveHistory(next)
      return next
    })
  }

  const archiveConversation = async (id: string, archived: boolean) => {
    try {
      await conversationsApi.update(id, { archived })
      reloadHistory()
    } catch {
      /* ignore — server unreachable */
    }
  }

  const commitRename = async (id: string) => {
    const title = renameDraft.trim()
    setRenamingId(null)
    if (!title) return
    try {
      await conversationsApi.update(id, { title })
      reloadHistory()
    } catch {
      /* ignore — server unreachable */
    }
  }

  // One-time migration: import any localStorage chat history into the server
  // store so existing local chats aren't orphaned. Best-effort; the marker is
  // only set on success so a failed import retries on the next mount.
  const migratedRef = useRef(false)
  useEffect(() => {
    if (migratedRef.current) return
    migratedRef.current = true
    try {
      if (localStorage.getItem(IMPORT_MARKER_KEY)) return
      const local = loadHistory()
      if (local.length === 0) {
        localStorage.setItem(IMPORT_MARKER_KEY, '1')
        return
      }
      const payload: ImportConversationInput[] = local.map((c) => ({
        id: c.id,
        title: c.title,
        messages: (c.messages || [])
          .filter((m) => m.role !== 'error')
          .map((m) => ({
            role: m.role === 'vigil' ? 'assistant' : 'user',
            content: m.text,
            thinking: m.role === 'vigil' ? m.thinking || null : null,
          })),
      }))
      // preserve investigation dedup keys across the migration
      for (const c of local) if (c.key) setKeymapEntry(c.key, c.id)
      conversationsApi
        .importHistory(payload)
        .then(() => {
          try {
            localStorage.setItem(IMPORT_MARKER_KEY, '1')
          } catch {
            /* ignore */
          }
          reloadHistory()
        })
        .catch(() => {
          /* leave the marker unset so the import retries next mount */
        })
    } catch {
      /* localStorage unavailable — nothing to migrate */
    }
  }, [reloadHistory])

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
      openInvestigation(seed)
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
      ref={panelRef}
      className={`chat${open ? ' open' : ''}`}
      role="dialog"
      aria-label="Vigil Assistant"
      aria-hidden={!open}
      onKeyDown={onPanelKeyDown}
    >
      <div className="chat-head">
        <span className="ch-ico"><Icon name="brain" /></span>
        <h3 className="ch-title">Vigil Assistant</h3>
        <div className="hbtns">
          <button title="History" onClick={() => { setHistoryOpen(true); reloadHistory() }}><Icon name="clock" /></button>
          <button title="Reasoning trace" onClick={openReasoningTrace}><Icon name="reason" /></button>
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
                  : isProcessingTools
                    ? 'Vigil is running tools'
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
        <div className="chat-meta">
          <div className="cm-line">
            <span className={`cm-ctx ${ctxState}`} title="Estimated context usage for the next request">
              {estimatedTokens.toLocaleString()} / {CONTEXT_WINDOW / 1000}k tokens
              {estimatedTokens > 150000 && <span className="cm-warn"> · auto-summarizes on send</span>}
            </span>
            {costEstimate && (
              <span className="cm-cost" title={costTitle}>
                ~${costEstimate.low_usd.toFixed(4)}–${costEstimate.high_usd.toFixed(4)}
                {costEstimate.pricing_source !== 'exact' && (
                  <span className="cm-src"> · {costEstimate.pricing_source}</span>
                )}
              </span>
            )}
          </div>
          <div className="cm-bar"><span className={`cm-bar-fill ${ctxState}`} style={{ width: `${ctxPct}%` }} /></div>
        </div>
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

    {/* Conversation history — server-backed (cross-device); falls back to the
        localStorage cache when the server can't be reached. */}
    <Popup open={historyOpen} onClose={() => setHistoryOpen(false)} title="Conversation history" width={460}>
      {(() => {
        const offline = histPhase === 'error'
        const rows: HistRow[] = offline
          ? history.map((c) => ({
              id: c.id,
              title: c.title || 'Untitled conversation',
              count: c.messages?.length || 0,
              ts: c.ts || null,
            }))
          : serverConvos.map((c) => ({
              id: c.id,
              title: c.title || 'Untitled conversation',
              count: c.message_count,
              ts: c.last_message_at
                ? Date.parse(c.last_message_at)
                : c.updated_at
                  ? Date.parse(c.updated_at)
                  : null,
              archived: c.archived,
            }))
        return (
          <>
            <div className="chist-toolbar">
              {offline && <span className="muted">Offline — showing cached conversations.</span>}
              <label className="chist-archtoggle">
                <input
                  type="checkbox"
                  checked={showArchived}
                  onChange={(e) => setShowArchived(e.target.checked)}
                  disabled={offline}
                />
                Show archived
              </label>
            </div>
            {histPhase === 'loading' ? (
              <div className="muted">Loading…</div>
            ) : rows.length === 0 ? (
              <div className="muted">
                No past conversations yet. Your chats are saved automatically so you can
                reopen them on any device.
              </div>
            ) : (
              <div className="chat-history">
                {rows.map((c) => (
                  <div
                    key={c.id}
                    className={`chist-row${c.id === sessionRef.current ? ' current' : ''}${c.archived ? ' archived' : ''}`}
                  >
                    {renamingId === c.id ? (
                      <input
                        className="chist-rename"
                        autoFocus
                        value={renameDraft}
                        onChange={(e) => setRenameDraft(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') {
                            e.preventDefault()
                            commitRename(c.id)
                          } else if (e.key === 'Escape') {
                            setRenamingId(null)
                          }
                        }}
                        onBlur={() => commitRename(c.id)}
                      />
                    ) : (
                      <button className="chist-main" onClick={() => openConversation(c.id)}>
                        <span className="chist-title">{c.title}</span>
                        <span className="chist-meta">
                          {c.count} message{c.count === 1 ? '' : 's'}
                          {c.ts != null && histTime(c.ts) ? ` · ${histTime(c.ts)}` : ''}
                        </span>
                      </button>
                    )}
                    <div className="chist-actions">
                      <button
                        className="chist-act"
                        title="Rename"
                        disabled={offline}
                        onClick={() => {
                          setRenamingId(c.id)
                          setRenameDraft(c.title)
                        }}
                      >
                        <Icon name="edit" size={14} />
                      </button>
                      <button
                        className="chist-act"
                        title={c.archived ? 'Unarchive' : 'Archive'}
                        disabled={offline}
                        onClick={() => archiveConversation(c.id, !c.archived)}
                      >
                        <Icon name="folder" size={14} />
                      </button>
                      <button
                        className="chist-act chist-del"
                        title="Delete"
                        onClick={() => deleteConversation(c.id)}
                      >
                        <Icon name="trash" size={14} />
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </>
        )
      })()}
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
              Context {exactTokens != null ? '' : '~'}{estimatedTokens.toLocaleString()} / {CONTEXT_WINDOW.toLocaleString()} tokens
              {estimatedTokens > 150000 && ' · auto-summarizes on next send'}
            </span>
            <div className="cs-bar"><span className={`cs-bar-fill ${ctxState}`} style={{ width: `${ctxPct}%` }} /></div>
            <span className="cs-ctx-sub">Output max {maxTokens.toLocaleString()} tokens</span>
          </div>
          {costEstimate && (
            <div className="cs-stat-row" title={costTitle}>
              <span className="cs-name">Est. cost</span>
              <span className="cs-cost-val">
                ${costEstimate.low_usd.toFixed(4)}–${costEstimate.high_usd.toFixed(4)}
                {costEstimate.pricing_source !== 'exact' && <span className="cs-ctx-sub"> · {costEstimate.pricing_source}</span>}
              </span>
            </div>
          )}
        </section>

        {/* Model settings */}
        <section className="cs-sec">
          <div className="cs-head">Model settings</div>
          <div className="cs-field">
            <span className="cs-name">Model</span>
            <Select
              value={model}
              onSelect={(m) => { userPickedModelRef.current = true; setModel(m) }}
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

    {/* Reasoning trace — per-interaction chain-of-thought for this session */}
    <Popup open={traceOpen} onClose={() => setTraceOpen(false)} title="Reasoning trace" width={760}>
      {sessionSummary && (
        <div className="trace-sum">
          {sessionSummary.total_interactions} call{sessionSummary.total_interactions === 1 ? '' : 's'}
          {' · '}${sessionSummary.total_cost_usd.toFixed(4)}
          {' · '}{(sessionSummary.total_input_tokens + sessionSummary.total_output_tokens).toLocaleString()} tokens
        </div>
      )}
      <div className="trace">
        <div className="trace-list">
          {traceLoading ? (
            <div className="muted">Loading…</div>
          ) : traceItems.length === 0 ? (
            <div className="muted">No reasoning recorded for this conversation yet.</div>
          ) : (
            traceItems.map((it) => (
              <button
                key={it.interaction_id}
                className={`trace-row${traceSelected?.interaction_id === it.interaction_id ? ' sel' : ''}`}
                onClick={() => loadTraceInteraction(it.interaction_id)}
              >
                <span className="trace-row-top">
                  <span>{traceTime(it.created_at)}</span>
                  {it.has_thinking && <span className="trace-chip" title="Has thinking">💭</span>}
                  {it.has_tools && <span className="trace-chip" title="Used tools">🔧</span>}
                  <span className="trace-row-agent">{it.agent_id || 'chat'}</span>
                </span>
                <span className="trace-row-meta">
                  {(it.input_tokens ?? 0).toLocaleString()} in · {(it.output_tokens ?? 0).toLocaleString()} out · ${(it.cost_usd ?? 0).toFixed(4)}
                </span>
              </button>
            ))
          )}
        </div>
        <div className="trace-detail">
          {!traceSelected ? (
            <div className="muted">{traceItems.length ? 'Select an interaction to inspect its reasoning.' : ''}</div>
          ) : (
            <>
              <div className="trace-meta">
                <span>{traceSelected.model || '—'}</span>
                {traceSelected.stop_reason && <span>· {traceSelected.stop_reason}</span>}
                {typeof traceSelected.duration_ms === 'number' && <span>· {(traceSelected.duration_ms / 1000).toFixed(1)}s</span>}
                {typeof traceSelected.cost_usd === 'number' && <span>· ${traceSelected.cost_usd.toFixed(4)}</span>}
              </div>
              {traceSelected.thinking_content && (
                <div className="trace-block thinking">
                  <div className="trace-block-h">💭 Thinking</div>
                  <div className="trace-block-b">{traceSelected.thinking_content}</div>
                </div>
              )}
              {traceSelected.response_content && (
                <div className="trace-block">
                  <div className="trace-block-h">Response</div>
                  <div className="trace-block-b"><Markdown>{traceSelected.response_content}</Markdown></div>
                </div>
              )}
              {traceSelected.tool_calls?.map((tc, i) => (
                <div key={`c${i}`} className="trace-block tool">
                  <div className="trace-block-h">🔧 {tc.name || 'tool'}</div>
                  <div className="trace-block-b mono">{safeJson(tc.input)}</div>
                </div>
              ))}
              {traceSelected.tool_results?.map((tr, i) => (
                <div key={`r${i}`} className={`trace-block ${tr.is_error ? 'err' : 'ok'}`}>
                  <div className="trace-block-h">{tr.is_error ? 'Tool error' : 'Tool result'}</div>
                  <div className="trace-block-b mono">{typeof tr.content === 'string' ? tr.content : safeJson(tr.content)}</div>
                </div>
              ))}
            </>
          )}
        </div>
      </div>
    </Popup>
    </>
  )
}
