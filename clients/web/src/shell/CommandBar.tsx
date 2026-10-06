import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { useAuth } from '../contexts/AuthContext'
import api, { casesApi, configApi, findingsApi, workflowApi } from '../services/api'
import { useToast } from './toast'
import {
  buildRows,
  COMMANDS,
  commandPreview,
  commandRemainder,
  firstEnabled,
  isLiveCommand,
  jiraReadiness,
  moveEnabled,
  readRecents,
  writeRecent,
  type BoardLink,
  type JiraReadiness,
  type LiveCommandId,
  type PaletteRow,
  type SearchHits,
} from './commandBar'

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

export default function CommandBar({
  boards,
  onOpenChat,
  onOpenCase,
  onGo,
}: {
  boards: BoardLink[]
  onOpenChat: (prompt?: string) => void
  onOpenCase: (caseId: string) => void
  onGo: (screen: string) => void
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
  const previewView = preview ? commandPreview(preview.id, preview.arg, jira) : null
  const previewKey = preview ? `${preview.id}:${preview.arg}` : ''
  const previewDisabled = previewView?.disabled ?? true

  useEffect(() => {
    if (previewKey && !previewDisabled) runRef.current?.focus()
  }, [previewKey, previewDisabled])

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
    // A finding has no detail route; taking the row records the search.
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
    if (!preview || commandPreview(preview.id, preview.arg, jira).disabled) return
    const arg = preview.arg.trim()
    // The workflow this command starts, from the command table.
    const workflowId = COMMANDS.find((c) => c.id === preview.id)?.workflowId ?? ''
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
          break
        }
        case 'hunt':
          await workflowApi.execute(workflowId, { hypothesis: arg })
          break
        case 'replay':
          onOpenCase(arg)
          break
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
    }
  }, [jira, notify, onOpenCase, onOpenChat, preview])

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
            <button
              key={row.key}
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
          ))}
          {previewView && (
            <div className="vg-command-preview">
              <p>{previewView.line}</p>
              <button
                ref={runRef}
                type="button"
                className="btn primary"
                disabled={previewView.disabled}
                onClick={() => void run()}
              >
                Run
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
