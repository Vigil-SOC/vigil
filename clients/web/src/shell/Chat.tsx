import { getPageSnapshot } from './pageSnapshot'
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type DragEvent,
  type KeyboardEvent,
} from 'react'
import { format } from 'date-fns'
import { Markdown, type MarkdownCite } from '../shared/Markdown'
import { citePattern, type Citation } from './citations'
import { ThreadGrip } from './ThreadGrip'
import { DockGrip } from './DockGrip'
import { useThreadHeight } from './useThreadHeight'
import { Icon } from '../shared/icons'
import { VigilMark } from '../shared/VigilLogo'
import { TITLES } from '../data/data'
import api, {
  agentsApi,
  casesApi,
  conversationsApi,
  streamFetch,
  workflowApi,
  type ConversationDetail,
} from '../services/api'
import { notificationService } from '../services/notifications'
import { useAuth } from '../contexts/AuthContext'
import { useConversations } from './useConversations'
import {
  loadHistory,
  loadKeymap,
  CHAT_HISTORY_MAX,
  retainChatCache,
  saveHistory,
  serverUnreachable,
  setKeymapEntry,
} from './chatCache'
import { LATER_RELEASE } from '../shared/later'
import { errorText } from '../shared/errorText'
import {
  attachRefusal, bindLine, parseLine, proposalFrom, startedText,
  type HuntAttachment, type HuntProposal, type LiveCommand,
} from './commandBarModel'
import { CaseRunBlock } from './ChatRun'
import { useToast } from './toast'
import { useNavigate } from 'react-router-dom'
import type { ChatAgent, ChatSeed, SeedDocument } from '../shared/types'
import { caseLabel } from '../shared/caseLabel'
import { prettyHandle } from '../data/appData'

type Role = 'user' | 'vigil' | 'error'
interface ChatMsg {
  role: Role
  text: string
  /** What the model thought before answering, when it says. Kept for this session only. */
  reasoning?: string
}

