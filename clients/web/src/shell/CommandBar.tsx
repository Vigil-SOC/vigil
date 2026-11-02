import { useCallback, useEffect, useMemo, useRef, useState, type ClipboardEvent, type DragEvent, type KeyboardEvent, type ReactNode } from 'react'
import { useAuth } from '../contexts/AuthContext'
import { casesApi, findingsApi, searchApi, workflowApi } from '../services/api'
import { Icon } from '../shared/icons'
import { useToast } from './toast'
import type { ConsoleScreenGoOptions, SeedDocument } from '../shared/types'
import {
  argumentHits,
  argumentKind,
  argumentRows,
  attachBlockedBy,
  ATTACH_TYPES,
  attachRefusal,
  buildRows,
  COMMANDS,
  commandPreview,
  commandRemainder,
  firstEnabled,
  huntHypothesis,
  huntTitle,
  isLiveCommand,
  liveCommandIn,
  moveEnabled,
  PASTE_ATTACH_CHARS,
  PASTED_NAME,
  proposalFrom,
  runningHunt,
  type RunningHunt,
  readRecents,
  removeRecent,
  tagText,
  writeRecent,
  type BoardLink,
  type Hit,
  type HuntAttachment,
  type LiveCommandId,
  type PaletteRow,
  type SearchHits,
  useJiraReadiness,
} from './commandBarModel'
import { errorText } from '../shared/errorText'

// A disabled button gets no hover, so the tooltip sits on a wrapper.
function LaterTip({ later, children }: { later: boolean; children: ReactNode }) {
  return later ? <span className="vg-command-later" title="Coming in a later release">{children}</span> : <>{children}</>
}

