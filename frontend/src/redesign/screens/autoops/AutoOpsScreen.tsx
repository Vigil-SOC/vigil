/* ============================================================
   Auto Ops — Autonomous Operations console. Redesign port of the
   legacy Orchestrator page: live orchestrator status + the
   investigation queue, the enable/scan/kill controls, and a
   full-bleed investigation detail (Overview · Files · Reasoning ·
   Chain of custody). Wired to the real orchestratorApi via
   useAutoOps; the rail item is always present (no feature gate),
   so it can no longer "disappear" when the orchestrator is off.
   ============================================================ */
import { useEffect, useState, type ReactNode } from 'react'
import { Icon } from '../../shared/icons'
import { Toggle, NumberInput } from '../../shared/ui'
import type { ScreenProps } from '../../shared/types'
import { useAutoOps, type Investigation, type OrchestratorStatus } from './useAutoOps'
import { StatusBadge } from './statusBadge'
import InvestigationDetail from './InvestigationDetail'

/** clickable KPI → the investigation statuses it filters the table to */
interface KpiDef {
  key: string
  label: string
  statuses: string[]
  value: (s: OrchestratorStatus) => number
  color?: string
  note?: string
}

const KPIS: KpiDef[] = [
  { key: 'active', label: 'Active Agents', statuses: ['assigned', 'executing'], value: (s) => s.active_agents, color: 'var(--med)', note: 'running now' },
  { key: 'queued', label: 'Queued', statuses: ['queued'], value: (s) => s.queued, note: 'waiting for a slot' },
  { key: 'review', label: 'Pending Review', statuses: ['review_submitted'], value: (s) => s.pending_review, color: 'var(--high)', note: 'awaiting a human' },
  { key: 'done', label: 'Completed', statuses: ['completed'], value: (s) => s.completed, color: 'var(--ok)', note: 'this session' },
  { key: 'failed', label: 'Failed', statuses: ['failed'], value: (s) => s.failed, color: 'var(--crit)', note: 'errored out' },
]

