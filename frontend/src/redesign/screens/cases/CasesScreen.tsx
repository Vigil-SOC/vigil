/* ============================================================
   Cases — A (full-bleed table) → B (master-detail on click)
   Wired to the real backend (casesApi/findingsApi) via useCases /
   useCaseDetail; falls through to loading/empty/error states.
   See REDESIGN_GAPS.md §9.
   ============================================================ */
import { useCallback, useEffect, useMemo, useState, type KeyboardEvent, type ReactNode } from 'react'
import { useSearchParams } from 'react-router-dom'
import { format } from 'date-fns'
import { Icon } from '../../shared/icons'
import { Markdown } from '../../shared/Markdown'
import { timelineApi, caseSearchApi, casesApi, timesketchApi } from '../../../services/api'
import { mapApiCase } from '../../data/mappers'
import type { CaseRow } from '../../data/data'
import type { ScreenProps } from '../../shared/types'
import { useCases, useCaseDetail, type Phase } from './useCases'
import { FilterButton, FilterGroup, Popup, Select } from '../../shared/ui'
import {
  inputCls,
  SectionCard,
  EvidenceCard,
  ResolutionStepsCard,
  TasksCard,
  SLACard,
  CommentsCard,
  WatchersCard,
  IOCsCard,
  RelatedCasesCard,
  AuditLogCard,
  ActivityCard,
} from './CaseSections'

const cap = (s: string) => s[0].toUpperCase() + s.slice(1)

/** build the "Open in Vigil" auto-message for a case */
function casePrompt(c: CaseRow): string {
  const tactic = c.tactic !== '—' ? `, primary tactic ${c.tactic}` : ''
  return `Investigate case ${c.id}: "${c.title}" — ${c.prio} priority, status ${c.status}, ${c.findings} linked findings${tactic}. Summarize the case and recommend next steps.`
}

/* ---------------- table sorting ---------------- */
type SortKey = 'id' | 'title' | 'status' | 'prio' | 'ownerName' | 'findings' | 'tactic' | 'age' | 'sla' | 'updated'
const PRIO_RANK: Record<CaseRow['prio'], number> = { critical: 0, high: 1, medium: 2, low: 3 }

function sortValue(c: CaseRow, key: SortKey): string | number {
  switch (key) {
    case 'findings': return c.findings
    case 'prio': return PRIO_RANK[c.prio]
    case 'updated': return c.updatedTs ?? 0
    case 'age': return c.createdTs ?? 0
    case 'id': return c.id.toLowerCase()
    case 'title': return c.title.toLowerCase()
    case 'status': return c.status
    case 'ownerName': return c.ownerName.toLowerCase()
    case 'tactic': return c.tactic.toLowerCase()
    case 'sla': return c.sla
    default: return ''
  }
}

type SortState = { key: SortKey; dir: 'asc' | 'desc' }
function Th({ label, k, sort, onSort }: { label: string; k: SortKey; sort: SortState; onSort: (k: SortKey) => void }) {
  const active = sort.key === k
  return (
    <th className={`sortable${active ? ' sorted' : ''}`} onClick={() => onSort(k)}>
      {label}{' '}
      <span
        className="arr"
        style={{ opacity: active ? 1 : 0.25, transform: active && sort.dir === 'asc' ? 'rotate(180deg)' : 'none' }}
      >
        <Icon name="arrowDn" size={12} />
      </span>
    </th>
  )
}

/* ---------------- case-detail tabs ---------------- */
type CaseTab = 'Overview' | 'Investigation' | 'Resolution' | 'Collaboration' | 'Details'
const CASE_TABS: CaseTab[] = ['Overview', 'Investigation', 'Resolution', 'Collaboration', 'Details']

type DetailData = ReturnType<typeof useCaseDetail>

/** KPI strip — total findings / critical / high / SLA */
function Metrics({ findings, crit, high, sla }: { findings: number; crit: number; high: number; sla: string }) {
  return (
    <div className="bg-panel border border-line rounded-lg overflow-hidden">
      <div className="kpi-strip">
        <div className="kpi"><div className="k-label">Total findings</div><div className="k-row"><span className="k-val">{findings}</span></div></div>
        <div className="kpi"><div className="k-label">Critical</div><div className="k-row"><span className="k-val crit">{crit}</span></div></div>
        <div className="kpi"><div className="k-label">High</div><div className="k-row"><span className="k-val high">{high}</span></div></div>
        <div className="kpi"><div className="k-label">SLA remaining</div><div className="k-row"><span className="k-val" style={{ fontSize: 18 }}>{sla}</span></div></div>
      </div>
    </div>
  )
}

