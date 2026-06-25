/* ============================================================
   Case Metrics — analytics console. Ported to mirror the legacy
   CaseMetrics page (Cases by priority · Status distribution ·
   MTTD vs MTTR by priority · Analyst performance), wired to the
   real caseMetricsApi via useCaseMetrics.
   ============================================================ */
import { useState } from 'react'
import { Icon } from '../../shared/icons'
import { Pie, GroupedBars, Trend } from '../../shared/charts'
import { useCaseMetrics, type CaseMetricsData } from './useCaseMetrics'

const RANGES: [string, number][] = [
  ['7d', 7],
  ['30d', 30],
  ['90d', 90],
]

const PRIORITY_LABELS: [string, string][] = [
  ['critical', 'Critical'],
  ['high', 'High'],
  ['medium', 'Medium'],
  ['low', 'Low'],
]

const STATUS_COLOR: Record<string, string> = {
  open: 'var(--med)',
  new: 'var(--accent)',
  investigating: 'var(--high)',
  resolved: 'var(--ok)',
  closed: 'var(--tx-faint)',
  unknown: 'var(--tx-faint)',
}

/** legacy formatDuration: <1h → m, <24h → h, else d */
function formatDuration(hours: number): string {
  if (!hours || hours <= 0) return '0m'
  if (hours < 1) return `${Math.round(hours * 60)}m`
  if (hours < 24) return `${hours.toFixed(1)}h`
  return `${(hours / 24).toFixed(1)}d`
}

function successRate(resolved: number, assigned: number): number {
  if (!assigned) return 0
  return Math.round((resolved / assigned) * 100)
}

export default function MetricsScreen() {
  const [days, setDays] = useState(30)
  const { data, phase, error, reload } = useCaseMetrics(days)

  return (
    <>
      <div className="flex items-center gap-3 flex-wrap px-[22px] py-[13px] border-b border-line">
        <span className="text-[11px] font-semibold tracking-[0.06em] uppercase text-tx-3">Time range</span>
        <div className="range-tabs">
          {RANGES.map(([label, val]) => (
            <button key={val} className={val === days ? 'active' : ''} onClick={() => setDays(val)}>
              {label}
            </button>
          ))}
        </div>
        <div className="flex-1" />
        <button className="btn ghost icon" title="Refresh" onClick={reload}><Icon name="refresh" /></button>
        <button className="btn primary"><Icon name="download" /> Export report</button>
      </div>

      {phase === 'loading' && <div className="text-sm text-tx-3 py-20 text-center">Loading case metrics…</div>}
      {phase === 'error' && (
        <div className="py-20 text-center">
          <div className="flex flex-col items-center gap-2.5">
            <span className="text-sm text-tx-3">Couldn’t load case metrics: {error}</span>
            <button className="btn ghost" onClick={reload}>Retry</button>
          </div>
        </div>
      )}
      {phase === 'ready' && data && <MetricsBody data={data} />}
    </>
  )
}

