/* ============================================================
   Analytics Dashboard — report + AI insights rail (Direction B)
   Ported from app/analytics.js. Wired to /analytics and
   /analytics/insights via useAnalytics / useAnalyticsInsights.
   ============================================================ */
import { useMemo, useRef, useState } from 'react'
import { format } from 'date-fns'
import { Icon } from '../../shared/icons'
import { Pie, Hbars, Trend } from '../../shared/charts'
import {
  useAnalytics,
  useAnalyticsInsights,
  insightLevel,
  type AnalyticsData,
  type HeatmapCell,
  type TimeRange,
} from './useAnalytics'

const TH_DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
const TH_DAY_FULL = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

interface HeatHover {
  x: number
  y: number
  day: string
  hour: number
  count: number
  critical: number
  high: number
}

/* attack-time heatmap: 7 days x 24 hours, driven by real findings. The
   backend returns one cell per (dayNum, hour) with an intensity count;
   we bucket each cell to a 0–4 level relative to the busiest cell. */
function TimeHeatmap({ cells }: { cells: HeatmapCell[] }) {
  const wrapRef = useRef<HTMLDivElement>(null)
  const [hover, setHover] = useState<HeatHover | null>(null)

  const { grid, max } = useMemo(() => {
    const g: HeatmapCell[][] = Array.from({ length: 7 }, () => [])
    let m = 0
    for (const c of cells) {
      if (c.dayNum >= 0 && c.dayNum < 7 && c.hour >= 0 && c.hour < 24) {
        g[c.dayNum][c.hour] = c
        if (c.intensity > m) m = c.intensity
      }
    }
    return { grid: g, max: m }
  }, [cells])

  const level = (intensity: number) => {
    if (!intensity || max <= 0) return 0
    return Math.min(4, Math.max(1, Math.ceil((intensity / max) * 4)))
  }

  const showTip = (e: React.MouseEvent, di: number, h: number, cell?: HeatmapCell) => {
    const rect = wrapRef.current?.getBoundingClientRect()
    if (!rect) return
    setHover({
      x: e.clientX - rect.left,
      y: e.clientY - rect.top,
      day: TH_DAY_FULL[di],
      hour: h,
      count: cell?.count ?? 0,
      critical: cell?.critical ?? 0,
      high: cell?.high ?? 0,
    })
  }

  return (
    <div className="th-wrap" ref={wrapRef} onMouseLeave={() => setHover(null)}>
      <div className="timeheat">
        <div className="th-hours">
          <span />
          {Array.from({ length: 24 }, (_, h) => (
            <span key={h}>{h % 3 === 0 ? h : ''}</span>
          ))}
        </div>
        {TH_DAYS.map((d, di) => (
          <div className="th-row" key={d}>
            <span className="th-day">{d}</span>
            {Array.from({ length: 24 }, (_, h) => {
              const cell = grid[di]?.[h]
              const lv = level(cell?.intensity ?? 0)
              const color =
                lv === 0
                  ? undefined
                  : (cell?.critical ?? 0) > 0 || lv >= 4
                    ? 'var(--crit)'
                    : (cell?.high ?? 0) > 0 || lv >= 3
                      ? 'var(--high)'
                      : 'var(--accent)'
              return (
                <div
                  className="th-cell"
                  key={h}
                  style={color ? { background: color, opacity: Number((0.3 + lv * 0.17).toFixed(2)) } : undefined}
                  onMouseEnter={(e) => showTip(e, di, h, cell)}
                  onMouseMove={(e) => showTip(e, di, h, cell)}
                />
              )
            })}
          </div>
        ))}
      </div>

      {/* color key: opacity ramp = volume, hue = worst severity in the cell */}
      <div className="th-legend">
        <span className="th-leg-cap">Fewer</span>
        <span className="th-leg-scale">
          {[1, 2, 3, 4].map((lv) => (
            <i key={lv} style={{ background: 'var(--accent)', opacity: Number((0.3 + lv * 0.17).toFixed(2)) }} />
          ))}
        </span>
        <span className="th-leg-cap">More findings</span>
        <span className="th-leg-sep" />
        <span className="th-leg-key"><i style={{ background: 'var(--high)' }} />High present</span>
        <span className="th-leg-key"><i style={{ background: 'var(--crit)' }} />Critical present</span>
      </div>

      {hover && (
        <div
          className="th-tip"
          style={{ left: hover.x, top: hover.y }}
          // keep the tip from intercepting the next cell's mouse events
        >
          <div className="th-tip-h">{hover.day} · {String(hover.hour).padStart(2, '0')}:00</div>
          <div className="th-tip-row"><span>Findings</span><b>{hover.count}</b></div>
          {hover.critical > 0 && <div className="th-tip-row crit"><span>Critical</span><b>{hover.critical}</b></div>}
          {hover.high > 0 && <div className="th-tip-row high"><span>High</span><b>{hover.high}</b></div>}
        </div>
      )}
    </div>
  )
}