function CaseDetailsCard({ c, created }: { c: CaseRow | null; created: string }) {
  return (
    <SectionCard title="Case details">
      <div className="p-[18px]">
        <div className="mb-[15px]">
          <div className="text-xs text-tx-3 mb-[5px]">Description</div>
          {c?.desc ? (
            <div className="text-[13px] leading-[1.55]"><Markdown>{c.desc}</Markdown></div>
          ) : (
            <div className="text-[13px] leading-[1.55] text-tx-3">No description provided.</div>
          )}
        </div>
        <div className="kv-grid">
          <span className="k">Status</span><span className="v"><span className={`status ${c?.status ?? 'open'}`}>{c?.status ?? '—'}</span></span>
          <span className="k">Priority</span><span className="v"><span className={`prio ${c?.prio ?? 'medium'}`}>{c ? cap(c.prio) : '—'}</span></span>
          <span className="k">Created</span><span className="v">{created}</span>
          <span className="k">Primary tactic</span><span className="v"><span className="tag">{c?.tactic ?? '—'}</span></span>
          <span className="k">Assignee</span><span className="v">{c?.ownerName ?? '—'}</span>
        </div>
      </div>
    </SectionCard>
  )
}

function FindingsCard({ total, linked, phase }: { total: number; linked: DetailData['linked']; phase: Phase }) {
  return (
    <SectionCard title="Linked findings" count={`${total} total`} wide>
      <div className="table-wrap">
        <table className="tbl">
          <thead><tr><th>Finding ID</th><th>Severity</th><th>Technique</th><th>Time</th></tr></thead>
          <tbody>
            {phase === 'loading' && (
              <tr><td colSpan={4} className="muted" style={{ textAlign: 'center', padding: '24px 0' }}>Loading findings…</td></tr>
            )}
            {phase === 'ready' && linked.length === 0 && (
              <tr><td colSpan={4} className="muted" style={{ textAlign: 'center', padding: '24px 0' }}>No data here.</td></tr>
            )}
            {linked.map((f) => (
              <tr key={f.id}>
                <td><span className="id-cell">{f.id}</span></td>
                <td><span className={`sev ${f.sev.toLowerCase()}`}><span className="dot" />{f.sev}</span></td>
                <td><span className="tag">{f.tech}</span></td>
                <td className="muted">{f.time}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </SectionCard>
  )
}

/* Aggregated case timeline — creation + findings + activities + notes +
   workflow events, from GET /timeline/case/{id} (same source the old UI
   used). Rendered as the lightweight .timeline list, not the vis-timeline. */
type TlEvent = { content: string; start: string; severity?: string | null }
function TimelineCard({ caseId }: { caseId: string }) {
  const [events, setEvents] = useState<TlEvent[]>([])
  const [phase, setPhase] = useState<Phase>('loading')

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    timelineApi
      .getCaseTimeline(caseId)
      .then((r) => {
        if (cancelled) return
        const evs = ((r.data?.events as TlEvent[]) || []).filter((e) => e.content)
        setEvents(evs)
        setPhase('ready')
      })
      .catch(() => !cancelled && setPhase('error'))
    return () => {
      cancelled = true
    }
  }, [caseId])

  const fmt = (s: string) => {
    const d = new Date(s)
    return Number.isNaN(d.getTime()) ? '—' : format(d, 'MMM d · HH:mm')
  }

  return (
    <SectionCard title="Timeline" count={phase === 'ready' ? `${events.length} events` : undefined}>
      <div className="p-[18px]">
        {phase === 'loading' && <div className="muted">Loading timeline…</div>}
        {phase === 'error' && <div className="muted">Couldn’t load the timeline.</div>}
        {phase === 'ready' && events.length === 0 && <div className="muted">No timeline events.</div>}
        {phase === 'ready' && events.length > 0 && (
          <div className="timeline">
            {events.map((e, i) => (
              <div key={i} className={`tl-item${e.severity === 'critical' ? ' crit' : ''}`}>
                <div className="tl-time">{fmt(e.start)}</div>
                <div className="tl-txt">{e.content}</div>
              </div>
            ))}
          </div>
        )}
      </div>
    </SectionCard>
  )
}

export default function CasesScreen({ openChat, setViewFull }: ScreenProps) {
  // the open case lives in a ?case=<id> query param so it's shareable /
  // deep-linkable; no ?case ⇒ show the full-width table.
  const [searchParams, setSearchParams] = useSearchParams()
  const selected = searchParams.get('case')
  const { rows, phase, error, reload } = useCases()

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
    <CasesTable rows={rows} phase={phase} error={error} reload={reload} onSelect={selectCase} />
  )
}

