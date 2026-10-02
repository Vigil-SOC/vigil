import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
} from 'react'
import { format } from 'date-fns'
import { Markdown } from '../shared/Markdown'
import { Icon } from '../shared/icons'
import { Cost } from '../shared/cost'
import api, {
  agentsApi,
  conversationsApi,
  reasoningApi,
  streamFetch,
  type ConversationDetail,
  type ImportConversationInput,
} from '../services/api'
import { notificationService } from '../services/notifications'
import { useConversations } from './useConversations'
import { Popup } from '../shared/ui'

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
  ms?: number
}

interface SessionSummary {
  total_interactions: number
  total_cost_usd: number | null
  unpriced_calls: number
  total_input_tokens: number
  total_output_tokens: number
}

function sessionSummaryFrom(s: Partial<SessionSummary>): SessionSummary {
  return {
    total_interactions: s.total_interactions ?? 0,
    total_cost_usd: s.total_cost_usd ?? null,
    unpriced_calls: s.unpriced_calls ?? 0,
    total_input_tokens: s.total_input_tokens ?? 0,
    total_output_tokens: s.total_output_tokens ?? 0,
  }
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

const newSessionId = () => crypto.randomUUID()

interface Conversation {
  id: string
  title: string
  ts: number
  messages: ChatMsg[]
  /** the seed prompt, when opened from an "Investigate with Vigil" affordance;
   *  re-opening the same finding restores the thread instead of duplicating it */
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
    /* empty */
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

const IMPORT_MARKER_KEY = 'soc.chat.imported'

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
/* user + assistant turns only */
function toChatMsgs(msgs: ConversationDetail['messages']): ChatMsg[] {
  return msgs
    .filter((m) => m.role === 'user' || m.role === 'assistant')
    .map((m) =>
      m.role === 'user'
        ? { role: 'user' as Role, text: m.content }
        : { role: 'vigil' as Role, text: m.content || '_(no response)_' },
    )
}

interface CaseHit {
  id: string
  title: string
}

function caseHits(data: unknown): CaseHit[] {
  const root = data as { cases?: unknown; comments?: unknown; evidence?: unknown } | null
  const seen = new Set<string>()
  const hits: CaseHit[] = []
  for (const bucket of [root?.cases, root?.comments, root?.evidence]) {
    if (!Array.isArray(bucket)) continue
    for (const item of bucket) {
      const row = item as { case_id?: unknown; title?: unknown; name?: unknown; content?: unknown }
      if (typeof row.case_id !== 'string' || !row.case_id || seen.has(row.case_id)) continue
      seen.add(row.case_id)
      const title = [row.title, row.name, row.content].find(
        (value) => typeof value === 'string' && value.trim(),
      ) as string | undefined
      hits.push({ id: row.case_id, title: title?.trim() || row.case_id })
    }
  }
  return hits
}

/** The token after the @ still being typed, or null when the draft is not mentioning. */
function mentionToken(text: string): string | null {
  const match = text.match(/(^|\s)@(\S*)$/)
  return match ? match[2] : null
}

function dayLabel(ts: number | null): string {
  if (ts == null || Number.isNaN(ts)) return 'Undated'
  return format(new Date(ts), 'MMM d, yyyy')
}

function traceTime(s?: string): string {
  if (!s) return '—'
  const d = new Date(s)
  return isNaN(d.getTime()) ? '—' : format(d, 'HH:mm:ss')
}
/* without throwing on cycles */
function safeJson(v: unknown): string {
  if (v == null) return ''
  if (typeof v === 'string') return v
  try {
    return JSON.stringify(v, null, 2)
  } catch {
    return String(v)
  }
}

function VigilMessage({ text }: { text: string; ms?: number }) {
  return (
    <div className="msg vigil">
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
  pageKey,
  pageTitle,
  onSeedConsumed,
}: {
  open: boolean
  onClose: () => void
  seed?: string | null
  /** Route key (`current` in SocConsole). Stored on the conversation; the title is display-only. */
  pageKey?: string
  pageTitle?: string
  onSeedConsumed?: () => void
}) {
  const [messages, setMessages] = useState<ChatMsg[]>([])
  const [draft, setDraft] = useState('')
  const [loading, setLoading] = useState(false)
  const [streamText, setStreamText] = useState('')
  // true between a `tool_processing` event and the next `text` chunk
  const [isProcessingTools, setIsProcessingTools] = useState(false)
  const [agents, setAgents] = useState<ChatAgent[]>([])
  const [agentsInfoOpen, setAgentsInfoOpen] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [histQuery, setHistQuery] = useState('')
  // the server is the source of truth; this shows when it can't be reached
  const [history, setHistory] = useState<Conversation[]>(() => loadHistory())
  const [showArchived, setShowArchived] = useState(false)
  const {
    items: serverConvos,
    phase: histPhase,
    reload: reloadHistory,
  } = useConversations(showArchived, histQuery)
  const [renamingId, setRenamingId] = useState<string | null>(null)
  const [renameDraft, setRenameDraft] = useState('')
  // null: the field is omitted. "" clears a case already stored on the row.
  const [caseId, setCaseId] = useState<string | null>(null)
  const [mentionHits, setMentionHits] = useState<CaseHit[] | null>(null)
  const [traceOpen, setTraceOpen] = useState(false)
  const [traceLoading, setTraceLoading] = useState(false)
  const [traceItems, setTraceItems] = useState<TraceItem[]>([])
  const [traceSelected, setTraceSelected] = useState<TraceDetail | null>(null)
  const [sessionSummary, setSessionSummary] = useState<SessionSummary | null>(null)

  const sessionRef = useRef<string>(newSessionId())
  const bodyRef = useRef<HTMLDivElement>(null)
  const taRef = useRef<HTMLTextAreaElement>(null)
  const abortRef = useRef<AbortController | null>(null)
  const openerRef = useRef<HTMLElement | null>(null)
  const panelRef = useRef<HTMLElement>(null)
  const currentKeyRef = useRef<string | null>(null)
  const caseIdRef = useRef<string | null>(null)
  // true once this session id has a conversation row, so an @ attach can PATCH
  const persistedRef = useRef(false)

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
  const anyPopupOpen = historyOpen || agentsInfoOpen || traceOpen

  // never while a Popup is open: it handles its own Esc, and closing the dock
  // too would dismiss both at once
  useEffect(() => {
    if (!open) return
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key !== 'Escape' || anyPopupOpen) return
      if (mentionHits) setMentionHits(null)
      else onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, mentionHits, onClose, anyPopupOpen])

  // unless a Popup is up, which traps focus itself
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

  useEffect(() => {
    agentsApi
      .listAgents()
      .then((res) => {
        const raw = (res.data?.agents || []) as ChatAgent[]
        setAgents(raw.map((a) => ({ id: a.id, name: a.name, specialization: a.specialization, description: a.description, icon: a.icon, color: a.color })))
      })
      .catch(() => {})
  }, [])

  useEffect(() => {
    const el = bodyRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages, streamText, loading])

  useEffect(() => {
    const ta = taRef.current
    if (!ta) return
    ta.style.height = 'auto'
    ta.style.height = Math.min(ta.scrollHeight, 130) + 'px'
  }, [draft])

  const token = mentionToken(draft)
  useEffect(() => {
    if (token == null || !token.trim()) {
      setMentionHits(token == null ? null : [])
      return
    }
    let live = true
    const timer = setTimeout(() => {
      api
        .get('/cases/search/full-text', { params: { query: token } })
        .then((res) => {
          if (live) setMentionHits(caseHits(res.data))
        })
        .catch(() => {
          if (live) setMentionHits([])
        })
    }, 150)
    return () => {
      live = false
      clearTimeout(timer)
    }
  }, [token])

  const rememberCase = (next: string | null) => {
    caseIdRef.current = next
    setCaseId(next)
  }

  const applyCase = (next: string | null) => {
    rememberCase(next)
    // A row is created on the first turn. Until then the id rides on that turn.
    if (persistedRef.current && next !== null) {
      conversationsApi.update(sessionRef.current, { case_id: next }).catch(() => {})
    }
  }

  const attachCase = (hit: CaseHit) => {
    applyCase(hit.id)
    setDraft((current) => current.replace(/(^|\s)@\S*$/, '$1').trim())
    setMentionHits(null)
  }

  const send = async (override?: string, opts?: { fresh?: boolean }) => {
    const text = (override ?? draft).trim()
    if (!text || loading) return
    // `fresh` keeps a new investigation's seed off an unrelated conversation
    const base = opts?.fresh ? [] : messages.filter((m) => m.role !== 'error')
    const next: ChatMsg[] = [...base, { role: 'user', text }]
    setMessages(next)
    setDraft('')
    setLoading(true)
    setStreamText('')
    setIsProcessingTools(false)
    const start = Date.now()

    const ac = new AbortController()
    abortRef.current = ac
    // The row is inserted when the stream ends. An @ change before that is held
    // and patched afterwards; patching at HTTP 200 races a row that is not there.
    const sentCase = caseIdRef.current
    let accepted = false
    try {
      const payload: {
        messages: { role: string; content: string }[]
        session_id: string
        page_context?: string
        case_id?: string
      } = {
        messages: next.map((m) => ({ role: m.role === 'vigil' ? 'assistant' : 'user', content: m.text })),
        session_id: sessionRef.current,
      }
      if (pageKey) payload.page_context = pageKey
      if (sentCase !== null) payload.case_id = sentCase
      const res = await streamFetch('/claude/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
        body: JSON.stringify(payload),
        signal: ac.signal,
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      accepted = true
      const reader = res.body?.getReader()
      const decoder = new TextDecoder()
      let curText = ''
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
            if (ev.type === 'tool_processing') {
              // separate tool output from the prose preceding it
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
      setMessages((m) => [...m, { role: 'vigil', text: curText || '_(no response)_', ms }])
      // gated inside notificationService by the setting + browser permission
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
          setSessionSummary(s ? sessionSummaryFrom(s) : null),
        )
        .catch(() => {})
    } catch (e) {
      const err = e as { name?: string; message?: string }
      if (err?.name !== 'AbortError') {
        setMessages((m) => [...m, { role: 'error', text: `Could not reach Vigil: ${err?.message || e}. Is the backend running?` }])
      }
    } finally {
      if (accepted) {
        const latest = caseIdRef.current
        persistedRef.current = true
        if (latest !== sentCase && latest !== null) {
          conversationsApi.update(sessionRef.current, { case_id: latest }).catch(() => {})
        }
      }
      setLoading(false)
      setStreamText('')
      setIsProcessingTools(false)
      abortRef.current = null
    }
  }

  const stop = () => abortRef.current?.abort()

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

  // the server already persisted every turn, so this only resets local state
  const reset = () => {
    if (loading) return
    archiveCurrent()
    setMessages([])
    sessionRef.current = newSessionId()
    currentKeyRef.current = null
    persistedRef.current = false
    rememberCase(null)
    setSessionSummary(null)
    reloadHistory()
  }

  // continues the same session_id, so new turns append to it
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
      persistedRef.current = true
      rememberCase(detail.case_id ?? null)
      setSessionSummary(null)
      return true
    } catch {
      const cached = loadHistory().find((c) => c.id === id)
      if (cached) {
        setMessages(cached.messages)
        sessionRef.current = id
        currentKeyRef.current = cached.key || key || null
        persistedRef.current = false
        rememberCase(null)
        setSessionSummary(null)
        return true
      }
      return false
    }
  }

  // the seed prompt is deterministic per finding/case, so it doubles as the
  // dedup key for reusing an existing thread
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
    persistedRef.current = false
    rememberCase(null)
    setKeymapEntry(prompt, sessionRef.current) // so re-opening this finding restores it
    setSessionSummary(null)
    send(prompt, { fresh: true })
  }

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
        setSessionSummary(s ? sessionSummaryFrom(s) : null),
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
      /* still drop it from the offline cache below */
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

  // the marker is only set on success, so a failed import retries next mount
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
          .map((m) => ({ role: m.role === 'vigil' ? 'assistant' : 'user', content: m.text, thinking: null })),
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
          /* retry next mount */
        })
    } catch {
      /* empty */
    }
  }, [reloadHistory])

  const seedRef = useRef<string | null>(null)
  useEffect(() => {
    // reset when the parent clears the seed, so the same finding can be
    // investigated again
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
          <button title="Clear chat" onClick={reset} disabled={loading || messages.length === 0}><Icon name="trash" /></button>
          <button type="button" title="Close assistant" aria-label="Close Vigil Assistant" onClick={onClose}><Icon name="close" /></button>
        </div>
      </div>
      <div className="chat-note">
        <span>Private to you</span>
        {pageTitle ? <span>Using {pageTitle}</span> : null}
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
            <VigilMessage key={i} text={m.text} ms={m.ms} />
          )
        )}
        {loading && (
          <div className="msg vigil">
            {/* always-on processing indicator so the user knows Vigil is still
                working — the phase label tracks reasoning → responding */}
            <div className="vigil-status" aria-live="polite">
              <span className="vs-dots" aria-hidden="true"><i /><i /><i /></span>
              <span className="vs-label">
                {isProcessingTools ? 'Vigil is running tools' : streamText ? 'Vigil is responding' : 'Vigil is working on it'}
                …
              </span>
            </div>
            {streamText && <div className="body"><Markdown>{streamText}</Markdown></div>}
          </div>
        )}
      </div>

      <div className="chat-foot">
        <div className="chat-input">
          {mentionHits && (
            <div className="chat-mention" role="listbox" aria-label="Matching cases">
              {mentionHits.length === 0 ? (
                <div className="cm-empty">No matching cases</div>
              ) : (
                mentionHits.map((hit) => (
                  <button key={hit.id} type="button" role="option" onMouseDown={(e) => e.preventDefault()} onClick={() => attachCase(hit)}>
                    <span>{hit.title}</span>
                    <span className="cm-id">{hit.id}</span>
                  </button>
                ))
              )}
            </div>
          )}
          <textarea
            ref={taRef}
            rows={1}
            placeholder="Ask Vigil, / for commands, @ for context"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={onKeyDown}
          />
          <div className="ci-row">
            {caseId ? (
              <span className="chat-case" data-testid="attached-case">
                <span>{caseId}</span>
                <button type="button" aria-label="Remove attached case" onClick={() => applyCase('')}>×</button>
              </span>
            ) : null}
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
              <input
                className="chist-search"
                aria-label="Search history"
                placeholder="Search history"
                value={histQuery}
                disabled={offline}
                onChange={(e) => setHistQuery(e.target.value)}
              />
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
                {rows.map((c, i) => {
                  const label = dayLabel(c.ts)
                  const prev = i > 0 ? dayLabel(rows[i - 1].ts) : null
                  return (
                  <div key={c.id}>
                    {label !== prev && <div className="chist-day">{label}</div>}
                  <div
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
                  </div>
                  )
                })}
              </div>
            )}
          </>
        )
      })()}
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
          {' · '}<Cost usd={sessionSummary.total_cost_usd} digits={4} />
          {sessionSummary.unpriced_calls > 0 && (
            <>{' · '}{sessionSummary.unpriced_calls.toLocaleString()} unpriced</>
          )}
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
                  {(it.input_tokens ?? 0).toLocaleString()} in · {(it.output_tokens ?? 0).toLocaleString()} out · <Cost usd={it.cost_usd} digits={4} />
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
                <span>· <Cost usd={traceSelected.cost_usd} digits={4} /></span>
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