/* delta chip — direction arrow + colour. All four KPIs are "lower is
   better" from a SOC-health view, so a negative change reads green. */
function Delta({ change }: { change: number }) {
  if (!change) return <span className="text-xs text-tx-faint">no change</span>
  const good = change < 0
  return (
    <span className={`text-xs font-semibold inline-flex items-center gap-[3px] ${good ? 'text-ok' : 'text-crit'}`}>
      <Icon name={change < 0 ? 'arrowDn' : 'arrowUp'} size={12} />
      {Math.abs(change)}%
    </span>
  )
}

const SEV_COLOR: Record<string, string> = {
  Critical: 'var(--crit)',
  High: 'var(--high)',
  Medium: '#c7a14a',
  Low: 'var(--ok)',
  Informational: 'var(--tx-faint)',
}

/* short, deterministic x-axis labels from ISO bucket timestamps */
function trendLabels(timestamps: string[]): string[] {
  const n = timestamps.length
  if (n === 0) return []
  // show ~7 evenly spaced labels so the axis stays readable at any bucket count
  const step = Math.max(1, Math.round(n / 7))
  return timestamps.map((ts, i) => {
    if (i % step !== 0 && i !== n - 1) return ''
    const d = new Date(ts)
    return Number.isNaN(d.getTime()) ? '' : format(d, 'MMM d')
  })
}