/* small state row spanning the whole table */
function StateRow({ children }: { children: ReactNode }) {
  return (
    <tr>
      <td colSpan={11} className="muted" style={{ textAlign: 'center', padding: '40px 0' }}>
        {children}
      </td>
    </tr>
  )
}

/* ---------------- Full-width table ---------------- */
function CasesTable({
  rows,
  phase,
  error,
  reload,
  onSelect,
}: {
  rows: CaseRow[]
  phase: Phase
  error: string | null
  reload: () => void
  onSelect: (id: string) => void
}) {
  const [query, setQuery] = useState('')
  const [statusF, setStatusF] = useState('any')
  const [prioF, setPrioF] = useState('any')
  const [assigneeF, setAssigneeF] = useState('any')
  const [showAdvanced, setShowAdvanced] = useState(false)
  // server-side full-text results (title/description/IOCs/id + filters);
  // null = not searching, fall back to the client-filtered list
  const [results, setResults] = useState<CaseRow[] | null>(null)
  const [sort, setSort] = useState<{ key: SortKey; dir: 'asc' | 'desc' }>({ key: 'updated', dir: 'desc' })
  const [page, setPage] = useState(0)
  const [pageSize, setPageSize] = useState(10)
  const [newOpen, setNewOpen] = useState(false)

  const toggleSort = (key: SortKey) =>
    setSort((s) => (s.key === key ? { key, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: 'asc' }))

  const assigneeOptions = useMemo(() => {
    const set = Array.from(new Set(rows.map((c) => c.ownerName).filter(Boolean))).sort()
    return [{ value: 'any', label: 'Any' }, ...set.map((a) => ({ value: a, label: a }))]
  }, [rows])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    return rows.filter((c) => {
      if (statusF !== 'any' && c.status !== statusF) return false
      if (prioF !== 'any' && c.prio !== prioF) return false
      if (assigneeF !== 'any' && c.ownerName !== assigneeF) return false
      if (!q) return true
      return (
        c.id.toLowerCase().includes(q) ||
        c.title.toLowerCase().includes(q) ||
        c.ownerName.toLowerCase().includes(q)
      )
    })
  }, [rows, query, statusF, prioF, assigneeF])

  // server search results take over the table when present
  const display = results ?? filtered

  const sorted = useMemo(() => {
    const dir = sort.dir === 'asc' ? 1 : -1
    return [...display].sort((a, b) => {
      const va = sortValue(a, sort.key)
      const vb = sortValue(b, sort.key)
      if (va < vb) return -dir
      if (va > vb) return dir
      return 0
    })
  }, [display, sort])

  const pageCount = Math.max(1, Math.ceil(sorted.length / pageSize))
  const safePage = Math.min(page, pageCount - 1)
  const paged = useMemo(
    () => sorted.slice(safePage * pageSize, safePage * pageSize + pageSize),
    [sorted, safePage, pageSize],
  )

  // jump back to the first page whenever the result set or page size changes
  useEffect(() => { setPage(0) }, [query, statusF, prioF, assigneeF, results, pageSize])

  return (
    <>
      <div className="flex items-center gap-3 flex-wrap px-[22px] py-[13px] border-b border-line">
        <div className="search" style={{ maxWidth: 320 }}>
          <span><Icon name="search" /></span>
          <input
            aria-label="Search cases"
            placeholder="Search cases by title, ID, owner…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <FilterButton
          activeCount={(statusF !== 'any' ? 1 : 0) + (prioF !== 'any' ? 1 : 0) + (assigneeF !== 'any' ? 1 : 0)}
          onClearAll={() => { setStatusF('any'); setPrioF('any'); setAssigneeF('any') }}
        >
          <FilterGroup
            label="Status"
            value={statusF}
            onSelect={setStatusF}
            options={[
              { value: 'any', label: 'Any' },
              { value: 'open', label: 'Open' },
              { value: 'investigating', label: 'Investigating' },
              { value: 'closed', label: 'Closed' },
            ]}
          />
          <FilterGroup
            label="Priority"
            value={prioF}
            onSelect={setPrioF}
            options={[
              { value: 'any', label: 'Any' },
              { value: 'critical', label: 'Critical' },
              { value: 'high', label: 'High' },
              { value: 'medium', label: 'Medium' },
              { value: 'low', label: 'Low' },
            ]}
          />
          <FilterGroup label="Assignee" value={assigneeF} onSelect={setAssigneeF} options={assigneeOptions} />
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
      <div className="table-wrap list-scroll">
        <table className="tbl cases-tbl">
          <thead>
            <tr>
              <Th label="Case ID" k="id" sort={sort} onSort={toggleSort} />
              <Th label="Title" k="title" sort={sort} onSort={toggleSort} />
              <Th label="Status" k="status" sort={sort} onSort={toggleSort} />
              <Th label="Priority" k="prio" sort={sort} onSort={toggleSort} />
              <Th label="Assignee" k="ownerName" sort={sort} onSort={toggleSort} />
              <Th label="Findings" k="findings" sort={sort} onSort={toggleSort} />
              <Th label="Tactic" k="tactic" sort={sort} onSort={toggleSort} />
              <Th label="Age" k="age" sort={sort} onSort={toggleSort} />
              <Th label="SLA" k="sla" sort={sort} onSort={toggleSort} />
              <Th label="Updated" k="updated" sort={sort} onSort={toggleSort} />
              <th />
            </tr>
          </thead>
          <tbody>
            {phase === 'loading' && <StateRow>Loading cases…</StateRow>}
            {phase === 'error' && (
              <StateRow>
                <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 10 }}>
                  <span>Couldn’t load cases: {error}</span>
                  <button className="btn ghost" onClick={reload}>Retry</button>
                </div>
              </StateRow>
            )}
            {phase === 'ready' && sorted.length === 0 && (
              <StateRow>
                {results
                  ? 'No cases match your search.'
                  : rows.length === 0
                    ? 'No cases found.'
                    : 'No cases match your filters.'}
              </StateRow>
            )}
            {phase === 'ready' &&
              paged.map((c) => (
                <tr key={c.id} className="clickable" onClick={() => onSelect(c.id)}>
                  <td><span className="id-cell">{c.id}</span></td>
                  <td className="case-title" title={c.title}>{c.title}</td>
                  <td><span className={`status ${c.status}`}>{c.status}</span></td>
                  <td><span className={`prio ${c.prio}`}>{cap(c.prio)}</span></td>
                  <td><span className="assignee"><span className="avatar">{c.owner}</span><span className="muted">{c.ownerName}</span></span></td>
                  <td><b>{c.findings}</b></td>
                  <td><span className="tag">{c.tactic}</span></td>
                  <td className="muted">{c.age}</td>
                  <td><span className={`sla ${c.slaState}`}>{c.sla}</span></td>
                  <td className="muted">{c.updated}</td>
                  <td><span className="row-act"><button title="Open"><Icon name="arrowR" /></button></span></td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>
      <div className="pager">
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
          Rows per page:
          <select
            className="pg-size"
            value={pageSize}
            onChange={(e) => setPageSize(Number(e.target.value))}
          >
            {[10, 25, 50, 100].map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </span>
        <span>
          {sorted.length === 0
            ? '0 of 0'
            : `${safePage * pageSize + 1}–${Math.min((safePage + 1) * pageSize, sorted.length)} of ${sorted.length}`}
        </span>
        <span style={{ display: 'flex', gap: 6 }}>
          <button className="pg-btn" disabled={safePage <= 0} onClick={() => setPage((p) => Math.max(0, p - 1))}><Icon name="chevL" size={14} /></button>
          <button className="pg-btn" disabled={safePage >= pageCount - 1} onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}><Icon name="chevR" size={14} /></button>
        </span>
      </div>

      <NewCaseDialog open={newOpen} onClose={() => setNewOpen(false)} onCreated={reload} />
    </>
  )
}

/* ---------------- Advanced (server-side) search panel ----------------
   Ports the old CaseSearch: full-text over title/description/IOCs/id plus
   priority / status / assignee / tags / date-range filters, via
   POST /case-search/. Results replace the table list until cleared. */
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
      const cases = (res.data?.cases || []) as Parameters<typeof mapApiCase>[0][]
      onResults(cases.map((c) => mapApiCase(c)))
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
            { value: 'critical', label: 'Critical' },
            { value: 'high', label: 'High' },
            { value: 'medium', label: 'Medium' },
            { value: 'low', label: 'Low' },
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

/* ---------------- New case dialog ----------------
   POST /cases/ with title/priority/status/description (no findings yet);
   refreshes the list on success. */
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
        description: description.trim() || undefined,
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
              options={[
                { value: 'critical', label: 'Critical' },
                { value: 'high', label: 'High' },
                { value: 'medium', label: 'Medium' },
                { value: 'low', label: 'Low' },
              ]}
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

/* ---------------- Edit case dialog ----------------
   PATCH /cases/{id} with the editable fields. */
function EditCaseDialog({ open, c, onClose, onSaved }: { open: boolean; c: CaseRow | null; onClose: () => void; onSaved: () => void }) {
  const [title, setTitle] = useState('')
  const [priority, setPriority] = useState('medium')
  const [status, setStatus] = useState('open')
  const [assignee, setAssignee] = useState('')
  const [description, setDescription] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  useEffect(() => {
    if (open && c) {
      setTitle(c.title)
      setPriority(c.prio)
      setStatus(c.status)
      setAssignee(c.ownerName && c.ownerName !== '—' ? c.ownerName : '')
      setDescription(c.desc || '')
      setErr('')
    }
  }, [open, c])

  const submit = async () => {
    if (!c) return
    if (!title.trim()) { setErr('Title is required.'); return }
    setBusy(true)
    setErr('')
    try {
      await casesApi.update(c.id, {
        title: title.trim(),
        priority,
        status,
        assignee: assignee.trim() || undefined,
        description: description.trim() || undefined,
      })
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
            <Select value={priority} onSelect={setPriority} options={[
              { value: 'critical', label: 'Critical' }, { value: 'high', label: 'High' }, { value: 'medium', label: 'Medium' }, { value: 'low', label: 'Low' },
            ]} />
          </label>
          <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
            <span>Status</span>
            <Select value={status} onSelect={setStatus} options={[
              { value: 'open', label: 'Open' }, { value: 'investigating', label: 'Investigating' }, { value: 'closed', label: 'Closed' },
            ]} />
          </label>
        </div>
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

/* ---------------- Merge case dialog ----------------
   POST /cases/{target}/merge { source_case_id }. The current case is the
   source; its findings/IOCs/evidence move to the chosen target and it closes. */
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

/* ---------------- Export to Timesketch dialog ----------------
   POST /timesketch/export. Requires the Timesketch integration to be
   configured; surfaces the backend error otherwise. */
function ExportTimesketchDialog({ open, c, onClose }: { open: boolean; c: CaseRow | null; onClose: () => void }) {
  const [timeline, setTimeline] = useState('')
  const [sketch, setSketch] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [done, setDone] = useState(false)

  useEffect(() => {
    if (open && c) {
      setTimeline(`${c.id} timeline`)
      setSketch(c.title)
      setErr('')
      setDone(false)
    }
  }, [open, c])

  const submit = async () => {
    if (!c) return
    if (!timeline.trim()) { setErr('Timeline name is required.'); return }
    setBusy(true)
    setErr('')
    try {
      await timesketchApi.exportToTimesketch({
        case_id: c.id,
        timeline_name: timeline.trim(),
        sketch_name: sketch.trim() || undefined,
      })
      setDone(true)
    } catch (e) {
      setErr((e as { message?: string })?.message || 'Export failed — is Timesketch configured?')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Popup open={open} onClose={onClose} title="Export to Timesketch" width={520}>
      {done ? (
        <div className="flex flex-col gap-3.5">
          <div className="text-[13px] text-tx-2">Case <span className="mono text-tx">{c?.id}</span> was exported to Timesketch.</div>
          <div className="flex justify-end"><button className="btn primary" onClick={onClose}>Done</button></div>
        </div>
      ) : (
        <div className="flex flex-col gap-3.5">
          <p className="text-[13px] text-tx-2 leading-[1.5] m-0">Push this case's findings into a Timesketch timeline for forensic analysis.</p>
          <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
            <span>Timeline name</span>
            <input className={inputCls} value={timeline} onChange={(e) => setTimeline(e.target.value)} autoFocus />
          </label>
          <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
            <span>Sketch name <span className="normal-case font-normal text-tx-faint">(new sketch if it doesn't exist)</span></span>
            <input className={inputCls} value={sketch} onChange={(e) => setSketch(e.target.value)} />
          </label>
          {err && <div className="text-[13px]" style={{ color: 'var(--crit)' }}>{err}</div>}
          <div className="flex justify-end gap-2.5">
            <button className="btn ghost" onClick={onClose}>Cancel</button>
            <button className="btn primary" onClick={submit} disabled={busy}>{busy ? 'Exporting…' : 'Export'}</button>
          </div>
        </div>
      )}
    </Popup>
  )
}

/* ---------------- Master-detail split ---------------- */
function CasesDetail({
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
  const { row, created, linked, sev, activities, resolutionSteps, phase, error, reload: reloadDetail } =
    useCaseDetail(id)
  // prefer the freshly-fetched detail; fall back to the list row while it loads
  const c = row || rows.find((x) => x.id === id) || null
  const [tab, setTab] = useState<CaseTab>('Overview')
  const [listQuery, setListQuery] = useState('')
  const [action, setAction] = useState<'edit' | 'merge' | 'export' | null>(null)

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

  // the old CaseDetailDialog's five tabs, re-skinned as redesign cards.
  // Header + tab bar stay pinned; only the active tab's body scrolls.
  const groups: Record<CaseTab, ReactNode> = {
    Overview: (
      <>
        <Metrics findings={c?.findings ?? 0} crit={sev.critical} high={sev.high} sla={c?.sla ?? '—'} />
        <CaseDetailsCard c={c} created={created} />
        <ActivityCard activities={activities} />
      </>
    ),
    Investigation: (
      <>
        <FindingsCard total={c?.findings ?? 0} linked={linked} phase={phase} />
        <TimelineCard caseId={id} />
        <EvidenceCard caseId={id} />
      </>
    ),
    Resolution: (
      <>
        <ResolutionStepsCard steps={resolutionSteps} />
        <TasksCard caseId={id} />
        <SLACard caseId={id} />
      </>
    ),
    Collaboration: (
      <>
        <CommentsCard caseId={id} />
        <WatchersCard caseId={id} />
      </>
    ),
    Details: (
      <>
        <IOCsCard caseId={id} />
        <RelatedCasesCard caseId={id} rows={rows} onSelect={onSelect} />
        <AuditLogCard caseId={id} />
      </>
    ),
  }

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
            <div className="muted" style={{ padding: '16px 18px', fontSize: 13 }}>No cases match.</div>
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
      <div className="detail-pane">
        <div className="detail-head">
          <div className="dh-crumb">
            <button className="back" onClick={onBack}><Icon name="chevL" size={13} /> All cases</button>
            <span>/</span><span className="mono">{id}</span>
          </div>
          {phase === 'error' ? (
            <div className="muted" style={{ padding: '6px 0' }}>Couldn’t load this case: {error}</div>
          ) : c ? (
            <div style={{ display: 'flex', alignItems: 'flex-start', gap: 14 }}>
              <div style={{ flex: 1 }}>
                <h2>{c.title}</h2>
                <div className="dh-meta">
                  <span className={`status ${c.status}`}>{c.status}</span>
                  <span className={`prio ${c.prio}`}>{cap(c.prio)} priority</span>
                  <span><Icon name="clock" size={13} /> SLA {c.sla}</span>
                  <span className="assignee"><span className="avatar">{c.owner}</span>{c.ownerName}</span>
                  <span>{c.findings} linked findings</span>
                </div>
              </div>
              <div className="dh-actions">
                <button className="btn ghost" onClick={() => setAction('edit')}><Icon name="edit" /> Edit</button>
                <button className="btn ghost" onClick={() => setAction('merge')}><Icon name="link" /> Merge</button>
                <button className="btn ghost" onClick={() => setAction('export')}><Icon name="download" /> Timesketch</button>
                <button className="btn primary to-vigil-case" onClick={() => openChat(casePrompt(c))}><Icon name="brain" /> Open in Vigil</button>
              </div>
            </div>
          ) : (
            <div className="muted" style={{ padding: '6px 0' }}>Loading case…</div>
          )}
        </div>
        <nav className="detail-tabs" role="tablist" aria-label="Case detail sections">
          {CASE_TABS.map((t) => (
            <button
              key={t}
              role="tab"
              aria-selected={tab === t}
              className={`tab${tab === t ? ' active' : ''}`}
              onClick={() => setTab(t)}
            >
              {t}
            </button>
          ))}
        </nav>
        <div className="detail-body" key={tab}>
          {groups[tab]}
        </div>
      </div>

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
      <ExportTimesketchDialog
        open={action === 'export'}
        c={c}
        onClose={() => setAction(null)}
      />
    </div>
  )
}
