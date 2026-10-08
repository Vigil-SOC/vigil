import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent, type KeyboardEvent, type ReactNode } from 'react'
import { useAuth } from '../contexts/AuthContext'
import api, { casesApi, configApi, findingsApi, workflowApi } from '../services/api'
import { useToast } from './toast'
import type { ConsoleScreenGoOptions } from '../shared/types'
import {
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
  jiraReadiness,
  moveEnabled,
  PASTED_NAME,
  proposalFrom,
  runningHunt,
  type RunningHunt,
  readRecents,
  writeRecent,
  type BoardLink,
  type HuntAttachment,
  type JiraReadiness,
  type LiveCommandId,
  type PaletteRow,
  type SearchHits,
} from './commandBarModel'

function hitFrom(id: unknown, title: unknown): { id: string; title: string } | null {
  if (typeof id !== 'string' || !id) return null
  return { id, title: typeof title === 'string' && title.trim() ? title : id }
}

function casesOf(data: unknown): { id: string; title: string }[] {
  const cases = (data as { cases?: unknown } | null)?.cases
  if (!Array.isArray(cases)) return []
  return cases
    .map((item) => {
      const row = item as { case_id?: unknown; title?: unknown; content?: unknown; name?: unknown }
      return hitFrom(row.case_id, row.title ?? row.name ?? row.content)
    })
    .filter((hit): hit is { id: string; title: string } => hit !== null)
}

async function searchAll(query: string): Promise<SearchHits> {
  const [caseExact, findingExact, text, ioc] = await Promise.all([
    casesApi.getById(query).then((res) => hitFrom(res.data.case_id, res.data.title)).catch(() => null),
    findingsApi.getById(query).then((res) => {
      const data = res.data as { finding_id?: unknown; title?: unknown }
      return hitFrom(data.finding_id, data.title)
    }).catch(() => null),
    api.get('/cases/search/full-text', { params: { query } }).then((res) => res.data).catch(() => null),
    api.get('/cases/search/by-ioc', { params: { ioc_value: query } }).then((res) => res.data).catch(() => null),
  ])
  const textData = text as { cases?: unknown; comments?: unknown; evidence?: unknown } | null
  return {
    caseExact,
    findingExact,
    textCases: [
      ...casesOf(textData),
      ...casesOf({ cases: textData?.comments }),
      ...casesOf({ cases: textData?.evidence }),
    ],
    iocCases: casesOf(ioc),
  }
}

