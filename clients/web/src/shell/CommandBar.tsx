import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent, type KeyboardEvent, type ReactNode } from 'react'
import { useAuth } from '../contexts/AuthContext'
import api, { casesApi, configApi, findingsApi, workflowApi } from '../services/api'
import { Icon } from '../shared/icons'
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
  readRecents,
  removeRecent,
  tagText,
  writeRecent,
  type BoardLink,
  type Hit,
  type HuntAttachment,
  type MatchType,
  type JiraReadiness,
  type LiveCommandId,
  type PaletteRow,
  type SearchHits,
} from './commandBarModel'

const str = (value: unknown): string | undefined => (typeof value === 'string' && value.trim() ? value : undefined)

function hitFrom(id: unknown, title: unknown, extra?: Partial<Hit>): Hit | null {
  if (typeof id !== 'string' || !id) return null
  return { id, title: str(title) ?? id, ...extra }
}

function casesOf(data: unknown, matchType?: MatchType): Hit[] {
  const cases = (data as { cases?: unknown } | null)?.cases
  if (!Array.isArray(cases)) return []
  return cases
    .map((item) => {
      const row = item as { case_id?: unknown; title?: unknown; content?: unknown; name?: unknown; priority?: unknown; status?: unknown; match_type?: unknown }
      const type = row.match_type === 'case' || row.match_type === 'comment' || row.match_type === 'evidence' ? row.match_type : matchType
      return hitFrom(row.case_id, row.title ?? row.name ?? row.content, {
        priority: str(row.priority),
        status: str(row.status),
        matchType: type,
      })
    })
    .filter((hit): hit is Hit => hit !== null)
}

async function searchAll(query: string): Promise<SearchHits> {
  const [caseExact, findingExact, text, ioc] = await Promise.all([
    casesApi.getById(query).then((res) => {
      const data = res.data as { priority?: unknown; status?: unknown }
      return hitFrom(res.data.case_id, res.data.title, { priority: str(data.priority), status: str(data.status) })
    }).catch(() => null),
    findingsApi.getById(query).then((res) => {
      const data = res.data as { finding_id?: unknown; title?: unknown; data_source?: unknown; severity?: unknown }
      return hitFrom(data.finding_id, data.title, { dataSource: str(data.data_source), severity: str(data.severity) })
    }).catch(() => null),
    api.get('/cases/search/full-text', { params: { query } }).then((res) => res.data).catch(() => null),
    api.get('/cases/search/by-ioc', { params: { ioc_value: query } }).then((res) => res.data).catch(() => null),
  ])
  const textData = text as { cases?: unknown; comments?: unknown; evidence?: unknown } | null
  return {
    caseExact,
    findingExact,
    textCases: [
      ...casesOf(textData, 'case'),
      ...casesOf({ cases: textData?.comments }, 'comment'),
      ...casesOf({ cases: textData?.evidence }, 'evidence'),
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

  const highlightedKey = open ? rows[highlighted]?.key : undefined
  useEffect(() => {
    if (highlightedKey) document.getElementById(highlightedKey)?.scrollIntoView?.({ block: 'nearest' })
  }, [highlightedKey])

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
      setPreview(null)
      setOpen(true)
      setActive(0)
      return
    }
    setOpen(false)
    setPreview(null)
    if (row.dest === 'Ask') {
      onOpenChat(query.trim() || undefined)
      return
    }
    remember(query)
    if (row.dest === 'Case' && row.caseId) onOpenCase(row.caseId)
    else if (row.dest === 'Page' && row.page) onGo(row.page)
    else if (row.dest === 'Alert' && row.findingId) {
      // The popup hands focus back to its opener, which would reopen this menu.
      inputRef.current?.blur()
      onGo('overview', { search: `?alert=${encodeURIComponent(row.findingId)}` })
    }
  }, [onGo, onOpenCase, onOpenChat, query, remember])

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

  const groups: { section: string; items: { row: PaletteRow; index: number }[] }[] = []
  rows.forEach((row, index) => {
    const last = groups[groups.length - 1]
    if (last?.section === row.section) last.items.push({ row, index })
    else groups.push({ section: row.section, items: [{ row, index }] })
  })

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
      <div className={`vg-command-bar${open ? ' is-open' : ''}`}>
        <input
          ref={inputRef}
          role="combobox"
          aria-label="Find a case, ask Vigil, or run a command"
          aria-expanded={open}
          aria-controls="vg-command-results"
          aria-activedescendant={open && rows[highlighted] ? rows[highlighted].key : undefined}
          placeholder="Ask Vigil, search, or type / for a command"
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
            onOpenChat()
            setOpen(false)
            setPreview(null)
          }}
        >
          <Icon name="sparkle" size={13} />
          Ask Vigil
        </button>
      </div>
      {open && (
        <div className="vg-command-menu">
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