export default function CommandBar({
  boards,
  onOpenChat,
  onOpenCase,
  onGo,
  caseOpen = false,
  fill,
  placement = 'header',
}: {
  boards: BoardLink[]
  onOpenChat: (prompt?: string, document?: SeedDocument) => void
  onOpenCase: (caseId: string) => void
  onGo: (screen: string, options?: ConsoleScreenGoOptions) => void
  /** A case is open, so asking goes to its composer instead of the dock. */
  caseOpen?: boolean
  /** A new `seq` puts `text` in the bar and opens it. Nothing runs; the person still presses Enter or Run. */
  fill?: { text: string; seq: number } | null
  /** Home draws the bar full width under the headline. Everywhere else it stays in the header. */
  placement?: 'header' | 'hero'
}) {
  const { user } = useAuth()
  const { notify } = useToast()
  const userId = user?.user_id
  const slotRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(0)
  const [recents, setRecents] = useState<string[]>([])
  const [hits, setHits] = useState<SearchHits | null>(null)
  const [args, setArgs] = useState<{ kind: 'case' | 'alert'; rows: PaletteRow[] } | null>(null)
  const jira = useJiraReadiness()
  const [preview, setPreview] = useState<{ id: LiveCommandId; arg: string } | null>(null)
  const [running, setRunning] = useState(false)
  const [attachment, setAttachment] = useState<HuntAttachment | null>(null)
  const [dragging, setDragging] = useState(false)
  const attachSeq = useRef(0)
  const fileRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (userId) setRecents(readRecents(userId))
  }, [userId])

  useEffect(() => {
    const onKey = (event: globalThis.KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        setOpen(true)
        inputRef.current?.focus()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const seenFill = useRef(fill?.seq)
  useEffect(() => {
    if (!fill || fill.seq === seenFill.current) return // a remount must not replay an old fill
    seenFill.current = fill.seq
    setQuery(fill.text)
    setHits(null)
    setActive(0)
    setPreview(liveCommandIn(fill.text))
    setOpen(true)
    inputRef.current?.focus()
  }, [fill?.seq]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!open) return
    const onDoc = (event: MouseEvent) => {
      if (!slotRef.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])

  useEffect(() => {
    const q = query.trim()
    if (!open || !q || q.startsWith('/')) {
      setHits(null)
      return
    }
    let live = true
    const round = new AbortController()
    const timer = setTimeout(() => {
      void searchApi.query(q, round.signal).then((res) => {
        if (!live) return
        setHits(res.data)
        setActive(0)
      }).catch(() => {})
    }, 150)
    return () => {
      live = false
      round.abort()
      clearTimeout(timer)
    }
  }, [open, query])

  // The cases or alerts a command's argument can name, so nobody has to know the id.
  const argKind = open && preview ? argumentKind(preview.id) : null
  const argText = preview?.arg.trim() ?? ''
  useEffect(() => {
    if (!argKind) {
      setArgs(null)
      return
    }
    let live = true
    const round = new AbortController()
    const timer = setTimeout(() => {
      // Nothing typed yet: the first few the Cases list or the alert feed shows.
      const found: Promise<Hit[]> = argText
        ? searchApi.query(argText, round.signal).then((res) => argumentHits(argKind, res.data))
        : argKind === 'case'
          ? casesApi.getAll({ limit: 5 }).then((res) =>
              (res.data.cases ?? []).map((c) => ({ id: c.case_id, title: c.title, priority: c.priority })),
            )
          : findingsApi.getAll({ limit: 5 }, round.signal).then((res) =>
              ((res.data as { findings?: { finding_id: string; title?: string | null; description?: string | null; data_source?: string | null; severity?: string | null }[] }).findings ?? [])
                .map((f) => ({ id: f.finding_id, title: f.title || f.description || f.finding_id, data_source: f.data_source, severity: f.severity })),
            )
      found.then((list) => {
        if (live) setArgs({ kind: argKind, rows: argumentRows(argKind, list) })
      }).catch(() => {
        if (live) setArgs(null)
      })
    }, 150)
    return () => {
      live = false
      round.abort()
      clearTimeout(timer)
    }
  }, [argKind, argText])

  // Rows from another command's kind are never offered: a case id must not land after /investigate.
  const argRows = args && args.kind === argKind ? args.rows : null
  const rows = useMemo(() => [...buildRows(query, hits, recents, boards), ...(argRows ?? [])], [query, hits, recents, boards, argRows])
  const highlighted = rows[active] && !rows[active].disabled ? active : firstEnabled(rows)
  const previewView = preview ? commandPreview(preview.id, preview.arg, jira, attachment) : null
  const canRun = !!previewView && !previewView.disabled && !running
  // Enter on an argument row the person moved to fills it in; the next Enter runs.
  const picking = rows[highlighted]?.fill !== undefined
  const alreadyRunning = preview?.id === 'hunt' && !preview.arg.trim() ? runningHunt(attachment) : null

  const highlightedKey = open ? rows[highlighted]?.key : undefined
  useEffect(() => {
    if (highlightedKey) document.getElementById(highlightedKey)?.scrollIntoView?.({ block: 'nearest' })
  }, [highlightedKey])

  // A command row click hands focus back to the input, caret at the end, once the new query has rendered.
  const [refocus, setRefocus] = useState(0)
  useEffect(() => {
    const input = inputRef.current
    if (!refocus || !input) return
    input.focus()
    input.setSelectionRange(input.value.length, input.value.length)
  }, [refocus])

  // With no hypothesis typed, the coverage check proposes one from the document.
  const huntNoArg = preview?.id === 'hunt' && !preview.arg.trim()
  const needsProposal = attachment?.status === 'ready' && attachment.proposal === null
  useEffect(() => {
    if (!huntNoArg || !needsProposal) return
    const seq = attachSeq.current
    const ready = attachment
    if (ready?.status !== 'ready') return
    setAttachment({ ...ready, proposal: { status: 'checking' } })
    const settle = (proposal: Extract<HuntAttachment, { status: 'ready' }>['proposal']) => {
      if (attachSeq.current === seq) setAttachment((now) => (now?.status === 'ready' ? { ...now, proposal } : now))
    }
    workflowApi
      .checkCoverage({ report: ready.text })
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
    // `attachment` is read once, when the proposal is first asked for.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [huntNoArg, needsProposal])

  const attach = useCallback(async (file: File, pasted = false) => {
    const name = pasted ? PASTED_NAME : file.name
    const seq = ++attachSeq.current
    const refusal = attachRefusal(file)
    if (refusal) {
      setAttachment({ status: 'refused', name, reason: refusal })
      return
    }
    setAttachment({ status: 'reading', name })
    try {
      const { data } = await workflowApi.readHuntDocument(file)
      if (attachSeq.current !== seq) return
      setAttachment({ status: 'ready', name, file, pasted, pages: data.pages, condensed: data.condensed, text: data.text, proposal: null })
    } catch (error) {
      if (attachSeq.current === seq) setAttachment({ status: 'refused', name, reason: errorText(error, `${name} could not be read`) })
    }
  }, [])

  const removeAttachment = useCallback(() => {
    attachSeq.current += 1
    setAttachment(null)
  }, [])

  // The typed command takes a case, or is not here yet: no file for it.
  const attachBlocked = attachBlockedBy(query)

  // Only a dragged file is taken, anywhere on the bar or its menu.
  const carriesFile = (event: DragEvent<HTMLDivElement>) => !attachBlocked && event.dataTransfer.types.includes('Files')
  const onDropFile = (event: DragEvent<HTMLDivElement>) => {
    if (!carriesFile(event)) return
    event.preventDefault()
    setDragging(false)
    const file = event.dataTransfer.files?.[0]
    if (file) void attach(file)
  }

  // A pasted file attaches, as in the chat dock. Long or multi-line text attaches as text; a short line pastes.
  const onPaste = (event: ClipboardEvent<HTMLInputElement>) => {
    if (attachBlocked) return
    const text = event.clipboardData.getData('text/plain')
    const file = event.clipboardData.files?.[0]
    if (file && !event.clipboardData.types.includes('text/plain')) {
      event.preventDefault()
      void attach(file)
    } else if (text.trim() && (text.length > PASTE_ATTACH_CHARS || /\r|\n/.test(text.trim()))) {
      event.preventDefault()
      void attach(new File([text], `${PASTED_NAME}.txt`, { type: 'text/plain' }), true)
    }
  }

  // Ask Vigil gets the read file with the question; one still being read, or refused, holds it back.
  const ask = useCallback((text?: string): boolean => {
    if (attachment?.status === 'reading') {
      notify('info', `Still reading ${attachment.name}`)
      return false
    }
    if (attachment?.status === 'refused') {
      notify('err', `${attachment.reason} Remove ${attachment.name} to ask without it.`)
      return false
    }
    const ready = attachment?.status === 'ready' ? attachment : null
    onOpenChat(text, ready ? { name: ready.name, text: ready.text, pages: ready.pages, condensed: ready.condensed } : undefined)
    if (ready) removeAttachment()
    return true
  }, [attachment, notify, onOpenChat, removeAttachment])

  // The door to the hunt already running on the attached document.
  const openRunning = useCallback((hunt: RunningHunt) => {
    if (hunt.caseId) onOpenCase(hunt.caseId)
    else if (hunt.runId) onGo('workflows', { search: `?run=${encodeURIComponent(hunt.runId)}` })
    else return
    setOpen(false)
    setPreview(null)
  }, [onGo, onOpenCase])

  const remember = useCallback((text: string) => {
    if (!userId) return
    setRecents(writeRecent(userId, text))
  }, [userId])

  const forget = useCallback((text: string) => {
    if (userId) setRecents(removeRecent(userId, text))
    inputRef.current?.focus()
  }, [userId])

  const follow = useCallback((row: PaletteRow) => {
    if (row.dest === 'Recent' && row.recent) {
      setQuery(row.recent)
      setPreview(liveCommandIn(row.recent))
      setOpen(true)
      setActive(0)
      return
    }
    if (row.dest === 'Ask') {
      if (!ask(query.trim() || undefined)) return
      setOpen(false)
      setPreview(null)
      return
    }
    setOpen(false)
    setPreview(null)
    remember(query)
    if (row.dest === 'Case' && row.caseId) onOpenCase(row.caseId)
    else if (row.dest === 'Page' && row.page) {
      if (row.search) onGo(row.page, { search: row.search })
      else onGo(row.page)
    }
    else if (row.dest === 'Alert' && row.findingId) {
      // The popup hands focus back to its opener, which would reopen this menu.
      inputRef.current?.blur()
      onGo('overview', { search: `?alert=${encodeURIComponent(row.findingId)}` })
    }
  }, [ask, onGo, onOpenCase, query, remember])

  const choose = useCallback((row: PaletteRow) => {
    if (row.disabled) return
    if (row.fill !== undefined && preview) {
      const name = COMMANDS.find((c) => c.id === preview.id)?.name ?? ''
      setQuery(`${name} ${row.fill}`)
      setPreview({ id: preview.id, arg: row.fill })
      setActive(0)
      setRefocus((n) => n + 1)
      return
    }
    if (row.commandId && isLiveCommand(row.commandId)) {
      const arg = commandRemainder(query, row.label)
      setQuery(arg ? `${row.label} ${arg}` : `${row.label} `)
      setPreview({ id: row.commandId, arg })
      setOpen(true)
      setRefocus((n) => n + 1)
      return
    }
    follow(row)
  }, [follow, preview, query])

  const run = useCallback(async () => {
    if (!preview || commandPreview(preview.id, preview.arg, jira, attachment).disabled) return
    const arg = preview.arg.trim()
    // The workflow this command starts, from the command table.
    const command = COMMANDS.find((c) => c.id === preview.id)
    const workflowId = command?.workflowId ?? ''
    setRunning(true)
    // A file attached while the run starts is not the one it sent, so it stays.
    const sentSeq = attachSeq.current
    const dropSent = () => {
      if (attachSeq.current === sentSeq) removeAttachment()
    }
    try {
      switch (preview.id) {
        case 'investigate': {
          const document = attachment?.status === 'ready' ? attachment : null
          let finding = false
          try {
            await findingsApi.getById(arg)
            finding = true
          } catch {
            finding = false
          }
          // The server opens the case, or reuses the open one holding the alert.
          const started = await workflowApi.execute(workflowId, {
            ...(finding ? { finding_id: arg } : { context: arg }),
            ...(document && { document: document.text }),
          })
          const caseId = started.data.case_id
          if (!caseId) throw new Error('The run started without a case')
          // The original stays on the case, as a hunt's does.
          if (document) {
            await casesApi
              .attachDocument(caseId, document.file, { name: document.pasted ? PASTED_NAME : undefined, pages: document.pages })
              .catch((error: unknown) => notify('err', `The run started, but ${document.name} could not be kept on the case: ${errorText(error, 'upload failed')}`))
          }
          onOpenCase(caseId)
          dropSent()
          notify('ok', `Started ${command?.runs}`)
          break
        }
        case 'hunt': {
          // What was typed, else what the attached document proposed.
          const hypothesis = huntHypothesis(arg, attachment)
          const document = attachment?.status === 'ready' ? attachment : null
          // The server puts the run on a case, so the drawer has something to open on.
          const title = huntTitle(hypothesis)
          const started = await workflowApi.execute(workflowId, {
            hypothesis,
            ...(document && { document: document.text }),
            // The proposal's own subjects and approval ride with it, unedited.
            ...(!arg && document?.proposal?.status === 'proposed' && {
              hypothesis_subjects: document.proposal.subjects,
              approve_hypotheses: document.proposal.approve,
            }),
          })
          const caseId = started.data.case_id
          if (!caseId) throw new Error('The hunt started without a case')
          // After the run is queued, so a refused hunt leaves no original behind.
          if (document) {
            await casesApi
              .attachDocument(caseId, document.file, { name: document.pasted ? PASTED_NAME : undefined, pages: document.pages })
              .catch((error: unknown) => notify('err', `The hunt started, but ${document.name} could not be kept on the case: ${errorText(error, 'upload failed')}`))
          }
          onOpenCase(caseId)
          dropSent()
          notify('ok', `Hunt started on case "${title}"`)
          break
        }
        case 'replay': {
          // Investigations come newest first.
          const runId = (await casesApi.getById(arg)).data.investigations?.[0]?.run_id
          if (runId) onGo('workflows', { search: `?run=${encodeURIComponent(runId)}` })
          else {
            onOpenCase(arg)
            notify('info', `Case ${arg} has no run to replay`)
          }
          break
        }
        case 'ask':
          ask(arg)
          break
        case 'ticket':
          notify('ok', await casesApi.exportJira(arg, jira.projectKey))
          break
        default: {
          const exhaustive: never = preview.id
          return exhaustive
        }
      }
      setQuery('')
      setOpen(false)
      setPreview(null)
    } catch (error) {
      notify('err', errorText(error, 'Command failed'))
    } finally {
      setRunning(false)
    }
  }, [ask, attachment, jira, notify, onGo, onOpenCase, preview, removeAttachment])

  const onInputKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setOpen(true)
      setActive(moveEnabled(rows, highlighted, 1))
      return
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault()
      setOpen(true)
      setActive(moveEnabled(rows, highlighted, -1))
      return
    }
    if (event.key === 'Tab' && query.trim()) {
      event.preventDefault()
      if (!ask(query.trim())) return
      setOpen(false)
      setPreview(null)
      return
    }
    if (event.key === 'Enter') {
      event.preventDefault()
      // A command with its argument runs on this press; otherwise Enter completes or opens the row.
      if (canRun && !picking) {
        void run()
        return
      }
      const row = rows[highlighted]
      if (row) choose(row)
    }
  }

  const groups: { section: string; items: { row: PaletteRow; index: number }[] }[] = []
  rows.forEach((row, index) => {
    const last = groups[groups.length - 1]
    if (last?.section === row.section) last.items.push({ row, index })
    else groups.push({ section: row.section, items: [{ row, index }] })
  })

  return (
    <div
      className={`vg-command-slot${placement === 'hero' ? ' is-hero' : ''}`}
      data-command-slot=""
      ref={slotRef}
      onKeyDown={(event) => {
        if (event.key === 'Escape') {
          event.stopPropagation()
          event.preventDefault()
          setOpen(false)
        }
      }}
      onDragOver={(event) => {
        if (!carriesFile(event)) return
        event.preventDefault()
        setDragging(true)
      }}
      onDragLeave={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false)
      }}
      onDrop={onDropFile}
    >
      <div className={`vg-command-bar${open ? ' is-open' : ''}${dragging ? ' dragging' : ''}`}>
        <input
          ref={fileRef}
          type="file"
          hidden
          accept={ATTACH_TYPES.join(',')}
          aria-label="File to attach"
          onChange={(event) => {
            const file = event.target.files?.[0]
            event.target.value = ''
            if (file) void attach(file)
          }}
        />
        {/* A disabled button gets no hover, so the reason sits on a wrapper. */}
        <span className="vg-command-clip-wrap" title={attachBlocked ?? (attachment ? `Attached: ${attachment.name}` : 'Attach a file')}>
          <button
            type="button"
            className={`vg-command-clip${attachment ? ' is-on' : ''}`}
            aria-label="Attach a file"
            aria-description={attachBlocked ?? undefined}
            disabled={attachBlocked !== null}
            onClick={() => fileRef.current?.click()}
          >
            <Icon name="paperclip" size={placement === 'hero' ? 20 : 16} />
          </button>
        </span>
        <input
          ref={inputRef}
          role="combobox"
          name="vigil-command"
          autoComplete="off"
          data-1p-ignore
          data-lpignore="true"
          data-bwignore
          data-form-type="other"
          aria-label="Find a case, ask Vigil, or run a command"
          aria-expanded={open}
          aria-controls="vg-command-results"
          aria-activedescendant={open && rows[highlighted] ? rows[highlighted].key : undefined}
          placeholder="Ask Vigil, search, or type / for a command"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value)
            setHits(null)
            setPreview(liveCommandIn(event.target.value))
            setOpen(true)
            setActive(0)
          }}
          onFocus={() => setOpen(true)}
          onKeyDown={onInputKeyDown}
          onPaste={onPaste}
        />
        {query && (
          <button
            type="button"
            className="vg-command-clear"
            aria-label="Clear search"
            onClick={() => {
              setQuery('')
              removeAttachment()
              setHits(null)
              setPreview(null)
              setActive(0)
              inputRef.current?.focus()
            }}
          >
            <Icon name="close" size={14} />
          </button>
        )}
        <span className="vg-command-kbd">⌘K</span>
        <button
          type="button"
          className="vg-command-ask"
          onClick={() => {
            if (!ask()) return
            setOpen(false)
            setPreview(null)
          }}
        >
          <Icon name="sparkle" size={placement === 'hero' ? 15 : 13} />
          Ask Vigil
        </button>
      </div>
      {open && (
        <div className="vg-command-menu">
          {attachment && (
            <div className="vg-command-files">
              <span
                className={`vg-command-file${attachment.status === 'refused' ? ' is-refused' : ''}`}
                title={attachment.status === 'refused' ? attachment.reason : attachment.name}
                role={attachment.status === 'refused' ? 'alert' : undefined}
              >
                <Icon name="paperclip" size={13} />
                <span className="vg-command-file-name">{attachment.name}</span>
                <span className="vg-command-file-meta">
                  {attachment.status === 'reading' && 'reading…'}
                  {attachment.status === 'ready' && `${attachment.pages} page${attachment.pages === 1 ? '' : 's'}${attachment.condensed ? ' · condensed' : ''}`}
                  {attachment.status === 'refused' && attachment.reason}
                </span>
                <button type="button" aria-label={`Remove ${attachment.name}`} onClick={removeAttachment}>
                  <Icon name="close" size={12} />
                </button>
              </span>
            </div>
          )}
          <div className="vg-command-scroll" id="vg-command-results" role="listbox">
            {rows.length === 0 && <div className="vg-command-empty">No matches</div>}
            {groups.map((group) => (
              <div key={group.section} role="group" aria-label={group.section}>
                <div className="vg-command-section" aria-hidden="true">{group.section}</div>
                {group.items.map(({ row, index }) => {
                  const on = index === highlighted
                  return (
                    <div key={row.key} className={`vg-command-item${on ? ' is-on' : ''}`}>
                      <LaterTip later={row.disabled}>
                        <button
                          id={row.key}
                          type="button"
                          role="option"
                          aria-selected={on}
                          disabled={row.disabled}
                          className="vg-command-row"
                          onClick={() => choose(row)}
                        >
                          <span className="vg-command-icon"><Icon name={row.icon} size={15} /></span>
                          <span className="vg-command-label">{row.label}</span>
                          {row.hint && <span className="vg-command-hint">{row.hint}</span>}
                          <span className="vg-command-desc" title={row.desc}>{row.desc}</span>
                          <span className="vg-command-dest">
                            {tagText(row)}
                            {on && <span className="vg-command-enter">↵</span>}
                          </span>
                        </button>
                      </LaterTip>
                      {row.dest === 'Recent' && row.recent && (
                        <button
                          type="button"
                          className="vg-command-remove"
                          aria-label="Remove from history"
                          onClick={() => forget(row.recent as string)}
                        >
                          <Icon name="close" size={13} />
                        </button>
                      )}
                    </div>
                  )
                })}
              </div>
            ))}
          </div>
          {previewView && preview && (
            <div className="vg-command-preview">
              <p>{previewView.line}</p>
              <button
                type="button"
                className="btn primary"
                disabled={previewView.disabled || running}
                onClick={() => void run()}
              >
                Run
              </button>
              {(previewView.note || alreadyRunning) && (
                <div className="vg-command-attach">
                  {previewView.note && <span className="vg-command-note">{previewView.note}</span>}
                  {alreadyRunning && (alreadyRunning.caseId || alreadyRunning.runId) && (
                    <button type="button" className="btn" onClick={() => openRunning(alreadyRunning)}>
                      Open its case
                    </button>
                  )}
                </div>
              )}
            </div>
          )}
          <div className="vg-command-foot">
            <span><kbd>↑↓</kbd> move</span>
            <span><kbd>Enter</kbd> {canRun && !picking ? 'run' : picking ? 'use' : 'open'}</span>
            <span><kbd>Tab</kbd> {caseOpen ? 'ask on this case' : 'ask Vigil'}</span>
            <span><kbd>/</kbd> commands</span>
          </div>
        </div>
      )}
    </div>
  )
}