function errorText(error: unknown, fallback: string): string {
  const data = (error as { response?: { data?: { detail?: unknown; error?: unknown } } })?.response?.data
  if (typeof data?.detail === 'string' && data.detail.trim()) return data.detail
  if (typeof data?.error === 'string' && data.error.trim()) return data.error
  const message = (error as { message?: string })?.message
  return message && message.trim() ? message : fallback
}

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
}: {
  boards: BoardLink[]
  onOpenChat: (prompt?: string) => void
  onOpenCase: (caseId: string) => void
  onGo: (screen: string, options?: ConsoleScreenGoOptions) => void
  /** A case is open, so asking goes to its composer instead of the dock. */
  caseOpen?: boolean
}) {
  const { user } = useAuth()
  const { notify } = useToast()
  const userId = user?.user_id
  const slotRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const runRef = useRef<HTMLButtonElement>(null)
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(0)
  const [recents, setRecents] = useState<string[]>([])
  const [hits, setHits] = useState<SearchHits | null>(null)
  const [jira, setJira] = useState<JiraReadiness>({ gap: 'Jira configuration could not be read', projectKey: '' })
  const [preview, setPreview] = useState<{ id: LiveCommandId; arg: string } | null>(null)
  const [running, setRunning] = useState(false)
  const [attachment, setAttachment] = useState<HuntAttachment | null>(null)
  const [pasting, setPasting] = useState<string | null>(null)
  const [dragging, setDragging] = useState(false)
  const attachSeq = useRef(0)
  const fileRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (userId) setRecents(readRecents(userId))
  }, [userId])

  useEffect(() => {
    let live = true
    configApi
      .getIntegrations()
      .then((res) => {
        if (live) setJira(jiraReadiness(res.data))
      })
      .catch(() => {
        if (live) setJira({ gap: 'Jira configuration could not be read', projectKey: '' })
      })
    return () => {
      live = false
    }
  }, [])

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
    const timer = setTimeout(() => {
      void searchAll(q).then((next) => {
        if (!live) return
        setHits(next)
        setActive(0)
      })
    }, 150)
    return () => {
      live = false
      clearTimeout(timer)
    }
  }, [open, query])

  const rows = useMemo(() => buildRows(query, hits, recents, boards), [query, hits, recents, boards])
  const highlighted = rows[active] && !rows[active].disabled ? active : firstEnabled(rows)
  const previewView = preview ? commandPreview(preview.id, preview.arg, jira, attachment) : null
  const previewKey = preview ? `${preview.id}:${preview.arg}` : ''
  const previewDisabled = previewView?.disabled ?? true
  const alreadyRunning = preview?.id === 'hunt' && !preview.arg.trim() ? runningHunt(attachment) : null

  useEffect(() => {
    if (previewKey && !previewDisabled) runRef.current?.focus()
  }, [previewKey, previewDisabled])

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

  const onDropFile = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault()
    setDragging(false)
    const file = event.dataTransfer.files?.[0]
    if (file) void attach(file)
  }

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

  const follow = useCallback((row: PaletteRow) => {
    if (row.dest === 'Recent' && row.recent) {
      setQuery(row.recent)
      setPreview(null)
      setOpen(true)
      setActive(0)
      return
    }
    remember(query)
    setOpen(false)
    setPreview(null)
    if (row.dest === 'Case' && row.caseId) onOpenCase(row.caseId)
    else if (row.dest === 'Page' && row.page) onGo(row.page)
    else if (row.dest === 'Alert' && row.findingId) {
      // The popup hands focus back to its opener, which would reopen this menu.
      inputRef.current?.blur()
      onGo('overview', { search: `?alert=${encodeURIComponent(row.findingId)}` })
    }
  }, [onGo, onOpenCase, query, remember])

  const choose = useCallback((row: PaletteRow) => {
    if (row.disabled) return
    if (row.commandId && isLiveCommand(row.commandId)) {
      const arg = commandRemainder(query, row.label)
      setQuery(arg ? `${row.label} ${arg}` : `${row.label} `)
      setPreview({ id: row.commandId, arg })
      setOpen(true)
      return
    }
    follow(row)
  }, [follow, query])

  const run = useCallback(async () => {
    if (!preview || commandPreview(preview.id, preview.arg, jira, attachment).disabled) return
    const arg = preview.arg.trim()
    // The workflow this command starts, from the command table.
    const command = COMMANDS.find((c) => c.id === preview.id)
    const workflowId = command?.workflowId ?? ''
    setRunning(true)
    try {
      switch (preview.id) {
        case 'investigate': {
          let finding = false
          try {
            await findingsApi.getById(arg)
            finding = true
          } catch {
            finding = false
          }
          await workflowApi.execute(workflowId, finding ? { finding_id: arg } : { context: arg })
          notify('ok', `Started ${command?.runs}`)
          break
        }
        case 'hunt': {
          // What was typed, else what the attached document proposed.
          const hypothesis = huntHypothesis(arg, attachment)
          const document = attachment?.status === 'ready' ? attachment : null
          // The run goes on a case of its own, so the drawer has something to open on.
          const title = huntTitle(hypothesis)
          const created = await casesApi.create({
            title,
            description: hypothesis,
            finding_ids: [],
            priority: 'medium',
            status: 'open',
          })
          const caseId = created.data.case_id
          if (!caseId) throw new Error('The case was created without an id')
          try {
            await workflowApi.execute(workflowId, {
              hypothesis,
              case_id: caseId,
              ...(document && { document: document.text }),
              // The proposal's own subjects and approval ride with it, unedited.
              ...(!arg && document?.proposal?.status === 'proposed' && {
                hypothesis_subjects: document.proposal.subjects,
                approve_hypotheses: document.proposal.approve,
              }),
            })
          } catch (error) {
            // A refusal (disabled workflow, not a claim) leaves no case behind. With
            // no response the run may have been queued, and its case stays.
            if ((error as { response?: unknown })?.response) {
              await casesApi.delete(caseId).catch(() => undefined)
            }
            throw error
          }
          // After the run is queued, so a refused hunt leaves no original behind.
          if (document) {
            await casesApi
              .attachDocument(caseId, document.file, { name: document.pasted ? PASTED_NAME : undefined, pages: document.pages })
              .catch((error: unknown) => notify('err', `The hunt started, but ${document.name} could not be kept on the case: ${errorText(error, 'upload failed')}`))
          }
          onOpenCase(caseId)
          setQuery('')
          removeAttachment()
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
          onOpenChat(arg)
          break
        case 'ticket': {
          const res = await api.post<{ success?: boolean; error?: string; issue_key?: string }>(
            `/cases/${encodeURIComponent(arg)}/export/jira`,
            { project_key: jira.projectKey },
          )
          if (res.data?.success === false) {
            notify('err', res.data.error || 'Jira export failed')
            return
          }
          notify('ok', res.data?.issue_key ? `Created ${res.data.issue_key}` : 'Ticket created')
          break
        }
        default: {
          const exhaustive: never = preview.id
          return exhaustive
        }
      }
      setOpen(false)
      setPreview(null)
    } catch (error) {
      notify('err', errorText(error, 'Command failed'))
    } finally {
      setRunning(false)
    }
  }, [attachment, jira, notify, onGo, onOpenCase, onOpenChat, preview, removeAttachment])

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
      onOpenChat(query.trim())
      setOpen(false)
      setPreview(null)
      return
    }
    if (event.key === 'Enter') {
      event.preventDefault()
      const row = rows[highlighted]
      if (row) choose(row)
    }
  }

  return (
    <div
      className="vg-command-slot"
      data-command-slot=""
      ref={slotRef}
      onKeyDown={(event) => {
        if (event.key === 'Escape') {
          event.stopPropagation()
          event.preventDefault()
          setOpen(false)
        }
      }}
    >
      <input
        ref={inputRef}
        role="combobox"
        aria-label="Find a case, ask Vigil, or run a command"
        aria-expanded={open}
        aria-controls="vg-command-results"
        aria-activedescendant={open && rows[highlighted] ? rows[highlighted].key : undefined}
        placeholder="Find a case, ask Vigil, or run a command"
        value={query}
        onChange={(event) => {
          setQuery(event.target.value)
          if (!/^\/hunt(\s|$)/i.test(event.target.value.trim())) removeAttachment()
          setHits(null)
          setPreview(null)
          setOpen(true)
          setActive(0)
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={onInputKeyDown}
      />
      <span className="vg-command-kbd">⌘K</span>
      {open && (
        <div className="vg-command-menu" id="vg-command-results" role="listbox">
          {rows.length === 0 && <div className="vg-command-empty">No matches</div>}
          {rows.map((row, index) => (
            <LaterTip key={row.key} later={row.disabled}>
              <button
                id={row.key}
                type="button"
                role="option"
                aria-selected={index === highlighted}
                disabled={row.disabled}
                className="vg-command-row"
                onClick={() => choose(row)}
              >
                <span className="vg-command-label">{row.label}</span>
                {row.hint && <span className="vg-command-hint">{row.hint}</span>}
                <span className="vg-command-dest">{row.dest}</span>
              </button>
            </LaterTip>
          ))}
          {previewView && preview && (
            <div
              className={`vg-command-preview${dragging ? ' dragging' : ''}`}
              onDragOver={preview.id === 'hunt' ? (event) => { event.preventDefault(); setDragging(true) } : undefined}
              onDragLeave={preview.id === 'hunt' ? () => setDragging(false) : undefined}
              onDrop={preview.id === 'hunt' ? onDropFile : undefined}
            >
              <p>{previewView.line}</p>
              <button
                ref={runRef}
                type="button"
                className="btn primary"
                disabled={previewView.disabled || running}
                onClick={() => void run()}
              >
                Run
              </button>
              {preview.id === 'hunt' && (
                <div className="vg-command-attach">
                  <input
                    ref={fileRef}
                    type="file"
                    hidden
                    accept={ATTACH_TYPES.join(',')}
                    aria-label="Attach intelligence file"
                    onChange={(event) => {
                      const file = event.target.files?.[0]
                      event.target.value = ''
                      if (file) void attach(file)
                    }}
                  />
                  <button type="button" className="btn" onClick={() => fileRef.current?.click()}>
                    Attach intelligence
                  </button>
                  <button type="button" className="btn" aria-expanded={pasting !== null} onClick={() => setPasting((text) => (text === null ? '' : null))}>
                    Paste text
                  </button>
                  {attachment && attachment.status !== 'refused' && (
                    <span className="vg-command-file" title={attachment.name}>
                      <span className="vg-command-file-name">{attachment.name}</span>
                      {attachment.status === 'ready' && (
                        <span className="vg-command-file-meta">
                          {attachment.pages} page{attachment.pages === 1 ? '' : 's'}
                          {attachment.condensed ? ' · condensed' : ''}
                        </span>
                      )}
                    </span>
                  )}
                  {attachment && (
                    <button type="button" className="btn" aria-label={`Remove ${attachment.name}`} onClick={removeAttachment}>
                      Remove
                    </button>
                  )}
                  {previewView.note && <span className="vg-command-note">{previewView.note}</span>}
                  {alreadyRunning && (alreadyRunning.caseId || alreadyRunning.runId) && (
                    <button type="button" className="btn" onClick={() => openRunning(alreadyRunning)}>
                      Open its case
                    </button>
                  )}
                  {pasting !== null && (
                    <div className="vg-command-paste">
                      <textarea
                        aria-label="Intelligence text"
                        placeholder="Paste a report, an advisory or a list of indicators"
                        value={pasting}
                        onChange={(event) => setPasting(event.target.value)}
                      />
                      <button
                        type="button"
                        className="btn"
                        disabled={!pasting.trim()}
                        onClick={() => {
                          void attach(new File([pasting], `${PASTED_NAME}.txt`, { type: 'text/plain' }), true)
                          setPasting(null)
                        }}
                      >
                        Attach text
                      </button>
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
          <div className="vg-command-foot">
            <span><kbd>↑↓</kbd> move</span>
            <span><kbd>Enter</kbd> open</span>
            <span><kbd>Tab</kbd> {caseOpen ? 'ask on this case' : 'ask Vigil'}</span>
            <span><kbd>/</kbd> commands</span>
          </div>
        </div>
      )}
    </div>
  )
}
