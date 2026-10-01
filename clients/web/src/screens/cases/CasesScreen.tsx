import { useCallback, useEffect, useMemo, useState, type KeyboardEvent, type ReactNode } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Icon } from '../../shared/icons'
import { caseSearchApi, casesApi } from '../../services/api'
import { mapQueueCase } from '../../data/mappers'
import type { CaseRow } from '../../data/data'
import type { ConsoleScreenProps } from '../../shared/types'
import {
  useCases,
  useCaseDetail,
  INITIAL_CASE_FILTERS,
  type CaseFilters,
  type CaseStrip,
  type Phase,
} from './useCases'
import { ConfirmDialog, EmptyState, FilterButton, FilterGroup, Popup, Select } from '../../shared/ui'
import { useAuth } from '../../contexts/AuthContext'
import { useToast } from '../../shell/toast'
import { inputCls } from './CaseSections'
import { CasePage } from './CasePage'

const cap = (s: string) => s[0].toUpperCase() + s.slice(1)

function caseActionError(error: unknown, fallback: string): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  return (error as { message?: string })?.message || fallback
}

const CASE_PRIO_OPTIONS: { value: CaseRow['prio']; label: string }[] = [
  { value: 'critical', label: 'Critical' },
  { value: 'high', label: 'High' },
  { value: 'medium', label: 'Medium' },
  { value: 'low', label: 'Low' },
  { value: 'unknown', label: 'Unknown' },
]
const RATED_PRIO_OPTIONS = CASE_PRIO_OPTIONS.filter((o) => o.value !== 'unknown')

const STATE_OPTIONS = [
  { value: '', label: 'Open queue' },
  { value: 'new', label: 'New' },
  { value: 'open', label: 'Open' },
  { value: 'investigating', label: 'Investigating' },
  { value: 'assigned', label: 'Assigned' },
  { value: 'executing', label: 'Executing' },
  { value: 'waiting_approval', label: 'Waiting approval' },
  { value: 'review_submitted', label: 'In review' },
  { value: 'closed', label: 'Closed' },
]

function budgetCell(c: CaseRow): string {
  if (c.costUsd == null && c.maxCostUsd == null) return '—'
  const cost = c.costUsd == null ? '—' : c.costUsd.toFixed(2)
  const max = c.maxCostUsd == null ? '—' : c.maxCostUsd.toFixed(2)
  return c.budgetHealth ? `${cost}/${max} ${c.budgetHealth}` : `${cost}/${max}`
}

function stripStates(by: Record<string, number>): string {
  const parts = Object.entries(by).filter(([, n]) => n > 0)
  if (!parts.length) return '—'
  return parts.map(([state, n]) => `${n} ${state}`).join(' · ')
}


export default function CasesScreen({ openChat, setViewFull }: ConsoleScreenProps) {
  // the open case is a ?case=<id> param, so a detail view is deep-linkable
  const [searchParams, setSearchParams] = useSearchParams()
  const selected = searchParams.get('case')
  const [filters, setFilters] = useState(INITIAL_CASE_FILTERS)
  const { rows, total, strip, phase, error, reload } = useCases(filters)

  const selectCase = useCallback(
    (id: string) => setSearchParams({ case: id }),
    [setSearchParams],
  )
  const backToList = useCallback(() => setSearchParams({}), [setSearchParams])

  useEffect(() => {
    setViewFull(selected !== null)
  }, [selected, setViewFull])

  return selected ? (
    <CasesDetail
      id={selected}
      rows={rows}
      onSelect={selectCase}
      onBack={backToList}
      openChat={openChat}
      reloadList={reload}
    />
  ) : (
    <CasesTable
      rows={rows}
      total={total}
      strip={strip}
      phase={phase}
      error={error}
      reload={reload}
      filters={filters}
      onFilters={setFilters}
      onSelect={selectCase}
    />
  )
}

/* small state row spanning the whole table */
function StateRow({ children }: { children: ReactNode }) {
  return (
    <tr>
      <td colSpan={14}>
        {children}
      </td>
    </tr>
  )
}