export default function AutoOpsScreen({ openChat, setViewFull }: ScreenProps) {
  const {
    status, investigations, phase, error, notice, busy,
    reload, clearError, clearNotice,
    toggleEnabled, killAll, setMaxAgents, scanFindings, wake, killInvestigation, review,
  } = useAutoOps()
  const [statusFilter, setStatusFilter] = useState<string[] | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)

  // a selected investigation takes over the full-bleed view; clearing the
  // selection (Back) returns to the queue. SocConsole resets viewFull on
  // screen change, so we only need to drive it from the selection here.
  useEffect(() => {
    setViewFull(!!selectedId)
  }, [selectedId, setViewFull])

  if (selectedId) {
    return (
      <InvestigationDetail
        id={selectedId}
        onBack={() => setSelectedId(null)}
        openChat={openChat}
        busy={busy}
        wake={wake}
        killInvestigation={killInvestigation}
        review={review}
      />
    )
  }

  if (!status && phase === 'loading') {
    return <div className="text-sm text-tx-3 py-20 text-center">Loading autonomous operations…</div>
  }
  if (!status && phase === 'error') {
    return (
      <div className="py-20 text-center">
        <div className="flex flex-col items-center gap-2.5">
          <span className="text-sm text-tx-3">Couldn’t load autonomous operations: {error}</span>
          <button className="btn ghost" onClick={reload}>Retry</button>
        </div>
      </div>
    )
  }
  if (!status) return <></>

  const cost = status.cost || {
    total_cost_usd: 0, active_cost_usd: 0, hourly_cost_usd: 0, hourly_budget_remaining: 0, per_investigation_limit: 0,
  }
  const filtered = statusFilter
    ? investigations.filter((inv) => statusFilter.includes(inv.status))
    : investigations
  const filterActive = (k: string[]) => !!statusFilter && statusFilter.join(',') === k.join(',')
  const toggleFilter = (k: string[]) =>
    setStatusFilter((cur) => (cur && cur.join(',') === k.join(',') ? null : k))

  return (
    <>
      {/* ---------- control bar ---------- */}
      <div className="bar-row">
        <div className="flex items-center gap-2.5">
          <Toggle
            checked={status.enabled}
            onChange={() => toggleEnabled()}
            disabled={busy === 'toggle'}
            label="Autonomous operations"
          />
          <span className="text-[13px] font-medium" style={{ color: status.enabled ? 'var(--ok)' : 'var(--tx-3)' }}>
            {status.enabled ? 'Enabled' : 'Disabled'}
          </span>
        </div>

        <label className="flex items-center gap-2 text-[11px] font-semibold tracking-[0.06em] uppercase text-tx-3">
          Max agents
          <NumberInput
            value={status.max_concurrent_agents}
            min={1}
            max={10}
            disabled={busy === 'maxAgents'}
            onChange={(e) => setMaxAgents(Number(e.target.value))}
            style={{ width: 66, textAlign: 'center' }}
          />
        </label>

        <span className="grow" />

        <button
          className="btn ghost"
          disabled={busy === 'scan'}
          onClick={scanFindings}
          title={status.enabled
            ? 'Scan existing findings and queue investigations'
            : 'Queue investigations from existing findings — they run once autonomous ops is enabled'}
        >
          <Icon name="search" /> {busy === 'scan' ? 'Scanning…' : 'Scan findings'}
        </button>
        <button
          className="btn danger"
          disabled={!status.active_agents || busy === 'killAll'}
          onClick={killAll}
          title={status.active_agents ? 'Kill all running agents' : 'No active agents to kill'}
        >
          <Icon name="x2" /> {busy === 'killAll' ? 'Killing…' : 'Kill all'}
        </button>
        <button className="btn ghost icon" title="Refresh" onClick={reload}><Icon name="refresh" /></button>
      </div>

      {error && <Banner tone="error" onClose={clearError}>{error}</Banner>}
      {notice && <Banner tone="ok" onClose={clearNotice}>{notice}</Banner>}

      {/* ---------- KPI strip ---------- */}
      <div className="kpi-strip" style={{ gridTemplateColumns: 'repeat(6, 1fr)' }}>
        {KPIS.map((k) => (
          <KpiCell
            key={k.key}
            label={k.label}
            value={k.value(status)}
            note={k.note}
            color={k.color}
            active={filterActive(k.statuses)}
            onClick={() => toggleFilter(k.statuses)}
          />
        ))}
        <KpiCell label="Total Cost" value={`$${cost.total_cost_usd?.toFixed(2) ?? '0.00'}`} note="cumulative spend" />
      </div>

      {/* ---------- hourly budget (only meaningful while enabled) ---------- */}
      {status.enabled && <BudgetBar used={cost.hourly_cost_usd} remaining={cost.hourly_budget_remaining} />}

      {/* ---------- investigation queue ---------- */}
      <div className="px-[22px] pt-5 pb-6">
        <div className="card card-sq">
          <div className="card-h">
            <h3 className="text-[14.5px]">Investigations</h3>
            {statusFilter && (
              <button className="chip sel" onClick={() => setStatusFilter(null)}>
                {statusFilter.join(', ').replace(/_/g, ' ')}
                <span className="dd clear"><Icon name="close" size={11} /></span>
              </button>
            )}
            <span className="grow" />
            <span className="text-xs text-tx-3">
              {filtered.length}{statusFilter ? ` of ${investigations.length}` : ''} total
            </span>
          </div>
          <div className="table-wrap list-scroll list-scroll-autoops">
            <table className="tbl">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Skill</th>
                  <th>Priority</th>
                  <th>Status</th>
                  <th>Activity</th>
                  <th>Iter</th>
                  <th>Cost</th>
                  <th>Created</th>
                  <th style={{ textAlign: 'right' }}>Actions</th>
                </tr>
              </thead>
              <tbody>
                {filtered.length === 0 ? (
                  <tr>
                    <td colSpan={9} className="muted" style={{ textAlign: 'center', padding: '40px 0' }}>
                      {statusFilter ? 'No investigations match this filter.' : 'No investigations yet.'}
                    </td>
                  </tr>
                ) : (
                  filtered.map((inv) => (
                    <InvestigationRow
                      key={inv.investigation_id}
                      inv={inv}
                      busy={busy}
                      onSelect={setSelectedId}
                      onWake={wake}
                      onKill={killInvestigation}
                    />
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </>
  )
}

function InvestigationRow({
  inv, busy, onSelect, onWake, onKill,
}: {
  inv: Investigation
  busy: string | null
  onSelect: (id: string) => void
  onWake: (id: string) => void
  onKill: (id: string) => void
}) {
  const canWake = ['sleeping', 'needs_rework', 'failed', 'completed'].includes(inv.status)
  const canKill = ['executing', 'assigned'].includes(inv.status)
  return (
    <tr className="clickable" onClick={() => onSelect(inv.investigation_id)}>
      <td><span className="id-cell">{inv.investigation_id}</span></td>
      <td><span className="tag">{inv.skill_id}</span></td>
      <td><span className={`prio ${inv.priority}`}>{inv.priority}</span></td>
      <td><StatusBadge status={inv.status} /></td>
      <td>
        <span
          className="mono muted"
          style={{ display: 'inline-block', maxWidth: 220, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', verticalAlign: 'middle' }}
        >
          {inv.current_activity || inv.status}
        </span>
      </td>
      <td>{inv.iteration_count}</td>
      <td className="muted">${inv.cost_usd?.toFixed(3)}</td>
      <td className="muted">{inv.created_at ? new Date(inv.created_at).toLocaleString() : '—'}</td>
      <td>
        <span className="row-act">
          <button title="View details" onClick={(e) => { e.stopPropagation(); onSelect(inv.investigation_id) }}>
            <Icon name="eye" />
          </button>
          {canWake && (
            <button title="Restart" disabled={busy === `wake:${inv.investigation_id}`} onClick={(e) => { e.stopPropagation(); onWake(inv.investigation_id) }}>
              <Icon name="play" />
            </button>
          )}
          {canKill && (
            <button title="Kill" disabled={busy === `kill:${inv.investigation_id}`} onClick={(e) => { e.stopPropagation(); onKill(inv.investigation_id) }}>
              <Icon name="trash" />
            </button>
          )}
        </span>
      </td>
    </tr>
  )
}

function KpiCell({
  label, value, note, color, active, onClick,
}: {
  label: string
  value: ReactNode
  note?: string
  color?: string
  active?: boolean
  onClick?: () => void
}) {
  const clickable = !!onClick
  return (
    <div
      className="kpi"
      role={clickable ? 'button' : undefined}
      tabIndex={clickable ? 0 : undefined}
      onClick={onClick}
      onKeyDown={clickable ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick!() } } : undefined}
      style={{ cursor: clickable ? 'pointer' : 'default', boxShadow: active ? 'inset 0 0 0 1.5px var(--accent)' : undefined }}
    >
      <span className="k-label">{label}</span>
      <div className="k-row"><span className="k-val" style={color ? { color } : undefined}>{value}</span></div>
      {note && <span className="k-note">{note}</span>}
    </div>
  )
}

function BudgetBar({ used, remaining }: { used: number; remaining: number }) {
  const total = used + remaining
  const pct = total > 0 ? Math.min(100, (used / total) * 100) : 100
  const low = remaining < 5
  return (
    <div className="px-[22px] pt-4">
      <div className="flex items-center justify-between mb-1.5 text-xs text-tx-3">
        <span>Hourly budget · ${used?.toFixed(2)} of ${total?.toFixed(2)}</span>
        <span style={low ? { color: 'var(--crit)', fontWeight: 600 } : undefined}>${remaining?.toFixed(2)} remaining</span>
      </div>
      <div style={{ height: 8, borderRadius: 6, background: 'var(--bg-2)', overflow: 'hidden' }}>
        <div style={{ width: `${pct}%`, height: '100%', background: low ? 'var(--crit)' : 'var(--accent)', borderRadius: 6, transition: 'width .3s' }} />
      </div>
    </div>
  )
}

function Banner({ tone, onClose, children }: { tone: 'error' | 'ok'; onClose: () => void; children: ReactNode }) {
  const c = tone === 'error' ? { bg: 'var(--crit-dim)', fg: 'var(--crit)' } : { bg: 'var(--ok-dim)', fg: 'var(--ok)' }
  return (
    <div
      className="flex items-center gap-2.5 mx-[22px] mt-3 px-3.5 py-2.5 text-[13px]"
      style={{ background: c.bg, color: c.fg, border: `1px solid ${c.fg}33`, borderRadius: 'var(--r)' }}
    >
      <Icon name={tone === 'error' ? 'alert' : 'check2'} size={15} />
      <span className="flex-1 text-tx-2">{children}</span>
      <button className="btn ghost icon" title="Dismiss" onClick={onClose} style={{ color: 'var(--tx-3)' }}>
        <Icon name="close" size={14} />
      </button>
    </div>
  )
}