// randomUUID exists only in secure contexts (HTTPS or localhost); plain HTTP at a LAN address lacks it.
const newSessionId = (): string => {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  const b = crypto.getRandomValues(new Uint8Array(16))
  b[6] = (b[6] & 0x0f) | 0x40 // version 4
  b[8] = (b[8] & 0x3f) | 0x80 // RFC 4122 variant
  const h = Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('')
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`
}

class ServerRefusal extends Error {}

interface Conversation {
  id: string
  title: string
  ts: number
  messages: ChatMsg[]
  /** the seed prompt, when opened from an "Investigate with Vigil" affordance;
   *  re-opening the same finding restores the thread instead of duplicating it */
  key?: string
  /** the agent it runs under, so an offline reopen keeps it */
  agent?: ChatAgent
}

interface HistRow {
  id: string
  title: string
  count: number
  ts: number | null
  archived?: boolean
  /** "Case X", the page's nav title, or "General" */
  context: string
}
function histTime(ts: number | null): string {
  if (ts == null) return ''
  const d = new Date(ts)
  return isNaN(d.getTime()) ? '' : format(d, 'HH:mm')
}
function histContext(caseId?: string | null, pageContext?: string | null): string {
  if (caseId) return caseLabel(caseId)
  const titles = TITLES as Record<string, [string, string] | undefined>
  return (pageContext && titles[pageContext]?.[0]) || 'General'
}
/* user + assistant turns only. An incomplete reply is a failed turn; its content is the failure. */
function toChatMsgs(msgs: ConversationDetail['messages']): ChatMsg[] {
  return msgs
    .filter((m) => m.role === 'user' || m.role === 'assistant')
    .map((m) =>
      m.role === 'user'
        ? { role: 'user' as Role, text: m.content }
        : m.complete === false
          ? { role: 'error' as Role, text: m.content || 'This turn did not finish.' }
          : { role: 'vigil' as Role, text: m.content || '_(no response)_' },
    )
}

/** What the model is sent: a failed turn and its question were never answered, so neither goes. */
function askedOf(msgs: ChatMsg[]): ChatMsg[] {
  return msgs.filter((m, i) => m.role !== 'error' && msgs[i + 1]?.role !== 'error')
}

async function refusalOf(res: Response): Promise<string | null> {
  try {
    const detail = (await res.json())?.detail
    if (typeof detail === 'string' && detail) return detail
    if (typeof detail?.message === 'string' && detail.message) return detail.message
  } catch {
    /* no body */
  }
  return null
}

/** A file dropped on the dock: being read, refused with a reason, or read. A read file rides on every turn, as the case does. */
type ChatFile =
  | { status: 'reading'; name: string }
  | { status: 'refused'; name: string; reason: string }
  | {
      status: 'ready'; name: string; text: string; pages: number; condensed: boolean; named: boolean
      /** The original, which a run started here keeps on its case; absent for a file handed over already read. */
      file?: File
      pasted?: boolean
      /** The hypothesis the coverage check proposed for /hunt; null until asked. */
      proposal: HuntProposal | null
    }

/** The dock's file as the command bar's attachment, so a run started here takes the bar's path. */
function attachmentOf(file: ChatFile | null): HuntAttachment | null {
  if (file?.status !== 'ready') return file
  if (!file.file) return { status: 'refused', name: file.name, reason: `${file.name} came here without its original, which a run keeps on its case. Drop it on the dock to start a run with it.` }
  return { status: 'ready', name: file.name, file: file.file, pasted: Boolean(file.pasted), pages: file.pages, condensed: file.condensed, text: file.text, proposal: file.proposal }
}

/** The commands the dock runs itself; anything else typed with a slash is asked as written. */
const DOCK_COMMANDS = ['hunt', 'investigate']
const NO_JIRA = { gap: null, projectKey: '' }

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

/** What a chip reads, without its kind: an alert by its title, an explanation by its statement, a row by its step and source. */
function citeName(cite: Citation): string {
  return cite.kind === 'evidence' ? cite.label : cite.label.replace(/^(?:alert|explanation) · /, '')
}

/** One chip: what it cites, by step and source, with the row's text on hover (components/citation-chip.md). */
function CiteChip({ cite, onCite }: { cite: Citation; onCite?: (cite: Citation) => void }) {
  return cite.href ? (
    <a className={`cite-chip ${cite.kind}`} href={cite.href} target="_blank" rel="noopener noreferrer" title={cite.text}>
      {cite.label}
    </a>
  ) : (
    <button type="button" className={`cite-chip ${cite.kind}`} title={cite.text} onClick={() => onCite?.(cite)}>
      {cite.label}
    </button>
  )
}

/** An answer's text, with the ids it cites drawn as chips where it wrote them. */
function AnswerText({ text, citations, onCite }: { text: string; citations?: readonly Citation[]; onCite?: (cite: Citation) => void }) {
  const cite = useMemo<MarkdownCite | undefined>(() => {
    const pattern = citations ? citePattern(citations.map((c) => c.id)) : null
    if (!citations || !pattern) return undefined
    const byId = new Map(citations.map((c) => [c.id, c]))
    return {
      pattern,
      // once per paragraph as a chip; named again in words after that (#2459)
      render: (id: string, again: boolean) => {
        const hit = byId.get(id)
        if (!hit) return id
        return again ? <span className="cite-again" title={hit.text}>{citeName(hit)}</span> : <CiteChip cite={hit} onCite={onCite} />
      },
      // an alert or explanation chip names its kind itself
      kindWord: (id: string) => {
        const kind = byId.get(id)?.kind
        return kind === 'evidence' ? undefined : kind
      },
    }
  }, [citations, onCite])
  return <div className="body"><Markdown cite={cite}>{text}</Markdown></div>
}

/** The model's reasoning, folded above its answer. */
function Thinking({ text, live = false }: { text: string; live?: boolean }) {
  return (
    <details className="msg-think">
      <summary>{live ? 'Thinking…' : 'Thinking'}</summary>
      <div className="msg-think-body">{text}</div>
    </details>
  )
}

function VigilMessage({
  text,
  reasoning,
  citations,
  onCite,
  pinned,
}: {
  text: string
  reasoning?: string
  citations?: readonly Citation[]
  onCite?: (cite: Citation) => void
  pinned?: boolean
}) {
  const copy = (
    <button title="Copy" onClick={() => navigator.clipboard?.writeText(text)}><Icon name="copy" size={15} /></button>
  )
  const content = (
    <>
      {reasoning && <Thinking text={reasoning} />}
      <AnswerText text={text} citations={citations} onCite={onCite} />
      <div className="msg-actions">
        {copy}
      </div>
    </>
  )
  return pinned ? (
    <div className="msg vigil">{content}</div>
  ) : (
    <div className="msg vigil">
      <span className="msg-av"><VigilMark /></span>
      <div className="msg-main">{content}</div>
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
  pinned = false,
  lockedCaseId,
  citations,
  onCite,
  onTurnDone,
  collapseKey,
  size,
}: {
  open: boolean
  onClose: () => void
  seed?: ChatSeed | null
  /** Route key (`current` in SocConsole). Stored on the conversation; the title is display-only. */
  pageKey?: string
  pageTitle?: string
  onSeedConsumed?: () => void
  /** In-flow case composer. Omits the dock frame and header. */
  pinned?: boolean
  /** Sent on every turn. Hides @ and the remove-case control. */
  lockedCaseId?: string
  /** What an answer on this case can cite: its evidence rows, alerts and explanations. An id an answer writes is drawn as its chip. */
  citations?: readonly Citation[]
  onCite?: (cite: Citation) => void
  /** Called when a turn ends, so a page can re-read what the answer may cite. */
  onTurnDone?: () => void
  /** When this changes (the case tab), a pinned thread folds to its last exchange. */
  collapseKey?: string
  /** The dock's width and full-screen state, owned by the console so the page reflows beside it. */
  size?: {
    width: number
    min: number
    max: number
    resizable: boolean
    full: boolean
    onFull: (full: boolean) => void
    preview: (width: number) => void
    commit: (width: number | null) => void
  }
}) {
  const userId = useAuth().user?.user_id ?? null
  const [messages, setMessages] = useState<ChatMsg[]>([])
  const [draft, setDraft] = useState('')
  const [loading, setLoading] = useState(false)
  const [streamText, setStreamText] = useState('')
  const [streamReasoning, setStreamReasoning] = useState('')
  // true between a `tool_processing` event and the next `text` chunk
  const [isProcessingTools, setIsProcessingTools] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [histQuery, setHistQuery] = useState('')
  // the server is the source of truth; this shows when it can't be reached
  const [history, setHistory] = useState<Conversation[]>(() => loadHistory<Conversation>(userId))
  const [showArchived, setShowArchived] = useState(false)
  const {
    items: serverConvos,
    phase: histPhase,
    reload: reloadHistory,
    offline: historyOffline,
  } = useConversations(showArchived, histQuery, userId)
  const [renamingId, setRenamingId] = useState<string | null>(null)
  const [renameDraft, setRenameDraft] = useState('')
  // null: the field is omitted. "" clears a case already stored on the row.
  const [caseId, setCaseId] = useState<string | null>(lockedCaseId ?? null)
  const [threadOpen, setThreadOpen] = useState(false)
  // the case composer's thread: a share of the drawer by default, or what the person dragged it to
  const thread = useThreadHeight()
  const [earlierHidden, setEarlierHidden] = useState(false)
  const [mentionHits, setMentionHits] = useState<CaseHit[] | null>(null)
  // the token mentionHits answers, so "No matching cases" is never shown for a search still in flight
  const [mentionFor, setMentionFor] = useState<string | null>(null)
  const [mentionActive, setMentionActive] = useState(0)
  const [file, setFile] = useState<ChatFile | null>(null)
  const [dragging, setDragging] = useState(false)
  const fileSeq = useRef(0)
  // a run starting from the composer, and a nudge for the run block to re-read the case
  const [starting, setStarting] = useState(false)
  const [runSeq, setRunSeq] = useState(0)
  const { notify } = useToast()
  const navigate = useNavigate()

  const sessionRef = useRef<string>(newSessionId())
  const bodyRef = useRef<HTMLDivElement>(null)
  const taRef = useRef<HTMLTextAreaElement>(null)
  const abortRef = useRef<AbortController | null>(null)
  const openerRef = useRef<HTMLElement | null>(null)
  const panelRef = useRef<HTMLElement>(null)
  const currentKeyRef = useRef<string | null>(null)
  const caseIdRef = useRef<string | null>(lockedCaseId ?? null)
  const loadGen = useRef(0)
  const turnGen = useRef(0)
  const localTurn = useRef(false)
  const messagesRef = useRef<ChatMsg[]>([])
  messagesRef.current = messages
  // false until the locked case's conversation list has settled
  const caseReady = useRef(!lockedCaseId)
  const pendingSend = useRef<{ text: string; document?: SeedDocument } | null>(null)
  if (lockedCaseId) caseIdRef.current = lockedCaseId
  const putMessages = (next: ChatMsg[]) => {
    messagesRef.current = next
    setMessages(next)
  }
  // true once this session id has a conversation row, so an @ attach can PATCH
  const persistedRef = useRef(false)
  // The agent this conversation runs under: set only as a conversation starts, sent on every turn.
  const [agent, setAgent] = useState<ChatAgent | null>(null)
  const agentRef = useRef<ChatAgent | null>(null)
  const rememberAgent = (next: ChatAgent | null) => {
    agentRef.current = next
    setAgent(next)
  }
  // A stored conversation keeps only the id: its name is read back, and the handle stands in until then.
  const restoreAgent = (id: string | null | undefined) => {
    if (!id) return rememberAgent(null)
    rememberAgent({ id, name: prettyHandle(id) })
    agentsApi.getAgent(id).then((res) => {
      const name = (res.data as { name?: string } | undefined)?.name
      if (name && agentRef.current?.id === id) rememberAgent({ id, name })
    }).catch(() => { /* the handle stays */ })
  }

  // Drop every other user's copy as soon as this dock knows who is signed in.
  // A new id without an unmount (session replaced in place) must not keep the
  // previous transcript, or the next archive would store it under the new id.
  const seenUser = useRef<string | null | undefined>(undefined)
  useEffect(() => {
    const previous = seenUser.current
    seenUser.current = userId
    const switched = previous !== undefined && previous !== userId
    if (switched) {
      messagesRef.current = []
      setMessages([])
      sessionRef.current = newSessionId()
      currentKeyRef.current = null
      persistedRef.current = false
      rememberAgent(null)
      setHistoryOpen(false)
      dropFile()
    }
    if (!userId) {
      if (switched) setHistory([])
      return
    }
    retainChatCache(userId)
    setHistory(loadHistory<Conversation>(userId))
    if (switched) reloadHistory()
    // reloadHistory's identity is not a reason to rewrite the cache
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId])

  useEffect(() => {
    if (pinned) return
    if (open) {
      openerRef.current = document.activeElement as HTMLElement | null
      taRef.current?.focus()
    } else {
      openerRef.current?.focus?.()
      openerRef.current = null
    }
  }, [open, pinned])

  // a closed dock reopens on the conversation, not the history view
  useEffect(() => {
    if (!open) setHistoryOpen(false)
  }, [open])

  // a row click, rename or delete unmounts the focused control; keep focus in the dock
  useEffect(() => {
    const root = panelRef.current
    if (!open || pinned || !root || root.contains(document.activeElement)) return
    root.querySelector<HTMLElement>(historyOpen ? '.chist-search input' : 'textarea')?.focus()
  }, [historyOpen, open, pinned, renamingId, serverConvos, history])

  const full = size?.full ?? false
  const onFull = size?.onFull

  // Esc steps back one layer: rename field, mention list, history view, full
  // screen, then the dock.
  useEffect(() => {
    if (!open || pinned) return
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key !== 'Escape' || renamingId) return
      if (historyOpen) setHistoryOpen(false)
      else if (mentionHits) setMentionHits(null)
      else if (full) {
        // so a case drawer under the dock stays open
        e.preventDefault()
        onFull?.(false)
      }
      else onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, pinned, mentionHits, onClose, historyOpen, renamingId, full, onFull])

  const onPanelKeyDown = (e: KeyboardEvent<HTMLElement>) => {
    if (pinned || e.key !== 'Tab' || !open) return
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
    const el = bodyRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages, streamText, loading])

  useEffect(() => {
    const ta = taRef.current
    if (!ta) return
    ta.style.height = 'auto'
    ta.style.height = Math.min(ta.scrollHeight, 130) + 'px'
  }, [draft])

  const token = lockedCaseId ? null : mentionToken(draft)
  useEffect(() => {
    setMentionActive(0)
    if (token == null) {
      setMentionHits(null)
      setMentionFor(null)
      return
    }
    let live = true
    const timer = setTimeout(() => {
      // A bare @ offers the open cases, first those that need you; a token searches every case.
      const read = token.trim()
        ? api.get('/cases/search/full-text', { params: { query: token } })
        : casesApi.getAll({ limit: 5 })
      read
        .then((res) => {
          if (!live) return
          setMentionHits(caseHits(res.data))
          setMentionFor(token)
        })
        .catch(() => {
          if (!live) return
          setMentionHits([])
          setMentionFor(token)
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

  // The hunt's attachment reading: the types, size cap and condensing are the server's.
  const attachFile = async (picked: File) => {
    const seq = ++fileSeq.current
    const refusal = attachRefusal(picked)
    if (refusal) {
      setFile({ status: 'refused', name: picked.name, reason: refusal })
      return
    }
    setFile({ status: 'reading', name: picked.name })
    try {
      const { data } = await workflowApi.readHuntDocument(picked)
      if (fileSeq.current === seq) setFile({ status: 'ready', name: picked.name, text: data.text, pages: data.pages, condensed: data.condensed, named: false, file: picked, proposal: null })
    } catch (error) {
      if (fileSeq.current === seq) setFile({ status: 'refused', name: picked.name, reason: errorText(error, `${picked.name} could not be read`) })
    }
  }

  function dropFile() {
    fileSeq.current += 1
    setFile(null)
  }

  // only a dragged file is taken; dragged text still drops into the textarea
  const carriesFile = (event: DragEvent<HTMLDivElement>) => event.dataTransfer.types.includes('Files')

  const onDropFile = (event: DragEvent<HTMLDivElement>) => {
    if (!carriesFile(event)) return
    event.preventDefault()
    setDragging(false)
    const picked = event.dataTransfer.files?.[0]
    if (picked) void attachFile(picked)
  }

  const attachCase = (hit: CaseHit) => {
    applyCase(hit.id)
    setDraft((current) => current.replace(/(^|\s)@\S*$/, '$1').trim())
    setMentionHits(null)
  }

  // /hunt and /investigate run as the command bar runs them, with the dock's file as the attachment
  const typed = pinned ? null : parseLine(draft)
  const command = typed?.kind === 'command' && DOCK_COMMANDS.includes(typed.command.id) ? typed : null
  const attachment = attachmentOf(file)
  const binding = command ? bindLine(command, null, { openCaseId: caseId || null, jira: NO_JIRA, attachment }) : null

  // With no hypothesis typed, the coverage check proposes one from the file, as in the command bar.
  const needsProposal = command?.command.id === 'hunt' && !command.word && file?.status === 'ready' && !!file.file && file.proposal === null
  useEffect(() => {
    if (!needsProposal || file?.status !== 'ready') return
    const seq = fileSeq.current
    const settle = (proposal: HuntProposal) => {
      if (fileSeq.current === seq) setFile((now) => (now?.status === 'ready' ? { ...now, proposal } : now))
    }
    settle({ status: 'checking' })
    workflowApi
      .checkCoverage({ report: file.text })
      .then((res) => settle(proposalFrom(res.data)))
      .catch((error: unknown) => {
        const status = (error as { response?: { status?: number } })?.response?.status
        settle({
          status: 'none',
          reason: status === 400
            ? 'Nothing in this document to propose a hypothesis from. Type one to hunt with it.'
            : `A hypothesis could not be proposed: ${errorText(error, 'the coverage check failed')}`,
        })
      })
    // the file is read once, when the proposal is first asked for
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [needsProposal])

  const runCommand = async () => {
    if (!command || binding?.state !== 'bound' || starting || loading) return
    setStarting(true)
    const sentSeq = fileSeq.current
    const sentDraft = draft
    // the conversation on screen at Enter; switching away while the run starts leaves the new one alone
    const session = sessionRef.current
    let started: string | null = null
    try {
      await (command.command as LiveCommand).run(binding.target, {
        openCaseId: caseId || null,
        jira: NO_JIRA,
        attachment,
        notify,
        // the conversation follows the run's case once it has started; the drawer stays as it is
        onOpenCase: (id) => { started = id },
        onGo: (screen, options) => navigate(`/${screen}${options?.search ?? ''}`),
        ask: () => false,
        dropSent: () => { if (fileSeq.current === sentSeq) dropFile() },
      })
      setDraft((now) => (now === sentDraft ? '' : now))
      if (started && sessionRef.current === session) {
        applyCase(started)
        setRunSeq((n) => n + 1)
        // an ordinary turn on the case writes the conversation row, so a reload finds it and its run
        void send(startedText(command.command as LiveCommand), { bare: true })
      }
    } catch (error) {
      notify('err', errorText(error, 'Command failed'))
    } finally {
      setStarting(false)
    }
  }

  /** A run started from the command bar: the dock follows its case, on a conversation of its own. */
  const followCase = (id: string, text: string) => {
    setRunSeq((n) => n + 1)
    if (caseIdRef.current === id && messagesRef.current.length > 0) return
    archiveCurrent()
    sessionRef.current = newSessionId()
    currentKeyRef.current = null
    persistedRef.current = false
    rememberCase(id)
    rememberAgent(null)
    dropFile()
    void send(text, { fresh: true })
  }

  /** Opened from an agent: a new conversation under it, with a first question when one came along. */
  const startUnder = (under: ChatAgent, text: string, document?: SeedDocument) => {
    if (loading) return
    setHistoryOpen(false)
    archiveCurrent()
    putMessages([])
    sessionRef.current = newSessionId()
    currentKeyRef.current = null
    persistedRef.current = false
    rememberCase(null)
    rememberAgent(under)
    dropFile()
    if (text) void send(text, { fresh: true, document })
  }

  // `document` is a file the command bar already read: it rides on this turn, whatever the dock holds
  const send = async (override?: string, opts?: { fresh?: boolean; bare?: boolean; document?: SeedDocument }) => {
    const text = (override ?? draft).trim()
    if (!text) return
    // Hold the turn until the locked thread is the one on screen.
    if (lockedCaseId && !caseReady.current) {
      pendingSend.current = { text, document: opts?.document }
      setDraft('')
      return
    }
    // a fresh seed leaves the file behind, so a file still being read does not hold it
    if (loading || (starting && !opts?.bare) || (file?.status === 'reading' && !opts?.fresh && !opts?.document)) return
    setHistoryOpen(false)
    const gen = turnGen.current
    localTurn.current = true
    if (lockedCaseId) {
      caseIdRef.current = lockedCaseId
      setCaseId(lockedCaseId)
      setThreadOpen(true)
    }
    // `fresh` keeps a new investigation's seed off an unrelated conversation
    const base = opts?.fresh ? [] : messagesRef.current
    // the turn a file first rides on names it, so the question reads right in history
    const handed = opts?.document ? { status: 'ready' as const, ...opts.document, named: false, proposal: null } : null
    // `bare`: a turn that follows a run started here, whose file the run already took
    const sentFile = handed ?? (file?.status === 'ready' && !opts?.fresh && !opts?.bare ? file : null)
    const asked = sentFile && !sentFile.named ? `${text}\n\nAttached: ${sentFile.name}` : text
    if (handed) {
      // the handed file replaces the dock's; the case composer shows no chip, so it keeps none
      fileSeq.current += 1
      setFile(pinned ? null : { ...handed, named: true })
    } else if (sentFile && !sentFile.named) setFile({ ...sentFile, named: true })
    const next: ChatMsg[] = [...base, { role: 'user', text: asked }]
    putMessages(next)
    setDraft('')
    setLoading(true)
    setStreamText('')
    setStreamReasoning('')
    setIsProcessingTools(false)

    const ac = new AbortController()
    abortRef.current = ac
    // The row is inserted when the stream ends. An @ change before that is held
    // and patched afterwards; patching at HTTP 200 races a row that is not there.
    const sentCase = caseIdRef.current
    let accepted = false
    // false until the API itself answered: only then is "is the backend running?" a fair question
    let reached = false
    try {
      const payload: {
        messages: { role: string; content: string }[]
        session_id: string
        page_context?: string
        page_snapshot?: string
        case_id?: string
        agent_id?: string
        document?: { name: string; text: string }
      } = {
        messages: askedOf(next).map((m) => ({ role: m.role === 'vigil' ? 'assistant' : 'user', content: m.text })),
        session_id: sessionRef.current,
      }
      if (pageKey) payload.page_context = pageKey
      const snapshot = getPageSnapshot(pageKey)
      if (snapshot) payload.page_snapshot = snapshot
      if (sentCase !== null) payload.case_id = sentCase
      if (agentRef.current) payload.agent_id = agentRef.current.id
      if (sentFile) payload.document = { name: sentFile.name, text: sentFile.text }
      const res = await streamFetch('/claude/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
        body: JSON.stringify(payload),
        signal: ac.signal,
      })
      if (!res.ok) {
        const refusal = await refusalOf(res)
        if (refusal === null && (res.status === 502 || res.status === 503)) throw new Error(`HTTP ${res.status}`)
        throw new ServerRefusal(refusal ?? `Vigil refused the request (HTTP ${res.status})`)
      }
      reached = true
      accepted = true
      const reader = res.body?.getReader()
      const decoder = new TextDecoder()
      let curText = ''
      let curReasoning = ''
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
            }
            try {
              ev = JSON.parse(data)
            } catch {
              continue
            }
            if (ev.error) throw new ServerRefusal(ev.error)
            if (ev.type === 'tool_processing') {
              // separate tool output from the prose preceding it
              setIsProcessingTools(true)
              if (curText && !curText.endsWith('\n\n')) curText += '\n\n'
            } else if (ev.type === 'text') {
              setIsProcessingTools(false)
              curText += ev.content || ''
              setStreamText(curText)
            } else if (ev.type === 'reasoning') {
              curReasoning += ev.content || ''
              setStreamReasoning(curReasoning)
            }
          }
        }
      }
      if (gen === turnGen.current) {
        setMessages((m) => {
          const vigil: ChatMsg = { role: 'vigil', text: curText || '_(no response)_', ...(curReasoning && { reasoning: curReasoning }) }
          const next = [...m, vigil]
          messagesRef.current = next
          return next
        })
      }
      // gated inside notificationService by the setting + browser permission
      if (currentKeyRef.current && curText) {
        const summary = curText.replace(/[#*`_>[\]]/g, '').replace(/\s+/g, ' ').trim().slice(0, 140)
        notificationService.notifyInvestigationComplete({
          title: 'Vigil',
          summary: summary || 'Analysis complete',
        })
      }
    } catch (e) {
      const err = e as { name?: string; message?: string }
      if (err?.name !== 'AbortError' && gen === turnGen.current) {
        // A reason the server sent is shown as is; only a failure to reach it blames the backend.
        const why = err?.message || String(e)
        const text = reached || e instanceof ServerRefusal ? why : `Could not reach Vigil: ${why}. Is the backend running?`
        setMessages((m) => {
          const next: ChatMsg[] = [...m, { role: 'error', text }]
          messagesRef.current = next
          return next
        })
      }
    } finally {
      if (gen === turnGen.current) {
        if (accepted) {
          const latest = caseIdRef.current
          persistedRef.current = true
          if (latest !== sentCase && latest !== null) {
            conversationsApi.update(sessionRef.current, { case_id: latest }).catch(() => {})
          }
        }
        setLoading(false)
        setStreamText('')
        setStreamReasoning('')
        setIsProcessingTools(false)
        abortRef.current = null
        if (accepted) onTurnDone?.()
      }
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
      // the cache keeps what the server keeps: no reasoning
      messages: messages.map(({ role, text }) => ({ role, text })),
      key: currentKeyRef.current || undefined,
      agent: agentRef.current ?? undefined,
    }
    setHistory((h) => {
      const next = [convo, ...h.filter((c) => c.id !== convo.id)].slice(0, CHAT_HISTORY_MAX)
      saveHistory(userId, next)
      return next
    })
  }

  // the server already persisted every turn, so this only resets local state
  const reset = () => {
    if (loading) return
    archiveCurrent()
    putMessages([])
    sessionRef.current = newSessionId()
    currentKeyRef.current = null
    persistedRef.current = false
    rememberCase(null)
    rememberAgent(null)
    dropFile()
    reloadHistory()
  }

  // continues the same session_id, so new turns append to it
  const openConversation = async (id: string, key?: string | null, force = false): Promise<boolean> => {
    if (loading && !force) return false
    const gen = loadGen.current
    archiveCurrent()
    setHistoryOpen(false)
    try {
      const res = await conversationsApi.get(id)
      // A turn started on this locked session while the row was loading.
      if (gen !== loadGen.current || (lockedCaseId && localTurn.current)) return false
      const detail = res.data as ConversationDetail
      const msgs = toChatMsgs(detail.messages || [])
      putMessages(msgs)
      if (lockedCaseId) setThreadOpen(msgs.length > 0)
      sessionRef.current = id
      currentKeyRef.current = key ?? null
      persistedRef.current = true
      rememberCase(lockedCaseId ?? detail.case_id ?? null)
      restoreAgent(detail.agent_id)
      dropFile()
      return true
    } catch (error) {
      if (gen !== loadGen.current || (lockedCaseId && localTurn.current)) return false
      // 401/403/404 is the server refusing this id. Only a dead backend may use the cache.
      if (!serverUnreachable(error)) return false
      const cached = loadHistory<Conversation>(userId).find((c) => c.id === id)
      if (cached) {
        putMessages(cached.messages)
        if (lockedCaseId) setThreadOpen(cached.messages.length > 0)
        sessionRef.current = id
        currentKeyRef.current = cached.key || key || null
        persistedRef.current = false
        rememberCase(lockedCaseId ?? null)
        rememberAgent(cached.agent ?? null)
        dropFile()
        return true
      }
      return false
    }
  }

  // A row clicked while Vigil is answering stops that answer, as Stop does (the server keeps what
  // it said), then opens. The answer's own conversation goes back to it instead.
  const openFromHistory = (id: string) => {
    if (loading && id === sessionRef.current) {
      setHistoryOpen(false)
      return
    }
    if (loading) {
      turnGen.current += 1
      abortRef.current?.abort()
      abortRef.current = null
      setLoading(false)
      setStreamText('')
      setIsProcessingTools(false)
    }
    void openConversation(id, null, true)
  }

  // the seed prompt is deterministic per finding/case, so it doubles as the
  // dedup key for reusing an existing thread
  const openInvestigation = async (prompt: string, document?: SeedDocument) => {
    setHistoryOpen(false)
    if (loading) return
    // a question with a file is a new turn, so it never just reopens the old thread
    if (!document && currentKeyRef.current === prompt && messages.length > 0) return // already here
    const mapped = document ? undefined : loadKeymap(userId)[prompt]
    if (mapped) {
      const opened = await openConversation(mapped, prompt)
      if (opened) return // reopened the existing thread for this finding/case
    }
    archiveCurrent()
    sessionRef.current = newSessionId()
    currentKeyRef.current = prompt
    persistedRef.current = false
    rememberCase(null)
    rememberAgent(null)
    dropFile()
    setKeymapEntry(userId, prompt, sessionRef.current) // so re-opening this finding restores it
    send(prompt, { fresh: true, document })
  }

  const deleteConversation = async (id: string) => {
    let removed = false
    try {
      await conversationsApi.delete(id)
      removed = true
      reloadHistory()
    } catch (err) {
      // 404: another tab already deleted it, so the row is gone all the same.
      if ((err as { response?: { status?: number } })?.response?.status === 404) removed = true
      /* still drop it from the offline cache below */
    }
    setHistory((h) => {
      const next = h.filter((c) => c.id !== id)
      saveHistory(userId, next)
      return next
    })
    // A failed delete leaves the open session. Deleting some other row does
    // too. archiveCurrent would write this thread back into the local history.
    if (removed && id === sessionRef.current) {
      putMessages([])
      sessionRef.current = newSessionId()
      currentKeyRef.current = null
      persistedRef.current = false
      if (!lockedCaseId) rememberCase(null)
      rememberAgent(null)
      dropFile()
    }
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

  // going back to a tab folds the thread to its last exchange; a case switch resets that below
  const seenKey = useRef(collapseKey)
  useEffect(() => {
    if (seenKey.current === collapseKey) return
    seenKey.current = collapseKey
    setEarlierHidden(true)
  }, [collapseKey])

  useEffect(() => {
    if (!lockedCaseId) return
    const gen = ++loadGen.current
    turnGen.current += 1
    localTurn.current = false
    caseReady.current = false
    pendingSend.current = null
    abortRef.current?.abort()
    abortRef.current = null
    setLoading(false)
    setStreamText('')
    setDraft('')
    putMessages([])
    setThreadOpen(false)
    setEarlierHidden(false)
    currentKeyRef.current = null
    persistedRef.current = false
    rememberCase(lockedCaseId)
    rememberAgent(null)
    let live = true
    const settle = () => {
      if (!live || gen !== loadGen.current) return
      caseReady.current = true
      const queued = pendingSend.current
      pendingSend.current = null
      if (queued) void send(queued.text, { document: queued.document })
    }
    conversationsApi
      .list({ case_id: lockedCaseId, limit: 1 })
      .then(async (res) => {
        if (!live || gen !== loadGen.current) return
        const match = (res.data?.conversations as Array<{ id: string }> | undefined)?.[0]
        if (match) {
          await openConversation(match.id, null, true)
          if (!live || gen !== loadGen.current) return
          rememberCase(lockedCaseId)
        } else {
          sessionRef.current = newSessionId()
          persistedRef.current = false
        }
        settle()
      })
      .catch(() => {
        if (!live || gen !== loadGen.current) return
        sessionRef.current = newSessionId()
        persistedRef.current = false
        settle()
      })
    return () => {
      live = false
      loadGen.current += 1
      turnGen.current += 1
    }
    // openConversation closes over the render that starts the load
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lockedCaseId])

  const seedRef = useRef<ChatSeed | null>(null)
  useEffect(() => {
    // reset when the parent clears the seed, so the same finding can be
    // investigated again
    if (!seed) {
      seedRef.current = null
      return
    }
    // guard against StrictMode's double-invoke firing the same seed twice.
    // While a stream is in flight, leave the seed set so it sends when loading drops.
    if (!open || seed === seedRef.current) return
    if (loading && (!lockedCaseId || caseReady.current)) return
    seedRef.current = seed
    if (seed.caseId && !pinned) {
      followCase(seed.caseId, seed.text)
    } else if (seed.agent && !pinned) {
      startUnder(seed.agent, seed.text, seed.document)
    } else if (!seed.text) {
      // a file with no question waits on the dock's chip for one
      if (seed.document && !pinned) {
        fileSeq.current += 1
        setFile({ status: 'ready', ...seed.document, named: false, proposal: null })
      }
    } else if (lockedCaseId) {
      // append on this session; do not open a keymap investigation or clear the case
      caseIdRef.current = lockedCaseId
      setCaseId(lockedCaseId)
      send(seed.text, { document: seed.document })
    } else {
      openInvestigation(seed.text, seed.document)
    }
    onSeedConsumed?.()
    // send intentionally omitted: we fire once per new seed, and again when a
    // stream that blocked it finishes
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, seed, lockedCaseId, loading])
  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // An open mention list takes the arrows, and Enter or Tab attaches the highlighted case, as in the command bar.
    // Only a list that answers what is typed: until then the keys wait rather than pick an older hit or send.
    const hits = token != null && mentionFor === token ? mentionHits : null
    const pick = (e.key === 'Enter' && !e.shiftKey) || e.key === 'Tab'
    if (token != null && mentionFor !== token && pick) {
      e.preventDefault()
      return
    }
    if (hits && hits.length > 0) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault()
        const step = e.key === 'ArrowDown' ? 1 : -1
        setMentionActive((i) => (i + step + hits.length) % hits.length)
        return
      }
      if (pick) {
        e.preventDefault()
        attachCase(hits[Math.min(mentionActive, hits.length - 1)])
        return
      }
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      if (command) void runCommand()
      else send()
    }
  }

  // the last question and what followed it
  const lastAsk = messages.map((m) => m.role).lastIndexOf('user')
  const firstShown = pinned && earlierHidden && lastAsk > 0 ? lastAsk : 0
  const [prevMsg, lastMsg] = messages.slice(-2)
  const retryOf = !loading && lastMsg?.role === 'error' && prevMsg?.role === 'user' ? prevMsg.text : undefined

  const transcript = (
    <div className="chat-body" ref={bodyRef} style={pinned && thread.height !== null ? { height: thread.height, maxHeight: 'none' } : undefined}>
      {firstShown > 0 && (
        <button type="button" className="composer-earlier" onClick={() => setEarlierHidden(false)}>
          Show {firstShown} earlier message{firstShown === 1 ? '' : 's'}
        </button>
      )}
      {messages.length === 0 && !loading && (
        pinned ? (
          <div className="chat-empty">Ask Vigil to investigate a finding, correlate activity, or summarize a case.</div>
        ) : (
          <div className="chat-empty dock">
            <div className="ce-head">
              <VigilMark className="ce-mark" />
              <span>Ask about what you are looking at</span>
            </div>
            <p>Ask about this page or any case. Conversations are private to you and kept in your history.</p>
          </div>
        )
      )}
      {messages.map((m, i) => i < firstShown ? null :
        m.role === 'user' ? (
          <div className="msg user" key={i}><div className="body">{m.text}</div></div>
        ) : m.role === 'error' ? (
          <div className="msg vigil err" key={i} role="alert">
            <div className="body">{m.text}</div>
            {retryOf && i === messages.length - 1 && (
              <button type="button" className="btn ghost msg-retry" onClick={() => send(retryOf)}>Retry</button>
            )}
          </div>
        ) : (
          <VigilMessage key={i} text={m.text} reasoning={m.reasoning} citations={citations} onCite={onCite} pinned={pinned} />
        )
      )}
      {/* read only while the dock is open: it polls the case */}
      {!pinned && !lockedCaseId && open && caseId && <CaseRunBlock caseId={caseId} refresh={runSeq} />}
      {loading && (
        <div className="msg vigil">
          {!pinned && <span className="msg-av"><VigilMark /></span>}
          <div className="msg-main">
            {/* always-on processing indicator so the user knows Vigil is still
                working — the phase label tracks reasoning → responding */}
            <div className="vigil-status" aria-live="polite">
              {pinned ? (
                <span className="vs-dots" aria-hidden="true"><i /><i /><i /></span>
              ) : (
                <Icon name="sparkle" size={14} className="vs-spark" />
              )}
              <span className="vs-label">
                {isProcessingTools ? 'Vigil is running tools' : streamText ? 'Vigil is responding' : streamReasoning ? 'Vigil is thinking' : 'Vigil is working on it'}
                …
              </span>
            </div>
            {streamReasoning && <Thinking text={streamReasoning} live />}
            {streamText && <AnswerText text={streamText} citations={citations} onCite={onCite} />}
          </div>
        </div>
      )}
    </div>
  )

  const mentionOpen = !lockedCaseId && mentionHits
  const addAt = () => {
    setDraft((d) => (d && !/\s$/.test(d) ? `${d} @` : `${d}@`))
    taRef.current?.focus()
  }

  const foot = (
    <div className="chat-foot">
      <div
        className={`chat-input${dragging ? ' dragging' : ''}`}
        onDragOver={pinned ? undefined : (event) => {
          if (!carriesFile(event)) return
          event.preventDefault()
          setDragging(true)
        }}
        onDragLeave={pinned ? undefined : () => setDragging(false)}
        onDrop={pinned ? undefined : onDropFile}
      >
        {mentionOpen && (
          <div className="chat-mention" role="listbox" aria-label="Matching cases">
            <span className="cm-head">Mention</span>
            {mentionHits.length === 0 ? (
              <div className="cm-empty">{!token?.trim() ? 'Type a case id or title' : mentionFor === token ? 'No matching cases' : 'Searching…'}</div>
            ) : (
              mentionHits.map((hit, i) => (
                <button
                  key={hit.id}
                  type="button"
                  role="option"
                  aria-selected={i === mentionActive}
                  className={i === mentionActive ? 'on' : undefined}
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => attachCase(hit)}
                >
                  <span className="cm-tile"><Icon name="cases" size={14} /></span>
                  <span className="cm-text">
                    <span className="cm-title">{hit.title}</span>
                    <span className="cm-id">{hit.id}</span>
                  </span>
                </button>
              ))
            )}
          </div>
        )}
        {!pinned && caseId ? (
          <span className="chat-case" data-testid="attached-case">
            <span>@{caseId}</span>
            <button type="button" aria-label="Remove attached case" onClick={() => applyCase('')}>
              <Icon name="x2" size={12} />
            </button>
          </span>
        ) : null}
        {!pinned && file ? (
          <span className="chat-case chat-file" data-testid="attached-file" title={file.status === 'refused' ? file.reason : file.name} role={file.status === 'refused' ? 'alert' : undefined}>
            <span>
              {file.name}
              {file.status === 'reading' && ' · reading…'}
              {file.status === 'ready' && ` · ${file.pages} page${file.pages === 1 ? '' : 's'}${file.condensed ? ' · condensed' : ''}`}
              {file.status === 'refused' && ` · ${file.reason}`}
            </span>
            <button type="button" aria-label={`Remove ${file.name}`} onClick={dropFile}>
              <Icon name="x2" size={12} />
            </button>
          </span>
        ) : null}
        <textarea
          ref={taRef}
          rows={1}
          placeholder={lockedCaseId ? 'Ask about this case' : 'Ask Vigil · @ to attach a case'}
          aria-label={lockedCaseId ? 'Ask about this case' : 'Ask Vigil'}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          onPaste={pinned ? undefined : (e) => {
            // Office and browsers put a picture beside copied text; text pasted is text
            const pasted = e.clipboardData.files?.[0]
            if (!pasted || e.clipboardData.types.includes('text/plain')) return
            e.preventDefault()
            void attachFile(pasted)
          }}
        />
        <div className="ci-row">
          {/* the case composer shows no chip; the dock's chip sits above the textarea */}
          {pinned ? null : (
            <button type="button" className="ci-at" aria-label="Mention a case" title="Mention a case" onClick={addAt}>@</button>
          )}
          <div className="ci-grow" />
          {pinned && <span className="composer-note">Private to you · Ask only</span>}
          {loading ? (
            <button className="ci-send busy" title="Stop" onClick={stop}>
              {pinned ? <Icon name="x2" size={15} /> : <span className="ci-stop" />}
            </button>
          ) : (
            <button
              className="ci-send" title={command ? 'Run' : 'Send'}
              onClick={() => (command ? void runCommand() : send())}
              disabled={starting || (command ? binding?.state !== 'bound' : !draft.trim() || file?.status === 'reading')}
            ><Icon name="send" /></button>
          )}
        </div>
      </div>
      {!pinned && (binding ? (
        <div className={`chat-hint${binding.state === 'bound' ? ' is-command' : ''}`} role="status">
          {starting ? 'Starting…' : binding.state === 'bound' ? `${binding.line}${binding.note ? ` · ${binding.note}` : ''} · Enter runs` : binding.reason}
        </div>
      ) : (
        <div className="chat-hint">Saved privately to your history · @ attaches · drop a file · /hunt or /investigate starts a run · Enter sends</div>
      ))}
    </div>
  )

  // history view rows: the server list, or this user's cache when the server can't be reached
  const offline = histPhase === 'error' && historyOffline
  const histRows: HistRow[] = offline
    ? history.map((c) => ({
        id: c.id,
        title: c.title || 'Untitled conversation',
        count: c.messages?.length || 0,
        ts: c.ts || null,
        context: 'General',
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
        context: histContext(c.case_id, c.page_context),
      }))
  const histGroups: Array<{ label: string; rows: HistRow[] }> = []
  for (const r of histRows) {
    const label = dayLabel(r.ts)
    const last = histGroups[histGroups.length - 1]
    if (last?.label === label) last.rows.push(r)
    else histGroups.push({ label, rows: [r] })
  }
  const convoTitle =
    histRows.find((r) => r.id === sessionRef.current)?.title ||
    messages.find((m) => m.role === 'user')?.text.replace(/\s+/g, ' ').trim() ||
    'New conversation'

  const historyView = (
    <>
      <div className="chist-top">
        <p className="chist-privacy">Private to you in this workspace. Saved on the server.</p>
        <label className="chist-search">
          <Icon name="search" size={14} />
          <input
            aria-label="Search conversations"
            placeholder="Search conversations…"
            value={histQuery}
            disabled={offline}
            autoFocus
            onChange={(e) => setHistQuery(e.target.value)}
          />
        </label>
        <label className="chist-archtoggle">
          <input
            type="checkbox"
            checked={showArchived}
            onChange={(e) => setShowArchived(e.target.checked)}
            disabled={offline}
          />
          Show archived
        </label>
        {offline && <span className="chist-note">Offline — showing cached conversations.</span>}
      </div>
      <div className="chist-list">
        {histPhase === 'loading' ? (
          <div className="chist-empty">Loading…</div>
        ) : histRows.length === 0 ? (
          <div className="chist-empty">
            {histQuery.trim()
              ? 'No conversations match.'
              : 'No past conversations yet. Your chats are saved automatically so you can reopen them on any device.'}
          </div>
        ) : (
          histGroups.map((g) => (
            <div key={g.label} className="chist-group">
              <div className="chist-day">{g.label}</div>
              {g.rows.map((c) => (
                <div
                  key={c.id}
                  className={`chist-row${c.id === sessionRef.current ? ' current' : ''}${c.archived ? ' archived' : ''}${renamingId === c.id ? ' renaming' : ''}`}
                >
                  {renamingId === c.id ? (
                    <input
                      className="chist-rename"
                      aria-label="Rename conversation"
                      autoFocus
                      value={renameDraft}
                      onChange={(e) => setRenameDraft(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') {
                          e.preventDefault()
                          commitRename(c.id)
                        } else if (e.key === 'Escape') {
                          // only cancels the rename, not the history view
                          e.stopPropagation()
                          setRenamingId(null)
                        }
                      }}
                      onBlur={() => commitRename(c.id)}
                    />
                  ) : (
                    <button className="chist-main" onClick={() => openFromHistory(c.id)}>
                      <span className="chist-title">{c.title}</span>
                      <span className="chist-meta">
                        <span className="chist-ctx">{c.context}</span>
                        {c.ts != null && histTime(c.ts) && <span>{histTime(c.ts)}</span>}
                        <span>{c.count} message{c.count === 1 ? '' : 's'}</span>
                      </span>
                    </button>
                  )}
                  <div className="chist-actions">
                    <button
                      className="chist-act"
                      title="Rename"
                      aria-label="Rename"
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
                      aria-label={c.archived ? 'Unarchive' : 'Archive'}
                      disabled={offline}
                      onClick={() => archiveConversation(c.id, !c.archived)}
                    >
                      <Icon name="folder" size={14} />
                    </button>
                    <button
                      className="chist-act chist-del"
                      title="Delete"
                      aria-label="Delete"
                      onClick={() => deleteConversation(c.id)}
                    >
                      <Icon name="trash" size={14} />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          ))
        )}
      </div>
    </>
  )

  return (
    <>
    {pinned ? (
      <section className={`case-composer${threadOpen ? ' thread-open' : ''}`} aria-label="Ask Vigil">
        {threadOpen && <ThreadGrip bodyRef={bodyRef} {...thread} />}
        {messages.length > 0 && (
          <button type="button" className="composer-fold" aria-expanded={threadOpen} onClick={() => setThreadOpen((openThread) => !openThread)}>
            <span className="composer-chev" aria-hidden="true">▾</span>
            {messages.length} message{messages.length === 1 ? '' : 's'} · {threadOpen ? 'hide' : 'show'}
          </button>
        )}
        {threadOpen && transcript}
        <div className="composer-box">
          <div className="composer-modes" role="group" aria-label="Composer mode">
            <button type="button" className="on" aria-pressed="true">Ask</button>
            <button type="button" disabled title={LATER_RELEASE}>Tell</button>
            <button type="button" disabled title={LATER_RELEASE}>Do</button>
          </div>
          {foot}
        </div>
      </section>
    ) : (
    <aside
      ref={panelRef}
      className={`chat${open ? ' open' : ''}${full ? ' full' : ''}`}
      role="dialog"
      aria-label="Ask Vigil"
      aria-hidden={!open}
      onKeyDown={onPanelKeyDown}
    >
      {size?.resizable && open && (
        <DockGrip width={size.width} min={size.min} max={size.max} preview={size.preview} commit={size.commit} />
      )}
      <div className="chat-head">
        <span className="ch-ico"><VigilMark /></span>
        <h3 className="ch-title">Ask Vigil</h3>
        {agent && <span className="ch-agent" title={`Runs under the ${agent.name} agent`}>{agent.name}</span>}
        <span className="ch-convo" title={convoTitle}>{convoTitle}</span>
        <div className="hbtns">
          <button
            type="button"
            title="Conversation history"
            aria-label="Conversation history"
            aria-pressed={historyOpen}
            onClick={() => {
              if (!historyOpen) reloadHistory()
              setHistoryOpen((v) => !v)
            }}
          >
            <Icon name="clock" />
          </button>
          <button
            type="button"
            title="New conversation"
            aria-label="New conversation"
            disabled={loading}
            onClick={() => {
              reset()
              setHistoryOpen(false)
            }}
          >
            <Icon name="plus" />
          </button>
          {size && (
            <button
              type="button"
              title={full ? 'Back to side panel' : 'Full screen'}
              aria-label={full ? 'Back to side panel' : 'Full screen'}
              onClick={() => size.onFull(!full)}
            >
              <Icon name={full ? 'shrink' : 'fit'} />
            </button>
          )}
          <button type="button" title="Close Ask Vigil" aria-label="Close Ask Vigil" onClick={onClose}><Icon name="close" /></button>
        </div>
      </div>
      {historyOpen ? (
        historyView
      ) : (
        <>
          {pageTitle ? (
            <div className="chat-ctx">
              <span className="cx-label">Using this page</span>
              <span className="cx-pill"><Icon name="target" size={12} /><span>{pageTitle}</span></span>
            </div>
          ) : null}
          {transcript}
          {foot}
        </>
      )}
    </aside>
    )}
    </>
  )
}