function AnalyticsBody({ data }: { data: AnalyticsData }) {
  const { metrics, timeSeriesData, severityDistribution, topSources, affectedEntities, attackHeatmap, mitreTechniques } = data

  const findingsSeries = timeSeriesData.map((p) => p.findings)
  const casesSeries = timeSeriesData.map((p) => p.cases)

  const maxSrc = Math.max(1, ...topSources.map((s) => s.count))
  const sources = topSources
    .slice(0, 6)
    .map((s) => ({ label: s.name, val: s.count, pct: Math.round((s.count / maxSrc) * 100) }))

  const maxMitre = Math.max(1, ...mitreTechniques.map((m) => m.count))
  const mitre = mitreTechniques
    .slice(0, 6)
    .map((m) => ({ label: m.techniqueId, val: m.count, pct: Math.round((m.count / maxMitre) * 100) }))

  const sevTotal = severityDistribution.reduce((a, s) => a + s.value, 0)
  const sevSegs = severityDistribution.map((s) => ({
    v: sevTotal ? s.value / sevTotal : 0,
    color: SEV_COLOR[s.name] || s.color || 'var(--accent)',
    label: s.name,
  }))
  const labels = trendLabels(timeSeriesData.map((p) => p.timestamp))
  const tipLabels = timeSeriesData.map((p) => {
    const d = new Date(p.timestamp)
    return Number.isNaN(d.getTime()) ? '' : format(d, 'MMM d, HH:mm')
  })

  return (
    <div className="flex-1 min-w-0">
      <div className="grid grid-cols-4 border-b border-line">
        <div className="relative flex flex-col gap-[3px] px-[22px] py-4 border-r border-line-soft last:border-r-0">
          <span className="text-[11px] font-semibold tracking-[0.07em] uppercase text-tx-3 truncate">Total Findings</span>
          <div className="flex items-baseline gap-2.5">
            <span className="text-[30px] font-semibold tracking-[-0.02em] leading-[1.1]">{metrics.totalFindings}</span>
            <Delta change={metrics.findingsChange} />
          </div>
          <span className="text-xs text-tx-faint">vs last period</span>
        </div>
        <div className="relative flex flex-col gap-[3px] px-[22px] py-4 border-r border-line-soft last:border-r-0">
          <span className="text-[11px] font-semibold tracking-[0.07em] uppercase text-tx-3 truncate">Active Cases</span>
          <div className="flex items-baseline gap-2.5">
            <span className="text-[30px] font-semibold tracking-[-0.02em] leading-[1.1]">{metrics.totalCases}</span>
            <Delta change={metrics.casesChange} />
          </div>
          <span className="text-xs text-tx-faint">vs last period</span>
        </div>
        <div className="relative flex flex-col gap-[3px] px-[22px] py-4 border-r border-line-soft last:border-r-0">
          <span className="text-[11px] font-semibold tracking-[0.07em] uppercase text-tx-3 truncate">Avg Response</span>
          <div className="flex items-baseline gap-2.5">
            <span className="text-[30px] font-semibold tracking-[-0.02em] leading-[1.1]">
              {metrics.avgResponseTime}
              <span className="text-base text-tx-3 font-medium">m</span>
            </span>
            <Delta change={metrics.responseTimeChange} />
          </div>
          <span className="text-xs text-tx-faint">mean time to respond</span>
        </div>
        <div className="relative flex flex-col gap-[3px] px-[22px] py-4 border-r border-line-soft last:border-r-0">
          <span className="text-[11px] font-semibold tracking-[0.07em] uppercase text-tx-3 truncate">False Positive</span>
          <div className="flex items-baseline gap-2.5">
            <span className="text-[30px] font-semibold tracking-[-0.02em] leading-[1.1] text-ok">
              {metrics.falsePositiveRate}
              <span className="text-base text-tx-3 font-medium">%</span>
            </span>
            <Delta change={metrics.falsePositiveChange} />
          </div>
          <span className="text-xs text-tx-faint">verified</span>
        </div>
      </div>

      <div className="px-[22px] pt-5 pb-3">
        <div className="bg-panel border border-line rounded-sm shadow-panel overflow-hidden">
          <div className="flex items-center gap-2.5 px-[18px] py-[15px] border-b border-line-soft">
            <h3 className="text-[14.5px]">Findings &amp; cases over time</h3>
            <span className="flex-1" />
            <div className="trend-legend">
              <span className="li">
                <span className="ln" style={{ background: 'var(--accent)' }} />
                Findings
              </span>
              <span className="li">
                <span className="ln" style={{ background: 'var(--ok)' }} />
                Cases
              </span>
            </div>
          </div>
          <div className="p-[18px]">
            {timeSeriesData.length > 1 ? (
              <Trend seriesA={findingsSeries} seriesB={casesSeries} labels={labels} pointLabels={tipLabels} names={['Findings', 'Cases']} />
            ) : (
              <div className="text-sm text-tx-3 py-10 text-center">Not enough data for this period.</div>
            )}
          </div>
        </div>
      </div>

      <div className="px-[22px] pt-0 pb-3">
        <div className="grid gap-3 grid-cols-[1.3fr_1fr]">
          <div className="bg-panel border border-line rounded-sm shadow-panel overflow-hidden">
            <div className="flex items-center gap-2.5 px-[18px] py-[15px] border-b border-line-soft">
              <h3 className="text-[14.5px]">Top alert sources</h3>
            </div>
            <div className="p-[18px]">
              {sources.length ? <Hbars items={sources} /> : <div className="text-sm text-tx-3 py-6 text-center">No sources.</div>}
            </div>
          </div>
          <div className="bg-panel border border-line rounded-sm shadow-panel overflow-hidden">
            <div className="flex items-center gap-2.5 px-[18px] py-[15px] border-b border-line-soft">
              <h3 className="text-[14.5px]">Severity distribution</h3>
            </div>
            <div className="p-[18px]">
              <div className="donut-wrap">
                <Pie segs={sevSegs} size={180} />
                <div className="legend">
                  {severityDistribution.map((s) => (
                    <div className="li" key={s.name}>
                      <span className="sw" style={{ background: SEV_COLOR[s.name] || s.color }} />
                      {s.name}
                      <span className="v">{s.value}</span>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div className="px-[22px] pt-0 pb-3">
        <div className="grid gap-3 grid-cols-[1.3fr_1fr]">
          <div className="bg-panel border border-line rounded-sm shadow-panel overflow-hidden">
            <div className="flex items-center gap-2.5 px-[18px] py-[15px] border-b border-line-soft">
              <h3 className="text-[14.5px]">Top MITRE ATT&amp;CK techniques</h3>
            </div>
            <div className="p-[18px]">
              {mitre.length ? <Hbars items={mitre} /> : <div className="text-sm text-tx-3 py-6 text-center">No techniques mapped.</div>}
            </div>
          </div>
          <div className="bg-panel border border-line rounded-sm shadow-panel overflow-hidden">
            <div className="flex items-center gap-2.5 px-[18px] py-[15px] border-b border-line-soft">
              <h3 className="text-[14.5px]">Most affected entities</h3>
            </div>
            <div className="p-[18px]">
              {affectedEntities.length ? (
                <div className="mini-list">
                  {affectedEntities.slice(0, 6).map((e) => (
                    <div className="ml-row" key={e.entity}>
                      <span className="ml-name">{e.entity}</span>
                      <span className="ml-kind">risk {e.riskScore}</span>
                      <span className="ml-val">{e.count}</span>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="text-sm text-tx-3 py-6 text-center">No entity data.</div>
              )}
            </div>
          </div>
        </div>
      </div>

      <div className="px-[22px] pt-0 pb-5">
        <div className="bg-panel border border-line rounded-sm shadow-panel overflow-hidden">
          <div className="flex items-center gap-2.5 px-[18px] py-[15px] border-b border-line-soft">
            <h3 className="text-[14.5px]">Attack time heatmap</h3>
            <span className="flex-1" />
            <span className="text-xs text-tx-3">hour of day × day of week</span>
          </div>
          <div className="p-[18px]">
            <TimeHeatmap cells={attackHeatmap} />
          </div>
        </div>
      </div>
    </div>
  )
}

function InsightsRail({ timeRange, onClose }: { timeRange: TimeRange; onClose: () => void }) {
  const { insights, generatedAt, isStale, generating, phase } = useAnalyticsInsights(timeRange)
  return (
    <aside className="insights-rail">
      <div className="ir-head">
        <span className="ico">
          <Icon name="brain" />
        </span>
        <h3>AI-Powered Insights</h3>
        {isStale && generatedAt && <span className="chip warn">Stale</span>}
        {generating && <span className="chip">Refreshing…</span>}
        <span className="flex-1" />
        <button className="btn ghost icon" title="Hide insights" onClick={onClose}>
          <Icon name="close" size={15} />
        </button>
      </div>
      <div className="ir-body">
        {phase === 'loading' && <div className="text-sm text-tx-3">Loading insights…</div>}
        {phase === 'error' && <div className="text-sm text-tx-3">Couldn’t load insights.</div>}
        {phase === 'ready' && insights.length === 0 && (
          <div className="text-sm text-tx-3">
            {generating ? 'Generating insights — this can take up to a minute.' : 'No insights yet.'}
          </div>
        )}
        {insights.map((i) => {
          const when = (() => {
            const d = new Date(i.timestamp)
            return Number.isNaN(d.getTime()) ? '' : format(d, 'MMM d, yyyy · HH:mm')
          })()
          return (
            <div className={`insight ${insightLevel(i.type)}`} key={i.id}>
              <div className="it-h">
                <span className="it-dot" />
                <span className="it-title">{i.title}</span>
                <span className="conf">{Math.round(i.confidence * 100)}%</span>
              </div>
              <div className="it-body">{i.description}</div>
              {when && <div className="it-time">{when}</div>}
            </div>
          )
        })}
      </div>
    </aside>
  )
}

export default function AnalyticsScreen() {
  const [range, setRange] = useState<TimeRange>('7d')
  const ranges: [string, TimeRange][] = [
    ['24h', '24h'],
    ['7d', '7d'],
    ['30d', '30d'],
    ['All', 'all'],
  ]
  const [showInsights, setShowInsights] = useState(true)
  const { data, phase, error, reload } = useAnalytics(range)

  return (
    <div className="flex items-start min-h-full">
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-3 flex-wrap px-[22px] py-[13px] border-b border-line">
          <span className="text-[11px] font-semibold tracking-[0.06em] uppercase text-tx-3">
            Time range
          </span>
          <div className="range-tabs">
            {ranges.map(([label, val]) => (
              <button key={val} className={val === range ? 'active' : ''} onClick={() => setRange(val)}>
                {label}
              </button>
            ))}
          </div>
          <div className="flex-1" />
          {!showInsights && (
            <button className="btn ghost" title="Show AI insights" onClick={() => setShowInsights(true)}>
              <Icon name="brain" size={15} /> Insights
            </button>
          )}
          <button className="btn ghost icon" title="Refresh" onClick={reload}>
            <Icon name="refresh" />
          </button>
          <button className="btn primary">
            <Icon name="download" /> Export report
          </button>
        </div>

        {phase === 'loading' && (
          <div className="text-sm text-tx-3 py-20 text-center">Loading analytics…</div>
        )}
        {phase === 'error' && (
          <div className="py-20 text-center">
            <div className="flex flex-col items-center gap-2.5">
              <span className="text-sm text-tx-3">Couldn’t load analytics: {error}</span>
              <button className="btn ghost" onClick={reload}>Retry</button>
            </div>
          </div>
        )}
        {phase === 'ready' && data && <AnalyticsBody data={data} />}
      </div>

      {showInsights && <InsightsRail timeRange={range} onClose={() => setShowInsights(false)} />}
    </div>
  )
}
