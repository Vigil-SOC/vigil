/* ============================================================
   Dashboard — tabbed console (Findings · ATT&CK · Timeline · Entity)
   Ported from dashboard.js / attack.js / timeline.js.
   ============================================================ */
import { Fragment, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Icon } from '../../shared/icons'
import { Pie, Hbars } from '../../shared/charts'
import { useFindings, useDashboardKpis } from './useFindings'
import type { Finding } from '../../data/data'
import { agentsApi, findingsApi } from '../../../services/api'
import { notificationService } from '../../../services/notifications'
import { useAttack } from './useAttack'
import { useTimeline } from './useTimeline'
import { FilterButton, FilterGroup, Popup } from '../../shared/ui'
import FindingPopup from './FindingPopup'
import AttackTechniqueFindings from './AttackTechniqueFindings'
import { SEV_COLOR, TL_MONTHS, type TimelineEvent } from './attackData'
import type { ScreenProps } from '../../shared/types'
import EntityGraph, { type GraphLink, type GraphNode } from '../../../components/graph/EntityGraph'

type DashTab = 'findings' | 'attack' | 'timeline' | 'entity'

export default function DashboardScreen({ openChat }: ScreenProps) {
  const [tab, setTab] = useState<DashTab>('findings')
  const tabs: [DashTab, string][] = [
    ['findings', 'Findings'],
    ['attack', 'ATT&CK'],
    ['timeline', 'Timeline'],
    ['entity', 'Entity Graph'],
  ]
  return (
    <>
      <div className="flex items-center gap-3 flex-wrap px-[22px] py-[13px] border-b border-line tabbar">
        <div className="tabs" role="tablist" aria-label="Dashboard views">
          {tabs.map(([k, label]) => (
            <button
              key={k}
              role="tab"
              aria-selected={tab === k}
              className={`tab${tab === k ? ' active' : ''}`}
              onClick={() => setTab(k)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>
      {tab === 'findings' && <FindingsTab openChat={openChat} />}
      {tab === 'attack' && <AttackTab />}
      {tab === 'timeline' && <TimelineTab />}
      {tab === 'entity' && <EntityTab />}
    </>
  )
}

/* ---------------- Findings ---------------- */
const NDASH = '—'

/* Sorting — only columns with a meaningful order are sortable. */
type SortKey = 'sev' | 'time' | 'score' | 'status'
type SortState = { key: SortKey; dir: 'asc' | 'desc' }
const SEV_RANK: Record<Finding['sev'], number> = { Critical: 4, High: 3, Medium: 2, Low: 1 }
const STATUS_RANK: Record<Finding['status'], number> = { open: 0, investigating: 1, closed: 2 }
// status reads best low→high (open first); the rest read best high→low
const DEFAULT_DIR: Record<SortKey, 'asc' | 'desc'> = { sev: 'desc', time: 'desc', score: 'desc', status: 'asc' }

/** comparable time key: findings normally carry epoch-ms `ts`; when it's
 *  missing, fall back to the YYYYMMDD in the id plus HH:MM from the display string */
function timeKey(f: Finding): number {
  if (typeof f.ts === 'number') return f.ts
  const d = /(\d{8})/.exec(f.id)?.[1]
  if (!d) return 0
  const t = /(\d{1,2}):(\d{2})/.exec(f.time)
  return Number(d) * 10000 + (t ? Number(t[1]) * 100 + Number(t[2]) : 0)
}

function sortVal(f: Finding, key: SortKey): number {
  switch (key) {
    case 'sev': return SEV_RANK[f.sev]
    case 'score': return f.score
    case 'time': return timeKey(f)
    case 'status': return STATUS_RANK[f.status]
  }
}

function SortHeader(
  { label, col, sort, onSort }:
  { label: string; col: SortKey; sort: SortState; onSort: (k: SortKey) => void },
) {
  const active = sort.key === col
  return (
    <th className={`sortable${active ? ' sorted' : ''}`} onClick={() => onSort(col)}>
      {label}
      {active && (
        <span className="arr"><Icon name={sort.dir === 'asc' ? 'arrowUp' : 'arrowDn'} size={12} /></span>
      )}
    </th>
  )
}

/** build the "Investigate with Vigil" auto-message for a finding */
function findingPrompt(f: Finding): string {
  const parts = [`${f.sev} severity`, `MITRE ${f.tech}${f.tactic !== NDASH ? ` (${f.tactic})` : ''}`, `source ${f.src}`]
  if (f.host !== NDASH) parts.push(`host ${f.host}`)
  if (f.user !== NDASH) parts.push(`user ${f.user}`)
  parts.push(`anomaly score ${f.score.toFixed(2)}`)
  return `Investigate finding ${f.id} — ${parts.join(', ')}. What happened and what should I do next?`
}

interface AgentOption {
  id: string
  name: string
  specialization?: string
  description?: string
}

function FindingsTab({ openChat }: { openChat: ScreenProps['openChat'] }) {
  const { rows, phase, error, reload } = useFindings()
  const { kpis, reload: reloadKpis } = useDashboardKpis()
  const [agents, setAgents] = useState<AgentOption[]>([])
  const [agentTarget, setAgentTarget] = useState<Finding | null>(null)
  const [bulkAgentOpen, setBulkAgentOpen] = useState(false)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set())
  const [agentBusy, setAgentBusy] = useState(false)
  const [query, setQuery] = useState('')
  const [sev, setSev] = useState('any')
  const [src, setSrc] = useState('any')
  const [detailId, setDetailId] = useState<string | null>(null)
  const [pageSize, setPageSize] = useState(10)
  const [page, setPage] = useState(1)
  const [sort, setSort] = useState<SortState>({ key: 'time', dir: 'desc' })
  const toggleSort = (key: SortKey) =>
    setSort((s) => (s.key === key
      ? { key, dir: s.dir === 'asc' ? 'desc' : 'asc' }
      : { key, dir: DEFAULT_DIR[key] }))

  useEffect(() => {
    agentsApi
      .listAgents()
      .then((res) => {
        const list = (res.data?.agents || []) as AgentOption[]
        setAgents(list)
      })
      .catch(() => setAgents([]))
  }, [])

  // source options derive from the data
  const srcOptions = useMemo(() => {
    const set = Array.from(new Set(rows.map((f) => f.src).filter((s) => s && s !== '—'))).sort()
    return [{ value: 'any', label: 'Any' }, ...set.map((s) => ({ value: s, label: s }))]
  }, [rows])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    return rows.filter((f) => {
      if (sev !== 'any' && f.sev.toLowerCase() !== sev) return false
      if (src !== 'any' && f.src !== src) return false
      if (!q) return true
      return (
        f.id.toLowerCase().includes(q) ||
        f.tech.toLowerCase().includes(q) ||
        f.host.toLowerCase().includes(q) ||
        f.user.toLowerCase().includes(q) ||
        f.src.toLowerCase().includes(q)
      )
    })
  }, [rows, query, sev, src])

  const sorted = useMemo(() => {
    const { key, dir } = sort
    return [...filtered].sort((a, b) => {
      const d = sortVal(a, key) - sortVal(b, key)
      return dir === 'asc' ? d : -d
    })
  }, [filtered, sort])

  // reset to the first page whenever the filtered set changes shape
  // (re-sorting keeps the same rows, so it doesn't reset the page)
  useEffect(() => { setPage(1) }, [query, sev, src, pageSize])

  const pageCount = Math.max(1, Math.ceil(sorted.length / pageSize))
  const safePage = Math.min(page, pageCount)
  const start = (safePage - 1) * pageSize
  const paged = sorted.slice(start, start + pageSize)
  const rangeLabel = sorted.length === 0
    ? '0 of 0'
    : `${start + 1}–${start + paged.length} of ${sorted.length}`

  const refresh = () => {
    reload()
    reloadKpis()
    setSelectedIds(new Set())
  }

  const kpi = (n: number | undefined) => (kpis ? String(n ?? 0) : NDASH)
  const visibleIds = paged.map((f) => f.id)
  const allVisibleSelected = visibleIds.length > 0 && visibleIds.every((id) => selectedIds.has(id))
  const selectedFindings = sorted.filter((f) => selectedIds.has(f.id))

  const toggleSelected = (findingId: string) => {
    setSelectedIds((cur) => {
      const next = new Set(cur)
      if (next.has(findingId)) next.delete(findingId)
      else next.add(findingId)
      return next
    })
  }

  const toggleVisibleSelected = () => {
    setSelectedIds((cur) => {
      const next = new Set(cur)
      if (allVisibleSelected) visibleIds.forEach((id) => next.delete(id))
      else visibleIds.forEach((id) => next.add(id))
      return next
    })
  }

  const startFindingInvestigation = async (finding: Finding, agentId?: string) => {
    const agent = agents.find((a) => a.id === agentId)
    setAgentBusy(true)
    try {
      let prompt = findingPrompt(finding)
      if (agentId) {
        const res = await agentsApi.startInvestigation({
          finding_id: finding.id,
          agent_id: agentId,
        })
        prompt = res.data?.prompt || prompt
      }
      openChat(prompt, { agentId })
      notificationService.notifyGeneric(
        'Investigation started',
        `${agent?.name || 'Vigil'} is investigating ${finding.id.substring(0, 12)}…`,
        { severity: 'success' },
      )
    } catch (e) {
      notificationService.notifyGeneric(
        'Investigation failed',
        (e as { message?: string })?.message || 'Could not start the agent.',
        { severity: 'error' },
      )
    } finally {
      setAgentBusy(false)
      setAgentTarget(null)
    }
  }

  const startBulkInvestigation = (agentId?: string) => {
    if (selectedFindings.length === 0) return
    const agent = agents.find((a) => a.id === agentId)
    const prompt = [
      `Investigate these ${selectedFindings.length} selected findings as a correlated incident.`,
      'Focus on shared entities, timeline order, likely root cause, and next actions.',
      '',
      ...selectedFindings.slice(0, 25).map((f) => `- ${findingPrompt(f)}`),
    ].join('\n')
    openChat(prompt, { agentId })
    notificationService.notifyGeneric(
      'Bulk investigation started',
      `${agent?.name || 'Vigil'} is correlating ${selectedFindings.length} findings.`,
      { severity: 'success' },
    )
  }

  const exportFindings = async () => {
    try {
      const res = await findingsApi.export('json')
      notificationService.notifyGeneric(
        'Findings exported',
        res.data?.file_path || 'Export complete.',
        { severity: 'success' },
      )
    } catch (e) {
      notificationService.notifyGeneric(
        'Export failed',
        (e as { message?: string })?.message || 'Could not export findings.',
        { severity: 'error' },
      )
    }
  }

  return (
    <>
      <div className="grid grid-cols-4 border-b border-line">
        <div className="relative flex flex-col gap-[3px] px-[22px] py-4 border-r border-line-soft last:border-r-0">
          <span className="text-[11px] font-semibold tracking-[0.07em] uppercase text-tx-3 truncate">Total Findings</span>
          <div className="flex items-baseline gap-2.5"><span className="text-[30px] font-semibold tracking-[-0.02em] leading-[1.1]">{kpi(kpis?.findingsTotal)}</span></div>
          <span className="text-xs text-tx-faint">{kpis ? `${kpis.findingsCritical} critical · ${kpis.findingsHigh} high` : ' '}</span>
        </div>
        <div className="relative flex flex-col gap-[3px] px-[22px] py-4 border-r border-line-soft last:border-r-0">
          <span className="text-[11px] font-semibold tracking-[0.07em] uppercase text-tx-3 truncate">Active Cases</span>
          <div className="flex items-baseline gap-2.5"><span className="text-[30px] font-semibold tracking-[-0.02em] leading-[1.1]">{kpi(kpis?.casesTotal)}</span></div>
          <span className="text-xs text-tx-faint">{kpis ? `${kpis.casesOpen} open · ${kpis.casesInvestigating} investigating` : ' '}</span>
        </div>
        <div className="relative flex flex-col gap-[3px] px-[22px] py-4 border-r border-line-soft last:border-r-0">
          <span className="text-[11px] font-semibold tracking-[0.07em] uppercase text-tx-3 truncate">Critical Alerts</span>
          <div className="flex items-baseline gap-2.5"><span className="text-[30px] font-semibold tracking-[-0.02em] leading-[1.1] text-crit">{kpi(kpis?.findingsCritical)}</span></div>
          <span className="text-xs text-tx-faint">requires immediate attention</span>
        </div>
        <div className="relative flex flex-col gap-[3px] px-[22px] py-4 border-r border-line-soft last:border-r-0">
          <span className="text-[11px] font-semibold tracking-[0.07em] uppercase text-tx-3 truncate">High Priority</span>
          <div className="flex items-baseline gap-2.5"><span className="text-[30px] font-semibold tracking-[-0.02em] leading-[1.1] text-high">{kpi(kpis?.findingsHigh)}</span></div>
          <span className="text-xs text-tx-faint">review within 24h</span>
        </div>
      </div>

      <div className="flex items-center gap-3 flex-wrap px-[22px] py-[13px] border-b border-line">
        <div className="search" style={{ maxWidth: 300 }}>
          <span><Icon name="search" /></span>
          <input aria-label="Search findings" placeholder="Search findings, hosts, techniques…" value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
        <FilterButton
          activeCount={(sev !== 'any' ? 1 : 0) + (src !== 'any' ? 1 : 0)}
          onClearAll={() => { setSev('any'); setSrc('any') }}
        >
          <FilterGroup
            label="Severity"
            value={sev}
            onSelect={setSev}
            options={[
              { value: 'any', label: 'Any' },
              { value: 'critical', label: 'Critical' },
              { value: 'high', label: 'High' },
              { value: 'medium', label: 'Medium' },
              { value: 'low', label: 'Low' },
            ]}
          />
          <FilterGroup label="Source" value={src} onSelect={setSrc} options={srcOptions} />
        </FilterButton>
        <div className="flex-1" />
        {selectedIds.size > 0 && (
          <>
            <button className="btn ghost" onClick={() => setBulkAgentOpen(true)}>
              <Icon name="brain" /> Investigate {selectedIds.size}
            </button>
            <button className="btn ghost" onClick={() => setSelectedIds(new Set())}>
              Clear selection
            </button>
          </>
        )}
        <button className="btn ghost icon" title="Refresh" onClick={refresh}><Icon name="refresh" /></button>
        <button className="btn primary" onClick={exportFindings}><Icon name="download" /> Export</button>
      </div>

      <div className="table-wrap list-scroll list-scroll-kpi">
        <table className="tbl findings-tbl">
          <thead>
            <tr>
              <th>
                <input
                  type="checkbox"
                  aria-label="Select visible findings"
                  checked={allVisibleSelected}
                  onChange={toggleVisibleSelected}
                />
              </th>
              <th>Finding ID</th>
              <SortHeader label="Severity" col="sev" sort={sort} onSort={toggleSort} />
              <th>MITRE Technique</th><th>Tactic</th>
              <th>Source</th><th>Host</th><th>User</th>
              <SortHeader label="Time" col="time" sort={sort} onSort={toggleSort} />
              <SortHeader label="Score" col="score" sort={sort} onSort={toggleSort} />
              <SortHeader label="Status" col="status" sort={sort} onSort={toggleSort} />
              <th />
            </tr>
          </thead>
          <tbody>
            {phase === 'loading' && (
              <tr><td colSpan={12} className="muted" style={{ textAlign: 'center', padding: '40px 0' }}>Loading findings…</td></tr>
            )}
            {phase === 'error' && (
              <tr><td colSpan={12} className="muted" style={{ textAlign: 'center', padding: '40px 0' }}>
                <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 10 }}>
                  <span>Couldn’t load findings: {error}</span>
                  <button className="btn ghost" onClick={refresh}>Retry</button>
                </div>
              </td></tr>
            )}
            {phase === 'ready' && filtered.length === 0 && (
              <tr><td colSpan={12} className="muted" style={{ textAlign: 'center', padding: '40px 0' }}>
                {rows.length === 0 ? 'No findings found.' : 'No findings match your filters.'}
              </td></tr>
            )}
            {phase === 'ready' && paged.map((f) => (
              <tr key={f.id} className="clickable" onClick={() => setDetailId(f.id)}>
                <td>
                  <input
                    type="checkbox"
                    aria-label={`Select ${f.id}`}
                    checked={selectedIds.has(f.id)}
                    onClick={(e) => e.stopPropagation()}
                    onChange={() => toggleSelected(f.id)}
                  />
                </td>
                <td><span className="id-cell">{f.id}</span></td>
                <td><span className={`sev ${f.sev.toLowerCase()}`}><span className="dot" />{f.sev}</span></td>
                <td><span className="tag">{f.tech}</span> <span className="muted">{f.conf}%</span></td>
                <td>{f.tactic}</td>
                <td className="muted">{f.src}</td>
                <td><span className="mono">{f.host}</span></td>
                <td><span className="mono muted">{f.user}</span></td>
                <td className="muted">{f.time}</td>
                <td>
                  <span className="scorebar">
                    <span className="track"><i className={f.score >= 0.8 ? 'hot' : ''} style={{ width: `${f.score * 100}%` }} /></span>
                    <span className="num">{f.score.toFixed(2)}</span>
                  </span>
                </td>
                <td><span className={`status ${f.status}`}>{f.status}</span></td>
                <td>
                  <span className="row-act">
                    <button title="View" onClick={(e) => { e.stopPropagation(); setDetailId(f.id) }}><Icon name="eye" /></button>
                    <button title="Investigate with agent" onClick={(e) => { e.stopPropagation(); setAgentTarget(f) }}><Icon name="brain" /></button>
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="pager">
        <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          Rows per page:
          <select
            className="pg-size"
            aria-label="Rows per page"
            value={pageSize}
            onChange={(e) => setPageSize(Number(e.target.value))}
          >
            {[10, 25, 50, 100].map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </span>
        <span>{rangeLabel}</span>
        <span style={{ display: 'flex', gap: 6 }}>
          <button
            className="pg-btn"
            disabled={safePage <= 1}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            title="Previous page"
          ><Icon name="chevL" size={14} /></button>
          <button
            className="pg-btn"
            disabled={safePage >= pageCount}
            onClick={() => setPage((p) => Math.min(pageCount, p + 1))}
            title="Next page"
          ><Icon name="chevR" size={14} /></button>
        </span>
      </div>
      <FindingPopup
        id={detailId}
        onClose={() => setDetailId(null)}
        onChanged={() => { reload(); reloadKpis() }}
        onInvestigate={(f) => {
          setDetailId(null)
          setAgentTarget(f)
        }}
      />
      <AgentPicker
        open={agentTarget !== null}
        title={agentTarget ? `Investigate ${agentTarget.id}` : 'Investigate finding'}
        agents={agents}
        busy={agentBusy}
        onClose={() => { if (!agentBusy) setAgentTarget(null) }}
        onSelect={(agentId) => agentTarget && startFindingInvestigation(agentTarget, agentId)}
      />
      <AgentPicker
        open={bulkAgentOpen}
        title="Investigate selected findings"
        agents={agents}
        busy={false}
        onClose={() => setBulkAgentOpen(false)}
        onSelect={(agentId) => {
          startBulkInvestigation(agentId)
          setSelectedIds(new Set())
          setBulkAgentOpen(false)
        }}
      />
    </>
  )
}

function AgentPicker({
  open,
  title,
  agents,
  busy,
  onClose,
  onSelect,
}: {
  open: boolean
  title: string
  agents: AgentOption[]
  busy: boolean
  onClose: () => void
  onSelect: (agentId?: string) => void
}) {
  return (
    <Popup open={open} onClose={onClose} title={title} width={440}>
      <div className="agent-pick-list">
        <button className="agent-pick-row" disabled={busy} onClick={() => onSelect(undefined)}>
          <span className="am-name">Default agent</span>
          <span className="am-spec">Use Vigil’s default routing</span>
        </button>
        {agents.map((a) => (
          <button className="agent-pick-row" key={a.id} disabled={busy} onClick={() => onSelect(a.id)}>
            <span className="am-name">{a.name}</span>
            {a.specialization && <span className="am-spec">{a.specialization}</span>}
            {a.description && <span className="am-desc">{a.description}</span>}
          </button>
        ))}
        {agents.length === 0 && <div className="muted">No agents available.</div>}
      </div>
    </Popup>
  )
}

/* ---------------- ATT&CK ---------------- */
function sevB(n: number, cls: string) {
  return n ? <span className={`scount ${cls}`}>{n}</span> : <span className="scount zero">·</span>
}

function AttackTab() {
  const [range, setRange] = useState('All')
  const [conf, setConf] = useState(0)
  const [expanded, setExpanded] = useState<string | null>(null)
  const { data, phase, error, reload } = useAttack(conf, range)
  const toggle = (id: string) => setExpanded((cur) => (cur === id ? null : id))

  const techniques = data?.techniques ?? []
  const k = data?.kpis ?? { techniques: 0, detections: 0, critical: 0, high: 0 }
  const tacticDist = data?.tacticDist ?? []
  const maxTac = Math.max(1, ...tacticDist.map((t) => t[1]))
  const tac = tacticDist.slice(0, 6).map((t) => ({ label: t[0], val: t[1], pct: Math.round((t[1] / maxTac) * 100) }))
  const sevList = data?.sevDist ?? []
  const sevTotal = sevList.reduce((a, s) => a + s[1], 0)
  const sevSegs = sevList.map((s) => ({ v: sevTotal ? s[1] / sevTotal : 0, color: s[2] }))

  return (
    <>
      <div className="grid grid-cols-4 border-b border-line">
        <div className="relative flex flex-col gap-[3px] px-[22px] py-4 border-r border-line-soft last:border-r-0"><span className="text-[11px] font-semibold tracking-[0.07em] uppercase text-tx-3 truncate">Unique Techniques</span><div className="flex items-baseline gap-2.5"><span className="text-[30px] font-semibold tracking-[-0.02em] leading-[1.1]">{k.techniques}</span></div><span className="text-xs text-tx-faint">observed across findings</span></div>
        <div className="relative flex flex-col gap-[3px] px-[22px] py-4 border-r border-line-soft last:border-r-0"><span className="text-[11px] font-semibold tracking-[0.07em] uppercase text-tx-3 truncate">Total Detections</span><div className="flex items-baseline gap-2.5"><span className="text-[30px] font-semibold tracking-[-0.02em] leading-[1.1]">{k.detections}</span></div><span className="text-xs text-tx-faint">mapped to ATT&CK</span></div>
        <div className="relative flex flex-col gap-[3px] px-[22px] py-4 border-r border-line-soft last:border-r-0"><span className="text-[11px] font-semibold tracking-[0.07em] uppercase text-tx-3 truncate">Critical Severity</span><div className="flex items-baseline gap-2.5"><span className="text-[30px] font-semibold tracking-[-0.02em] leading-[1.1] text-crit">{k.critical}</span></div><span className="text-xs text-tx-faint">detections by severity</span></div>
        <div className="relative flex flex-col gap-[3px] px-[22px] py-4 border-r border-line-soft last:border-r-0"><span className="text-[11px] font-semibold tracking-[0.07em] uppercase text-tx-3 truncate">High Severity</span><div className="flex items-baseline gap-2.5"><span className="text-[30px] font-semibold tracking-[-0.02em] leading-[1.1] text-high">{k.high}</span></div><span className="text-xs text-tx-faint">detections by severity</span></div>
      </div>

      <div className="flex items-center gap-3 flex-wrap px-[22px] py-[13px] border-b border-line">
        <span className="bar-cap">Time range</span>
        <div className="range-tabs">
          {['24h', '7d', '30d', 'All'].map((r) => (
            <button key={r} className={range === r ? 'active' : ''} onClick={() => setRange(r)}>{r}</button>
          ))}
        </div>
        <div className="flex-1" />
        <div className="conf-ctrl">
          <span className="bar-cap">Min confidence</span>
          <input type="range" min={0} max={0.99} step={0.01} value={conf} className="conf-range" aria-label="Minimum confidence threshold" onChange={(e) => setConf(parseFloat(e.target.value))} />
          <span className="mono" style={{ color: 'var(--tx-2)', fontSize: '12.5px' }}>{conf.toFixed(2)}</span>
        </div>
        <button className="btn ghost icon" title="Refresh" onClick={reload}><Icon name="refresh" /></button>
      </div>

      {/* charts row — tactics distribution + severity split, side by side */}
      <div className="flex gap-4 items-stretch px-[22px] pt-5 pb-4">
        <div className="bg-panel border border-line rounded-lg shadow-panel overflow-hidden flex-[1.4] min-w-0 flex flex-col">
          <div className="flex items-center gap-2.5 px-[18px] py-[15px] border-b border-line-soft"><h3 className="text-[14.5px]">Tactics distribution</h3></div>
          <div className="p-[18px] overflow-x-hidden flex-1"><Hbars items={tac} /></div>
        </div>
        <div className="bg-panel border border-line rounded-lg shadow-panel overflow-hidden flex-1 min-w-[300px] flex flex-col">
          <div className="flex items-center gap-2.5 px-[18px] py-[15px] border-b border-line-soft"><h3 className="text-[14.5px]">Severity split</h3></div>
          <div className="p-[18px] overflow-x-hidden flex-1 flex items-center justify-center gap-6 flex-wrap">
            <Pie segs={sevSegs} size={220} />
            <div className="legend">
              {sevList.map((s) => (
                <div className="li" key={s[0]}><span className="sw" style={{ background: s[2] }} />{s[0]}<span className="v">{s[1]}</span></div>
              ))}
              <div className="li li-total"><span className="sw" style={{ background: 'transparent' }} />Total<span className="v">{sevTotal}</span></div>
            </div>
          </div>
        </div>
      </div>

      {/* full-width techniques table — the deep-dive */}
      <div className="px-[22px] pb-6">
        <div className="bg-panel border border-line rounded-lg shadow-panel overflow-hidden">
          <div className="flex items-center gap-2.5 px-[18px] py-[15px] border-b border-line-soft">
            <h3 className="text-[14.5px]">Techniques by occurrence</h3>
            <span className="flex-1" />
            <span className="text-xs text-tx-3">{techniques.length} techniques · click a row for findings</span>
          </div>
          <div className="table-wrap list-scroll list-scroll-attack">
            <table className="tbl attack-tbl">
              <thead>
                <tr>
                  <th>ID</th><th>Name</th><th>Tactic</th>
                  <th>Total</th><th>Critical</th><th>High</th><th>Medium</th><th>Low</th><th />
                </tr>
              </thead>
              <tbody>
                {phase === 'loading' && (
                  <tr><td colSpan={9} className="muted" style={{ textAlign: 'center', padding: '40px 0' }}>Loading techniques…</td></tr>
                )}
                {phase === 'error' && (
                  <tr><td colSpan={9} className="muted" style={{ textAlign: 'center', padding: '40px 0' }}>
                    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 10 }}>
                      <span>Couldn’t load ATT&CK data: {error}</span>
                      <button className="btn ghost" onClick={reload}>Retry</button>
                    </div>
                  </td></tr>
                )}
                {phase === 'ready' && techniques.length === 0 && (
                  <tr><td colSpan={9} className="muted" style={{ textAlign: 'center', padding: '40px 0' }}>No techniques at this confidence threshold.</td></tr>
                )}
                {phase === 'ready' && techniques.map((t) => (
                  <Fragment key={t.id}>
                    <tr className={`clickable${expanded === t.id ? ' expanded' : ''}`} onClick={() => toggle(t.id)}>
                      <td><span className="id-cell">{t.id}</span></td>
                      <td>{t.name}</td>
                      <td><span className="tactic-chip">{t.tactic}</span></td>
                      <td><span className="tot-badge">{t.total}</span></td>
                      <td>{sevB(t.c, 'c')}</td><td>{sevB(t.h, 'h')}</td><td>{sevB(t.m, 'm')}</td><td>{sevB(t.l, 'l')}</td>
                      <td>
                        <span className="row-act">
                          <button title={expanded === t.id ? 'Hide findings' : 'Show findings'} onClick={(e) => { e.stopPropagation(); toggle(t.id) }}>
                            <span className="caret" style={{ transform: expanded === t.id ? 'rotate(180deg)' : undefined }}><Icon name="chevD" size={14} /></span>
                          </button>
                        </span>
                      </td>
                    </tr>
                    {expanded === t.id && (
                      <tr className="tech-expand"><td colSpan={9}><AttackTechniqueFindings techniqueId={t.id} /></td></tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </>
  )
}

/* ---------------- Entity Graph ---------------- */
function graphNodeId(type: GraphNode['type'], value: string) {
  return `${type}:${value}`
}

function entityTypeLabel(type: GraphNode['type']) {
  switch (type) {
    case 'hostname': return 'Hosts'
    case 'user': return 'Users'
    case 'domain': return 'Sources'
    default: return type
  }
}

function EntityTab() {
  const { rows, phase, error, reload } = useFindings()
  const graph = useMemo(() => {
    const nodes = new Map<string, GraphNode>()
    const links = new Map<string, GraphLink>()

    const addNode = (type: GraphNode['type'], value: string, finding: Finding) => {
      if (!value || value === NDASH) return undefined
      const id = graphNodeId(type, value)
      const existing = nodes.get(id)
      const severity = finding.sev.toLowerCase() as GraphNode['severity']
      if (existing) {
        existing.findingCount = (existing.findingCount || 0) + 1
        if (!existing.severity || SEV_RANK[finding.sev] > SEV_RANK[(existing.severity[0].toUpperCase() + existing.severity.slice(1)) as Finding['sev']]) {
          existing.severity = severity
        }
        return id
      }
      nodes.set(id, {
        id,
        label: value,
        type,
        severity,
        findingCount: 1,
        metadata: { firstFindingId: finding.id, source: finding.src },
      })
      return id
    }

    const addLink = (source: string | undefined, target: string | undefined, finding: Finding) => {
      if (!source || !target || source === target) return
      const [a, b] = source < target ? [source, target] : [target, source]
      const key = `${a}->${b}`
      const existing = links.get(key)
      if (existing) {
        existing.value = (existing.value || 1) + 1
        existing.techniques = Array.from(new Set([...(existing.techniques || []), finding.tech]))
        return
      }
      links.set(key, {
        source: a,
        target: b,
        value: 1,
        label: finding.id,
        techniques: [finding.tech],
      })
    }

    rows.forEach((finding) => {
      const host = addNode('hostname', finding.host, finding)
      const user = addNode('user', finding.user, finding)
      const source = addNode('domain', finding.src, finding)
      addLink(host, user, finding)
      addLink(host, source, finding)
      addLink(user, source, finding)
    })

    return { nodes: Array.from(nodes.values()), links: Array.from(links.values()) }
  }, [rows])

  const counts = useMemo(() => {
    const byType = new Map<GraphNode['type'], number>()
    graph.nodes.forEach((node) => byType.set(node.type, (byType.get(node.type) || 0) + 1))
    return Array.from(byType.entries()).sort(([a], [b]) => a.localeCompare(b))
  }, [graph.nodes])

  if (phase === 'loading') {
    return <div className="entity-empty"><h3>Loading entity graph…</h3></div>
  }

  if (phase === 'error') {
    return (
      <div className="entity-empty">
        <h3>Entity graph unavailable</h3>
        <p>{error || 'Could not load findings for graph analysis.'}</p>
        <button className="btn ghost" onClick={reload}>Retry</button>
      </div>
    )
  }

  if (graph.nodes.length === 0) {
    return (
      <div className="entity-empty">
        <h3>No entities found</h3>
        <p>Load findings with host, user, or source fields to build the relationship graph.</p>
      </div>
    )
  }

  return (
    <div className="entity-tab">
      <div className="entity-summary">
        <div>
          <h3>Entity relationships</h3>
          <span className="muted">{graph.nodes.length} entities · {graph.links.length} relationships · {rows.length} findings</span>
        </div>
        <div className="entity-counts">
          {counts.map(([type, count]) => (
            <span key={type}>{entityTypeLabel(type)} <b>{count}</b></span>
          ))}
        </div>
      </div>
      <EntityGraph nodes={graph.nodes} links={graph.links} height="calc(100vh - 255px)" maxNodes={500} />
    </div>
  )
}

/* ---------------- Timeline (interactive Gantt) ---------------- */
const DAY = 86400000

interface TLBar {
  e: TimelineEvent
  left: number
  w: number
  top: number
  label: string
}
interface TLLayout {
  min: number
  max: number
  pxPerDay: number
  innerW: number
  plotH: number
  bars: TLBar[]
  ticks: { px: number; lab: string }[]
  grids: { px: number; h: number }[]
  months: { px: number; lab: string }[]
}

function computeLayout(events: TimelineEvent[], zoom: number, containerW: number): TLLayout {
  if (events.length === 0) {
    return { min: 0, max: 1, pxPerDay: 1, innerW: containerW || 800, plotH: 200, bars: [], ticks: [], grids: [], months: [] }
  }
  const times = events.map((e) => e.t)
  const min = Math.min(...times) - DAY * 0.6
  const max = Math.max(...times) + DAY * 0.6
  const spanDays = (max - min) / DAY
  const cw = containerW || 800
  const pxPerDay = ((cw - 32) / spanDays) * zoom
  const x = (t: number) => ((t - min) / DAY) * pxPerDay + 16
  const innerW = spanDays * pxPerDay + 32

  const laneEnds: number[] = []
  const rowH = 22
  const gap = 8
  const topPad = 54
  const botPad = 28
  const bars: TLBar[] = events.map((e) => {
    const label = `${e.sev.toUpperCase()} · ${e.id}`
    const w = Math.max(150, label.length * 6.5 + 26)
    const left = x(e.t)
    let lane = 0
    while (laneEnds[lane] !== undefined && laneEnds[lane] > left - 8) lane++
    laneEnds[lane] = left + w
    return { e, left, w, lane: lane, label, top: 54 + lane * (rowH + gap) }
  })
  const laneCount = Math.max(laneEnds.length, 1)
  const plotH = topPad + laneCount * (rowH + gap) + botPad

  const stepDays = Math.max(1, Math.ceil(64 / pxPerDay))
  const ticks: { px: number; lab: string }[] = []
  const grids: { px: number; h: number }[] = []
  const d0 = new Date(min)
  d0.setHours(0, 0, 0, 0)
  let t0 = d0.getTime()
  if (t0 < min) t0 += DAY
  const gridH = plotH - 52 - botPad + 14
  for (let t = t0; t <= max; t += DAY * stepDays) {
    const px = x(t)
    const d = new Date(t)
    const lab = `${d.getMonth() + 1}/${d.getDate()}`
    ticks.push({ px, lab })
    grids.push({ px, h: gridH })
  }
  const months: { px: number; lab: string }[] = []
  const mi = new Date(min)
  mi.setDate(1)
  mi.setHours(0, 0, 0, 0)
  for (let mt = mi.getTime(); mt <= max; ) {
    const d = new Date(mt)
    const px = Math.max(x(mt), 14)
    months.push({ px, lab: `${TL_MONTHS[d.getMonth()]} ${d.getFullYear()}` })
    d.setMonth(d.getMonth() + 1)
    mt = d.getTime()
  }
  return { min, max, pxPerDay, innerW, plotH, bars, ticks, grids, months }
}

function TimelineTab() {
  const [filter, setFilter] = useState<'all' | 'finding'>('all')
  const [speed, setSpeed] = useState(1)
  const [zoom, setZoom] = useState(1)
  const [playing, setPlaying] = useState(false)
  const [containerW, setContainerW] = useState(800)
  const [detailId, setDetailId] = useState<string | null>(null)

  const scrollRef = useRef<HTMLDivElement>(null)
  const innerRef = useRef<HTMLDivElement>(null)
  const playheadRef = useRef<HTMLDivElement>(null)
  const rafRef = useRef<number | null>(null)
  const lastRef = useRef(0)
  const playTRef = useRef(0)
  const engagedRef = useRef(false)
  const playingRef = useRef(false)
  const speedRef = useRef(1)
  const downRef = useRef(false)

  const { events: tlEvents, phase: tlPhase } = useTimeline()
  const events = useMemo(() => tlEvents.filter((e) => filter === 'all' || e.kind === 'finding'), [tlEvents, filter])
  const layout = useMemo(() => computeLayout(events, zoom, containerW), [events, zoom, containerW])
  const layoutRef = useRef(layout)
  layoutRef.current = layout

  const positionPlayhead = useCallback(() => {
    const inner = innerRef.current
    const ph = playheadRef.current
    const scroll = scrollRef.current
    const lo = layoutRef.current
    if (!inner || !ph) return
    const px = ((playTRef.current - lo.min) / DAY) * lo.pxPerDay + 16
    ph.style.left = px + 'px'
    ph.style.height = lo.plotH - 44 + 'px'
    ph.style.display = engagedRef.current ? 'block' : 'none'
    inner.classList.toggle('engaged', engagedRef.current)
    if (engagedRef.current) {
      inner.querySelectorAll<HTMLElement>('.tl-bar').forEach((el) => {
        el.classList.toggle('fut', Number(el.dataset.t) > playTRef.current)
      })
    } else {
      inner.querySelectorAll<HTMLElement>('.tl-bar.fut').forEach((el) => el.classList.remove('fut'))
    }
    if (playingRef.current && scroll) {
      const target = px - scroll.clientWidth * 0.5
      scroll.scrollLeft = Math.max(0, Math.min(lo.innerW - scroll.clientWidth, target))
    }
  }, [])

  // measure container width
  useLayoutEffect(() => {
    const el = scrollRef.current
    if (!el) return
    setContainerW(el.clientWidth || 800)
    const ro = new ResizeObserver(() => setContainerW(el.clientWidth || 800))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // reposition whenever the layout changes; snap to start when not engaged
  useEffect(() => {
    if (!engagedRef.current) playTRef.current = layout.min
    positionPlayhead()
  }, [layout, positionPlayhead])

  // cleanup any running animation on unmount
  useEffect(() => () => { if (rafRef.current) cancelAnimationFrame(rafRef.current) }, [])

  const pause = useCallback(() => {
    playingRef.current = false
    setPlaying(false)
    if (rafRef.current) cancelAnimationFrame(rafRef.current)
    rafRef.current = null
  }, [])

  const step = useCallback((ts: number) => {
    if (!playingRef.current) return
    const lo = layoutRef.current
    const dt = (ts - lastRef.current) / 1000
    lastRef.current = ts
    playTRef.current += 4 * speedRef.current * DAY * dt
    if (playTRef.current >= lo.max) {
      playTRef.current = lo.max
      positionPlayhead()
      pause()
      return
    }
    positionPlayhead()
    rafRef.current = requestAnimationFrame(step)
  }, [positionPlayhead, pause])

  const play = useCallback(() => {
    if (playTRef.current >= layoutRef.current.max - 1) playTRef.current = layoutRef.current.min
    engagedRef.current = true
    // honour the OS reduced-motion preference: snap to the end instead of
    // animating the scrub through every frame
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
      playTRef.current = layoutRef.current.max
      positionPlayhead()
      return
    }
    playingRef.current = true
    setPlaying(true)
    lastRef.current = performance.now()
    rafRef.current = requestAnimationFrame(step)
  }, [step, positionPlayhead])

  const togglePlay = () => (playingRef.current ? pause() : play())

  const setFromX = (clientX: number) => {
    const inner = innerRef.current
    const lo = layoutRef.current
    if (!inner) return
    const r = inner.getBoundingClientRect()
    const px = clientX - r.left
    let t = lo.min + ((px - 16) / lo.pxPerDay) * DAY
    t = Math.max(lo.min, Math.min(lo.max, t))
    playTRef.current = t
    engagedRef.current = true
    positionPlayhead()
  }

  const changeFilter = (f: 'all' | 'finding') => {
    pause()
    engagedRef.current = false
    setFilter(f)
  }
  const setZoomF = (f: number) => setZoom((z) => Math.max(1, Math.min(12, z * f)))
  const fit = () => {
    pause()
    engagedRef.current = false
    playTRef.current = layoutRef.current.min
    setZoom(1)
    positionPlayhead()
  }
  const exportCsv = () => {
    const rows = [
      ['id', 'severity', 'technique', 'kind', 'timestamp'],
      ...events.map((e) => [e.id, e.sev, e.tech, e.kind, new Date(e.t).toISOString()]),
    ]
    const csv = rows.map((r) => r.join(',')).join('\n')
    const blob = new Blob([csv], { type: 'text/csv' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = 'timeline-events.csv'
    a.click()
    setTimeout(() => URL.revokeObjectURL(a.href), 1000)
  }

  return (
    <>
      <div className="tl-controls">
        <div className="tl-toggle">
          <button className={filter === 'all' ? 'active' : ''} onClick={() => changeFilter('all')}>All</button>
          <button className={filter === 'finding' ? 'active' : ''} onClick={() => changeFilter('finding')}>finding</button>
        </div>
        <span className="tl-count">{tlPhase === 'loading' ? 'Loading…' : `${events.length} events`}</span>
        <div className="grow" />
        <button className="tl-iconbtn" title="Zoom in" onClick={() => setZoomF(1.5)}><Icon name="zoomIn" /></button>
        <button className="tl-iconbtn" title="Zoom out" onClick={() => setZoomF(1 / 1.5)}><Icon name="zoomOut" /></button>
        <button className="tl-iconbtn" title="Fit all events" onClick={fit}><Icon name="fit" /></button>
        <button className={`tl-iconbtn play${playing ? ' on' : ''}`} title="Play / pause" onClick={togglePlay}>
          <Icon name={playing ? 'pause' : 'play'} />
        </button>
        <div className="tl-seg">
          {[1, 2, 4].map((s) => (
            <button key={s} className={speed === s ? 'active' : ''} onClick={() => { setSpeed(s); speedRef.current = s }}>{s}×</button>
          ))}
        </div>
        <button className="tl-iconbtn" title="Export visible events (CSV)" onClick={exportCsv}><Icon name="download" /></button>
      </div>
      <div className="tl-hint">Drag anywhere to scrub · click a bar to investigate · scroll to pan</div>
      <div className="tl-scroll" ref={scrollRef}>
        <div
          className="tl-inner"
          ref={innerRef}
          style={{ width: layout.innerW, height: layout.plotH }}
          onPointerDown={(e) => {
            if ((e.target as HTMLElement).closest('.tl-bar')) return
            downRef.current = true
            pause()
            setFromX(e.clientX)
            try { innerRef.current?.setPointerCapture(e.pointerId) } catch { /* noop */ }
          }}
          onPointerMove={(e) => { if (downRef.current) setFromX(e.clientX) }}
          onPointerUp={() => { downRef.current = false }}
        >
          {layout.grids.map((g, i) => (
            <div key={`g${i}`} className="tl-grid" style={{ left: g.px, height: g.h }} />
          ))}
          <div className="tl-axis-line" />
          {layout.months.map((m, i) => (
            <div key={`m${i}`} className="tl-month" style={{ left: m.px }}>{m.lab}</div>
          ))}
          {layout.ticks.map((t, i) => (
            <Fragment key={`t${i}`}>
              <div className="tl-tlabel" style={{ left: t.px }}>{t.lab}</div>
              <div className="tl-tlabel bottom" style={{ left: t.px }}>{t.lab}</div>
            </Fragment>
          ))}
          {layout.bars.map((b, i) => (
            <div
              key={i}
              className="tl-bar"
              data-t={b.e.t}
              style={{ left: b.left, top: b.top, width: b.w }}
              title={`${b.e.id} · ${b.e.kind}`}
              onClick={(ev) => { ev.stopPropagation(); if (b.e.kind === 'finding') setDetailId(b.e.id) }}
            >
              <i style={{ background: SEV_COLOR[b.e.sev] }} />
              <span>{b.label}</span>
            </div>
          ))}
          <div className="tl-playhead" ref={playheadRef} style={{ display: 'none' }} />
        </div>
      </div>
      <FindingPopup id={detailId} onClose={() => setDetailId(null)} />
    </>
  )
}