function CasesTable({
  rows,
  total,
  strip,
  phase,
  error,
  reload,
  filters,
  onFilters,
  onSelect,
}: {
  rows: CaseRow[]
  total: number
  strip: CaseStrip
  phase: Phase
  error: string | null
  reload: () => void
  filters: CaseFilters
  onFilters: (next: CaseFilters) => void
  onSelect: (id: string) => void
}) {
  const { hasPermission } = useAuth()
  const canDelete = hasPermission('cases.delete')
  const [showAdvanced, setShowAdvanced] = useState(false)
  // Advanced search replaces the page until cleared. Results stay in API order.
  const [results, setResults] = useState<CaseRow[] | null>(null)
  const [newOpen, setNewOpen] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<CaseRow | null>(null)

  const setFilters = (partial: Partial<CaseFilters>) =>
    onFilters({
      ...filters,
      ...partial,
      offset: 'offset' in partial ? partial.offset ?? 0 : 0,
    })

  const activeFilters =
    (filters.state ? 1 : 0) +
    (filters.priority !== 'any' ? 1 : 0) +
    (filters.sla ? 1 : 0) +
    (filters.assignee.trim() ? 1 : 0) +
    (filters.workflow.trim() ? 1 : 0) +
    (filters.dataSource.trim() ? 1 : 0)
  const showingSearch = results !== null
  const display = showingSearch ? results : rows
  const openCount = Object.values(strip.by_state).reduce((sum, n) => sum + n, 0)
  const pageStart = total === 0 ? 0 : filters.offset + 1
  const pageEnd = Math.min(filters.offset + rows.length, total)
  const agentShare = strip.closed_today
    ? `${Math.round(strip.agent_closure_share * 100)}%`
    : '—'

  return (
    <>
      <div className="flex items-center gap-3 flex-wrap px-[22px] py-[13px] border-b border-line">
        <div className="search" style={{ maxWidth: 320 }}>
          <span><Icon name="search" /></span>
          <input
            aria-label="Search cases"
            placeholder="Search cases by title, ID, owner…"
            value={filters.query}
            onChange={(e) => setFilters({ query: e.target.value })}
          />
        </div>
        <FilterButton
          activeCount={activeFilters}
          onClearAll={() => setFilters({
            state: '',
            priority: 'any',
            sla: '',
            assignee: '',
            workflow: '',
            dataSource: '',
          })}
        >
          <FilterGroup
            label="State"
            value={filters.state}
            onSelect={(state) => setFilters({ state })}
            options={STATE_OPTIONS}
          />
          <FilterGroup
            label="Priority"
            value={filters.priority}
            onSelect={(priority) => setFilters({ priority })}
            options={[{ value: 'any', label: 'Any' }, ...CASE_PRIO_OPTIONS]}
          />
          <FilterGroup
            label="SLA"
            value={filters.sla}
            onSelect={(sla) => setFilters({ sla: sla as CaseFilters['sla'] })}
            options={[
              { value: '', label: 'Any' },
              { value: 'risk', label: 'At risk' },
            ]}
          />
          <label className="filter-grp">
            <span className="filter-grp-label">Assignee</span>
            <input aria-label="Assignee filter" className={inputCls} value={filters.assignee} onChange={(e) => setFilters({ assignee: e.target.value })} />
          </label>
          <label className="filter-grp">
            <span className="filter-grp-label">Workflow</span>
            <input aria-label="Workflow filter" className={inputCls} value={filters.workflow} onChange={(e) => setFilters({ workflow: e.target.value })} />
          </label>
          <label className="filter-grp">
            <span className="filter-grp-label">Data source</span>
            <input aria-label="Data source filter" className={inputCls} value={filters.dataSource} onChange={(e) => setFilters({ dataSource: e.target.value })} />
          </label>
        </FilterButton>
        <div className="flex-1" />
        <button
          className={`btn ${showAdvanced ? 'primary' : 'ghost'}`}
          onClick={() => setShowAdvanced((v) => !v)}
        >
          Advanced Search
        </button>
        <button className="btn ghost icon" title="Refresh" onClick={reload}><Icon name="refresh" /></button>
        <button className="btn primary" onClick={() => setNewOpen(true)}><Icon name="plus" /> New Case</button>
      </div>
      {showAdvanced && <AdvancedSearchPanel onResults={setResults} rows={rows} />}
      {results && (
        <div className="adv-results-bar">
          <span>Showing <b>{results.length}</b> search result{results.length === 1 ? '' : 's'}</span>
          <button className="btn ghost" onClick={() => setResults(null)}>Clear search</button>
        </div>
      )}
      <div className="kpi-strip">
        <div className="kpi">
          <div className="k-label">Open</div>
          <div className="k-row"><span className="k-val">{openCount}</span></div>
          <div className="muted" style={{ fontSize: 12 }}>{stripStates(strip.by_state)}</div>
        </div>
        <div className="kpi">
          <div className="k-label">SLA at risk</div>
          <div className="k-row"><span className="k-val">{strip.sla_at_risk}</span></div>
        </div>
        <div className="kpi">
          <div className="k-label">Closed today</div>
          <div className="k-row"><span className="k-val">{strip.closed_today}</span></div>
        </div>
        <div className="kpi">
          <div className="k-label">Agent closures</div>
          <div className="k-row"><span className="k-val" style={{ fontSize: 18 }}>{agentShare}</span></div>
        </div>
      </div>
      <div className="table-wrap list-scroll">
        <table className="tbl cases-tbl">
          <thead>
            <tr>
              <th>Case ID</th>
              <th>Title</th>
              <th>State</th>
              <th>Priority</th>
              <th>Assignee</th>
              <th>Findings</th>
              <th>Workflow</th>
              <th>Iterations</th>
              <th>Budget</th>
              <th>Comments</th>
              <th>Age</th>
              <th>SLA</th>
              <th>Last activity</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {phase === 'loading' && <StateRow><EmptyState loading table compact icon="folder" title="Loading cases…" /></StateRow>}
            {phase === 'error' && (
              <StateRow>
                <EmptyState error table icon="alert" title="Couldn’t load cases" body={error} primary={{ label: 'Retry', onClick: reload, icon: 'refresh' }} />
              </StateRow>
            )}
            {phase === 'ready' && display.length === 0 && (
              <StateRow>
                <EmptyState
                  table
                  icon={showingSearch || activeFilters || filters.query ? 'filter' : 'folder'}
                  title={showingSearch ? 'No cases match this search' : activeFilters || filters.query ? 'No cases match these filters' : 'No cases yet'}
                  body={showingSearch || activeFilters || filters.query ? 'Clear the search and filters to return to the open queue.' : 'Create a case manually or link findings from the dashboard to start an investigation record.'}
                  primary={showingSearch || activeFilters || filters.query ? { label: 'Clear filters', onClick: () => { setResults(null); onFilters({ ...INITIAL_CASE_FILTERS, limit: filters.limit }) }, icon: 'close' } : { label: 'New case', onClick: () => setNewOpen(true), icon: 'plus' }}
                />
              </StateRow>
            )}
            {phase === 'ready' &&
              display.map((c) => (
                <tr key={c.id} className="clickable" onClick={() => onSelect(c.id)}>
                  <td><span className="id-cell">{c.id}</span></td>
                  <td className="case-title" title={c.title}>{c.title}</td>
                  <td><span className={`status ${c.status}`}>{c.status}</span></td>
                  <td><span className={`prio ${c.prio}`}>{cap(c.prio)}</span></td>
                  <td><span className="assignee"><span className="avatar">{c.owner}</span><span className="muted">{c.ownerName}</span></span></td>
                  <td><b>{c.findings}</b></td>
                  <td className="muted">{c.workflowId || '—'}</td>
                  <td className="muted">{c.iterations ?? '—'}</td>
                  <td className="muted">{budgetCell(c)}</td>
                  <td>{c.comments ?? 0}</td>
                  <td className="muted">{c.age}</td>
                  <td><span className={`sla ${c.slaState}`}>{c.sla}</span></td>
                  <td className="muted">{c.updated}</td>
                  <td>
                    <span className="row-act" style={{ opacity: 1 }}>
                      {canDelete && (
                        <button
                          type="button"
                          aria-label={`Delete case ${c.id}`}
                          title="Delete case"
                          style={{ color: 'var(--crit)' }}
                          onClick={(event) => {
                            event.stopPropagation()
                            setDeleteTarget(c)
                          }}
                        >
                          <Icon name="trash" />
                        </button>
                      )}
                      <button
                        type="button"
                        aria-label={`Open case ${c.id}`}
                        title="Open case"
                        onClick={(event) => {
                          event.stopPropagation()
                          onSelect(c.id)
                        }}
                      >
                        <Icon name="arrowR" />
                      </button>
                    </span>
                  </td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>
      {!showingSearch && (
        <div className="pager">
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
            Rows per page:
            <select
              className="pg-size"
              aria-label="Rows per page"
              value={filters.limit}
              onChange={(e) => setFilters({ limit: Number(e.target.value) })}
            >
              {[10, 25, 50, 100].map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </span>
          <span>
            {total === 0 ? '0 of 0' : `${pageStart}–${pageEnd} of ${total}`}
          </span>
          <span style={{ display: 'flex', gap: 6 }}>
            <button className="pg-btn" aria-label="Previous page" disabled={filters.offset <= 0} onClick={() => setFilters({ offset: Math.max(0, filters.offset - filters.limit) })}><Icon name="chevL" size={14} /></button>
            <button className="pg-btn" aria-label="Next page" disabled={filters.offset + rows.length >= total} onClick={() => setFilters({ offset: filters.offset + filters.limit })}><Icon name="chevR" size={14} /></button>
          </span>
        </div>
      )}

      <NewCaseDialog open={newOpen} onClose={() => setNewOpen(false)} onCreated={reload} />
      <DeleteCaseDialog
        target={deleteTarget}
        onClose={() => setDeleteTarget(null)}
        onDeleted={(deleted) => {
          setResults((current) => current?.filter((c) => c.id !== deleted.id) ?? null)
          reload()
        }}
      />
    </>
  )
}

function DeleteCaseDialog({
  target,
  onClose,
  onDeleted,
}: {
  target: CaseRow | null
  onClose: () => void
  onDeleted: (deleted: CaseRow) => void
}) {
  const { notify } = useToast()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (target) setError('')
  }, [target])

  const remove = async () => {
    if (!target || busy) return
    const deleting = target
    setBusy(true)
    setError('')
    try {
      await casesApi.delete(deleting.id)
      setBusy(false)
      notify('ok', `Deleted ${deleting.id}. Linked findings were preserved.`)
      onClose()
      onDeleted(deleting)
    } catch (cause) {
      const message = caseActionError(cause, `Failed to delete ${deleting.id}`)
      setBusy(false)
      setError(message)
      notify('err', message)
    }
  }

  return (
    <ConfirmDialog
      open={target !== null}
      title="Delete case?"
      body={
        <span className="flex flex-col gap-2">
          <span>
            Permanently delete case <span className="mono text-tx">{target?.id}</span>?
          </span>
          <span>
            The case and its case-owned records will be removed. Linked findings will remain.
            This cannot be undone.
          </span>
          {error && <span role="alert" style={{ color: 'var(--crit)' }}>{error}</span>}
        </span>
      }
      confirmLabel="Delete case"
      busy={busy}
      onConfirm={remove}
      onClose={() => {
        if (!busy) onClose()
      }}
    />
  )
}

function AdvancedSearchPanel({ onResults, rows }: { onResults: (r: CaseRow[] | null) => void; rows: CaseRow[] }) {
  const [query, setQuery] = useState('')
  const [priority, setPriority] = useState('')
  const [status, setStatus] = useState('')
  const [assignee, setAssignee] = useState('')
  const [tags, setTags] = useState('')
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [sugOpen, setSugOpen] = useState(false)

  // typeahead suggestions drawn from loaded case titles/ids matching the query
  const suggestions = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return [] as { value: string; meta: string }[]
    const out: { value: string; meta: string }[] = []
    const seen = new Set<string>()
    for (const c of rows) {
      if (out.length >= 6) break
      const hay = `${c.title} ${c.id}`.toLowerCase()
      if (hay.includes(q) && !seen.has(c.title)) {
        seen.add(c.title)
        out.push({ value: c.title, meta: c.id })
      }
    }
    return out
  }, [rows, query])

  const run = async () => {
    const filters: Record<string, unknown> = {}
    if (priority) filters.priority = priority
    if (status) filters.status = status
    if (assignee.trim()) filters.assignee = assignee.trim()
    const tagList = tags.split(',').map((t) => t.trim()).filter(Boolean)
    if (tagList.length) filters.tags = tagList
    if (start) filters.start_date = start
    if (end) filters.end_date = end
    if (!query.trim() && Object.keys(filters).length === 0) {
      onResults(null)
      return
    }
    setBusy(true)
    setErr('')
    try {
      const res = await caseSearchApi.search({ query: query.trim(), filters, limit: 50 })
      const cases = (res.data?.cases || []) as Parameters<typeof mapQueueCase>[0][]
      onResults(cases.map((c) => mapQueueCase(c)))
    } catch (e) {
      setErr((e as { message?: string })?.message || 'Search failed')
    } finally {
      setBusy(false)
    }
  }

  const reset = () => {
    setQuery(''); setPriority(''); setStatus(''); setAssignee(''); setTags(''); setStart(''); setEnd('')
    setErr('')
    onResults(null)
  }

  const onKey = (e: KeyboardEvent) => {
    if (e.key === 'Enter') run()
  }

  return (
    <div className="adv-search">
      <label className="af span-all"><span>Full-text query</span>
        <div className="sug-wrap">
          <input
            className={inputCls}
            placeholder="Search title, description, IOCs, or case ID…"
            value={query}
            onChange={(e) => { setQuery(e.target.value); setSugOpen(true) }}
            onKeyDown={(e) => { if (e.key === 'Enter') setSugOpen(false); onKey(e) }}
            onFocus={() => setSugOpen(true)}
            onBlur={() => setTimeout(() => setSugOpen(false), 120)}
            autoComplete="off"
          />
          {sugOpen && suggestions.length > 0 && (
            <div className="drop-menu sug-menu" role="listbox">
              {suggestions.map((s) => (
                <button
                  key={s.meta}
                  type="button"
                  role="option"
                  onMouseDown={(e) => { e.preventDefault(); setQuery(s.value); setSugOpen(false) }}
                >
                  <span className="sug-val">{s.value}</span>
                  <span className="sug-meta mono">{s.meta}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      </label>
      <label className="af"><span>Priority</span>
        <Select
          value={priority}
          onSelect={setPriority}
          placeholder="Any"
          options={[
            { value: '', label: 'Any' },
            ...CASE_PRIO_OPTIONS,
          ]}
        />
      </label>
      <label className="af"><span>Status</span>
        <Select
          value={status}
          onSelect={setStatus}
          placeholder="Any"
          options={[
            { value: '', label: 'Any' },
            { value: 'open', label: 'Open' },
            { value: 'investigating', label: 'Investigating' },
            { value: 'closed', label: 'Closed' },
          ]}
        />
      </label>
      <label className="af"><span>Assignee</span>
        <input className={inputCls} placeholder="name or email" value={assignee} onChange={(e) => setAssignee(e.target.value)} onKeyDown={onKey} />
      </label>
      <label className="af"><span>Tags</span>
        <input className={inputCls} placeholder="comma-separated" value={tags} onChange={(e) => setTags(e.target.value)} onKeyDown={onKey} />
      </label>
      <label className="af"><span>From</span>
        <input className={inputCls} type="date" value={start} onChange={(e) => setStart(e.target.value)} />
      </label>
      <label className="af"><span>To</span>
        <input className={inputCls} type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
      </label>
      <div className="af-actions">
        {err && <span className="muted" style={{ color: 'var(--crit)' }}>{err}</span>}
        <span style={{ flex: 1 }} />
        <button className="btn ghost" onClick={reset}>Clear</button>
        <button className="btn primary" onClick={run} disabled={busy}>{busy ? 'Searching…' : 'Search'}</button>
      </div>
    </div>
  )
}

function NewCaseDialog({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: () => void }) {
  const [title, setTitle] = useState('')
  const [priority, setPriority] = useState('medium')
  const [status, setStatus] = useState('open')
  const [description, setDescription] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const submit = async () => {
    if (!title.trim()) {
      setErr('Title is required.')
      return
    }
    setBusy(true)
    setErr('')
    try {
      await casesApi.create({
        title: title.trim(),
        description: description.trim(),
        finding_ids: [],
        priority,
        status,
      })
      setTitle(''); setDescription(''); setPriority('medium'); setStatus('open')
      onCreated()
      onClose()
    } catch (e) {
      setErr((e as { message?: string })?.message || 'Failed to create case')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Popup open={open} onClose={onClose} title="New case" width={520}>
      <div className="flex flex-col gap-3.5">
        <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
          <span>Title</span>
          <input className={inputCls} placeholder="Case title" value={title} onChange={(e) => setTitle(e.target.value)} autoFocus />
        </label>
        <div className="grid grid-cols-2 gap-3">
          <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
            <span>Priority</span>
            <Select
              value={priority}
              onSelect={setPriority}
              options={RATED_PRIO_OPTIONS}
            />
          </label>
          <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
            <span>Status</span>
            <Select
              value={status}
              onSelect={setStatus}
              options={[
                { value: 'open', label: 'Open' },
                { value: 'investigating', label: 'Investigating' },
                { value: 'closed', label: 'Closed' },
              ]}
            />
          </label>
        </div>
        <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
          <span>Description</span>
          <textarea className={inputCls} rows={4} placeholder="Optional description" value={description} onChange={(e) => setDescription(e.target.value)} style={{ resize: 'vertical' }} />
        </label>
        {err && <div className="text-[13px]" style={{ color: 'var(--crit)' }}>{err}</div>}
        <div className="flex justify-end gap-2.5">
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn primary" onClick={submit} disabled={busy}>{busy ? 'Creating…' : 'Create case'}</button>
        </div>
      </div>
    </Popup>
  )
}

/** What a closer says a Case turned out to be. `duplicate` is bookkeeping rather
 *  than a determination, so it records a category and mints no verdict. */
const CLOSURE_CATEGORIES = [
  { value: 'resolved', label: 'Resolved' },
  { value: 'false_positive', label: 'False positive' },
  { value: 'duplicate', label: 'Duplicate' },
  { value: 'unable_to_resolve', label: 'Unable to resolve' },
] as const

type ClosureCategory = (typeof CLOSURE_CATEGORIES)[number]['value']

function EditCaseDialog({ open, c, onClose, onSaved }: { open: boolean; c: CaseRow | null; onClose: () => void; onSaved: () => void }) {
  const [title, setTitle] = useState('')
  const [priority, setPriority] = useState('medium')
  const [status, setStatus] = useState('open')
  const [assignee, setAssignee] = useState('')
  const [description, setDescription] = useState('')
  const [category, setCategory] = useState<ClosureCategory>('resolved')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  useEffect(() => {
    if (open && c) {
      setTitle(c.title)
      setPriority(c.prio)
      setStatus(c.status)
      setAssignee(c.ownerName && c.ownerName !== '—' ? c.ownerName : '')
      setDescription(c.desc || '')
      setCategory('resolved')
      setReason('')
      setErr('')
    }
  }, [open, c])

  // Only a Case crossing into closed is being closed. Re-saving one that is
  // already closed is an edit, and re-stamping it would move the closure's date
  // and re-derive its verdict for a typo fix.
  const closing = status === 'closed' && c?.status !== 'closed'

  const submit = async () => {
    if (!c) return
    if (!title.trim()) { setErr('Title is required.'); return }
    // The determination the epic exists for. "It is benign" with nothing behind it
    // is the one claim nobody can weigh later, so it is the one reason required.
    if (closing && category === 'false_positive' && !reason.trim()) {
      setErr('Say why this is a false positive — it becomes the reason on the record.')
      return
    }
    setBusy(true)
    setErr('')
    try {
      // The status is left off when closing: the close endpoint sets it, and a
      // PATCH carrying it too would stamp a second, unstated closure over the
      // category chosen here.
      await casesApi.update(c.id, {
        title: title.trim(),
        priority,
        ...(closing ? {} : { status }),
        assignee: assignee.trim() || undefined,
        description: description.trim() || undefined,
      })
      // Second, so a close that fails leaves the edits saved and the Case open,
      // rather than closed under a title that never landed.
      if (closing) {
        const said = reason.trim() || undefined
        await casesApi.closeCase(c.id, {
          closure_category: category,
          ...(category === 'false_positive'
            ? { false_positive_reason: said }
            : { closure_notes: said }),
        })
      }
      onSaved()
      onClose()
    } catch (e) {
      setErr((e as { message?: string })?.message || 'Failed to save changes')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Popup open={open} onClose={onClose} title="Edit case" width={520}>
      <div className="flex flex-col gap-3.5">
        <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
          <span>Title</span>
          <input className={inputCls} value={title} onChange={(e) => setTitle(e.target.value)} autoFocus />
        </label>
        <div className="grid grid-cols-2 gap-3">
          <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
            <span>Priority</span>
            <Select value={priority} onSelect={setPriority} options={CASE_PRIO_OPTIONS} />
          </label>
          <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
            <span>Status</span>
            <Select value={status} onSelect={setStatus} options={[
              { value: 'open', label: 'Open' }, { value: 'investigating', label: 'Investigating' }, { value: 'closed', label: 'Closed' },
            ]} />
          </label>
        </div>
        {closing && (
          <>
            <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
              <span>What it turned out to be</span>
              <Select value={category} onSelect={(v) => setCategory(v as ClosureCategory)} options={[...CLOSURE_CATEGORIES]} />
            </label>
            <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
              <span>{category === 'false_positive' ? 'Why it is benign' : 'Reason'}</span>
              <textarea
                className={inputCls}
                rows={3}
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder={category === 'false_positive'
                  ? 'The scanner runs from that host every Sunday…'
                  : 'Optional — what a reader should know about how this ended.'}
                style={{ resize: 'vertical' }}
              />
              <span className="text-[11.5px] font-normal normal-case tracking-normal text-tx-3 leading-[1.45]">
                {category === 'duplicate'
                  ? 'Recorded on the case as bookkeeping. A duplicate is not a finding about the estate, so it adds nothing to memory.'
                  : 'Kept as the reason on the record, and read back as why this case ended.'}
              </span>
            </label>
          </>
        )}
        <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
          <span>Assignee</span>
          <input className={inputCls} placeholder="name or email" value={assignee} onChange={(e) => setAssignee(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
          <span>Description</span>
          <textarea className={inputCls} rows={4} value={description} onChange={(e) => setDescription(e.target.value)} style={{ resize: 'vertical' }} />
        </label>
        {err && <div className="text-[13px]" style={{ color: 'var(--crit)' }}>{err}</div>}
        <div className="flex justify-end gap-2.5">
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn primary" onClick={submit} disabled={busy}>{busy ? 'Saving…' : 'Save changes'}</button>
        </div>
      </div>
    </Popup>
  )
}

function MergeCaseDialog({ open, c, rows, onClose, onMerged }: { open: boolean; c: CaseRow | null; rows: CaseRow[]; onClose: () => void; onMerged: () => void }) {
  const [target, setTarget] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const candidates = useMemo(() => rows.filter((r) => r.id !== c?.id), [rows, c])

  useEffect(() => { if (open) { setTarget(''); setErr('') } }, [open])

  const submit = async () => {
    if (!c) return
    if (!target) { setErr('Select a case to merge into.'); return }
    setBusy(true)
    setErr('')
    try {
      await casesApi.merge(target, c.id)
      onMerged()
      onClose()
    } catch (e) {
      setErr((e as { message?: string })?.message || 'Merge failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Popup open={open} onClose={onClose} title="Merge case" width={520}>
      <div className="flex flex-col gap-3.5">
        <p className="text-[13px] text-tx-2 leading-[1.5] m-0">
          Merge <span className="mono text-tx">{c?.id}</span> into another case. Its linked findings, IOCs and
          evidence move to the target case, and this case is closed.
        </p>
        <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
          <span>Merge into</span>
          <Select
            value={target}
            onSelect={setTarget}
            placeholder="Select target case…"
            options={candidates.map((r) => ({ value: r.id, label: `${r.id} — ${r.title}` }))}
          />
        </label>
        {err && <div className="text-[13px]" style={{ color: 'var(--crit)' }}>{err}</div>}
        <div className="flex justify-end gap-2.5">
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn primary" onClick={submit} disabled={busy}>{busy ? 'Merging…' : 'Merge case'}</button>
        </div>
      </div>
    </Popup>
  )
}

export function CasesDetail({
  id,
  rows,
  onSelect,
  onBack,
  openChat,
  reloadList,
}: {
  id: string
  rows: CaseRow[]
  onSelect: (id: string) => void
  onBack: () => void
  openChat: (prompt?: string) => void
  reloadList: () => void
}) {
  const { row, created, combinedState, investigations, closure, phase, error, reload: reloadDetail } =
    useCaseDetail(id)
  const { hasPermission } = useAuth()
  const canDelete = hasPermission('cases.delete')
  // prefer the freshly-fetched detail; fall back to the list row while it loads
  const c = row || rows.find((x) => x.id === id) || null
  const [listQuery, setListQuery] = useState('')
  const [action, setAction] = useState<'edit' | 'merge' | 'delete' | null>(null)

  const listRows = useMemo(() => {
    const q = listQuery.trim().toLowerCase()
    if (!q) return rows
    return rows.filter(
      (r) =>
        r.id.toLowerCase().includes(q) ||
        r.title.toLowerCase().includes(q) ||
        r.ownerName.toLowerCase().includes(q),
    )
  }, [rows, listQuery])

  return (
    <div className="split">
      <div className="list-pane">
        <div className="flex items-center gap-2 flex-wrap px-[22px] py-[13px] border-b border-line">
          <div className="search" style={{ flex: 1, minWidth: 0 }}>
            <span><Icon name="search" /></span>
            <input
              placeholder="Search cases…"
              value={listQuery}
              onChange={(e) => setListQuery(e.target.value)}
            />
          </div>
        </div>
        <div style={{ overflowY: 'auto', flex: 1, minHeight: 0 }}>
          {listRows.length === 0 && (
            <div className="muted" style={{ padding: '16px 18px', fontSize: 13 }}>
              {rows.length === 0
                ? 'No cases yet. Upload findings or create a case to start case tracking.'
                : 'No cases match your filters.'}
            </div>
          )}
          {listRows.map((cr) => (
            <div
              key={cr.id}
              className={`case-row${cr.id === id ? ' sel' : ''}`}
              onClick={() => onSelect(cr.id)}
            >
              <div className="cr-top">
                <span className="cr-title">{cr.title}</span>
                <span className={`prio ${cr.prio}`} style={{ marginLeft: 'auto' }}>{cr.prio[0].toUpperCase()}</span>
              </div>
              <div className="cr-meta">
                <span className="mono">{cr.id}</span>
                <span className={`status ${cr.status}`}>{cr.status}</span>
                <span style={{ marginLeft: 'auto' }}>{cr.findings} findings</span>
              </div>
            </div>
          ))}
        </div>
      </div>
      <CasePage
        id={id}
        c={c}
        created={created}
        combinedState={combinedState}
        investigations={investigations}
        closure={closure}
        phase={phase}
        error={error}
        openChat={openChat}
        onBack={onBack}
        onEdit={() => setAction('edit')}
        onMerge={() => setAction('merge')}
        onDelete={() => setAction('delete')}
        canDelete={canDelete}
        onChanged={() => { reloadDetail(); reloadList() }}
      />

      <EditCaseDialog
        open={action === 'edit'}
        c={c}
        onClose={() => setAction(null)}
        onSaved={() => { reloadDetail(); reloadList() }}
      />
      <MergeCaseDialog
        open={action === 'merge'}
        c={c}
        rows={rows}
        onClose={() => setAction(null)}
        onMerged={() => { reloadList(); onBack() }}
      />
      <DeleteCaseDialog
        target={action === 'delete' ? c : null}
        onClose={() => setAction(null)}
        onDeleted={() => {
          reloadList()
          onBack()
        }}
      />
    </div>
  )
}