function MetricsBody({ data }: { data: CaseMetricsData }) {
  const { totalCases, openCases, criticalCases, mttdHours, mttrHours, mttdByPriority, mttrByPriority, byPriority, byStatus, analysts } = data

  const priorityRows = byPriority.map((p) => ({
    label: (PRIORITY_LABELS.find(([k]) => k === p.priority)?.[1]) || p.priority,
    a: p.count,
    b: p.closed_count,
  }))

  const statusTotal = byStatus.reduce((acc, s) => acc + s.count, 0)
  const statusSegs = byStatus.map((s) => ({
    v: statusTotal ? s.count / statusTotal : 0,
    color: STATUS_COLOR[s.status] || 'var(--tx-faint)',
    label: s.status,
  }))

  // MTTD vs MTTR across priority levels (both in hours)
  const rtLabels = PRIORITY_LABELS.map(([, label]) => label)
  const mttdSeries = PRIORITY_LABELS.map(([k]) => Number((mttdByPriority[k] ?? 0).toFixed(2)))
  const mttrSeries = PRIORITY_LABELS.map(([k]) => Number((mttrByPriority[k] ?? 0).toFixed(2)))

  return (
    <>
      <div className="grid grid-cols-4 border-b border-line">
        <Kpi label="Total Cases" note="across all sources">
          <span className="text-[30px] font-semibold tracking-[-0.02em] leading-[1.1]">{totalCases}</span>
        </Kpi>
        <Kpi label="Open Cases" note={`${criticalCases} critical`}>
          <span className="text-[30px] font-semibold tracking-[-0.02em] leading-[1.1] text-high">{openCases}</span>
        </Kpi>
        <Kpi label="MTTD" note="mean time to detect">
          <span className="text-[30px] font-semibold tracking-[-0.02em] leading-[1.1]">{formatDuration(mttdHours)}</span>
        </Kpi>
        <Kpi label="MTTR" note="mean time to respond">
          <span className="text-[30px] font-semibold tracking-[-0.02em] leading-[1.1]">{formatDuration(mttrHours)}</span>
        </Kpi>
      </div>

      <div className="px-[22px] pt-5 pb-3">
        <div className="grid gap-3 grid-cols-[1.6fr_1fr]">
          <div className="card card-sq">
            <div className="card-h">
              <h3 className="text-[14.5px]">Cases by priority</h3>
              <span className="flex-1" />
              <div className="trend-legend">
                <span className="li"><span className="ln" style={{ background: 'var(--accent)' }} />Total</span>
                <span className="li"><span className="ln" style={{ background: 'var(--ok)' }} />Closed</span>
              </div>
            </div>
            <div className="card-b">
              {priorityRows.length ? (
                <GroupedBars rows={priorityRows} />
              ) : (
                <div className="text-sm text-tx-3 py-10 text-center">No cases in range.</div>
              )}
            </div>
          </div>
          <div className="card card-sq">
            <div className="card-h"><h3 className="text-[14.5px]">Status distribution</h3></div>
            <div className="card-b">
              {statusTotal ? (
                <div className="donut-wrap">
                  <Pie segs={statusSegs} size={180} />
                  <div className="legend">
                    {byStatus.map((s) => (
                      <div className="li" key={s.status}>
                        <span className="sw" style={{ background: STATUS_COLOR[s.status] || 'var(--tx-faint)' }} />
                        <span className="capitalize">{s.status}</span>
                        <span className="v">{s.count}</span>
                      </div>
                    ))}
                  </div>
                </div>
              ) : (
                <div className="text-sm text-tx-3 py-10 text-center">No cases in range.</div>
              )}
            </div>
          </div>
        </div>
      </div>

      <div className="px-[22px] pb-3">
        <div className="card card-sq">
          <div className="card-h">
            <h3 className="text-[14.5px]">Response time trends (MTTD vs MTTR)</h3>
            <span className="flex-1" />
            <div className="trend-legend">
              <span className="li"><span className="ln" style={{ background: 'var(--accent)' }} />MTTD (h)</span>
              <span className="li"><span className="ln" style={{ background: 'var(--ok)' }} />MTTR (h)</span>
            </div>
          </div>
          <div className="card-b">
            <Trend
              seriesA={mttdSeries}
              seriesB={mttrSeries}
              labels={rtLabels}
              pointLabels={rtLabels}
              names={['MTTD (h)', 'MTTR (h)']}
            />
          </div>
        </div>
      </div>

      <div className="px-[22px] pb-6">
        <div className="card card-sq">
          <div className="card-h">
            <h3 className="text-[14.5px]">Analyst performance</h3>
            <span className="flex-1" />
            <span className="text-xs text-tx-3">{analysts.length} analysts</span>
          </div>
          <div className="table-wrap">
            <table className="tbl">
              <thead>
                <tr>
                  <th>Analyst</th>
                  <th>Assigned</th>
                  <th>Closed</th>
                  <th>Avg Resolution</th>
                  <th>Success Rate</th>
                </tr>
              </thead>
              <tbody>
                {analysts.length === 0 && (
                  <tr><td colSpan={5} className="muted" style={{ textAlign: 'center', padding: '32px 0' }}>No analyst activity in range.</td></tr>
                )}
                {analysts.map((a) => {
                  const rate = successRate(a.cases_resolved, a.cases_assigned)
                  const cls = rate > 80 ? 'hot' : ''
                  return (
                    <tr key={a.analyst_id}>
                      <td>{a.analyst_name || 'Unassigned'}</td>
                      <td>{a.cases_assigned}</td>
                      <td>{a.cases_resolved}</td>
                      <td className="muted">{formatDuration(a.avg_resolution_time)}</td>
                      <td>
                        <span className="scorebar">
                          <span className="track"><i className={cls} style={{ width: `${rate}%` }} /></span>
                          <span className="num">{rate}%</span>
                        </span>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </>
  )
}

function Kpi({ label, note, children }: { label: string; note: string; children: React.ReactNode }) {
  return (
    <div className="relative flex flex-col gap-[3px] px-[22px] py-4 border-r border-line-soft last:border-r-0">
      <span className="text-[11px] font-semibold tracking-[0.07em] uppercase text-tx-3 truncate">{label}</span>
      <div className="flex items-baseline gap-2.5">{children}</div>
      <span className="text-xs text-tx-faint">{note}</span>
    </div>
  )
}
