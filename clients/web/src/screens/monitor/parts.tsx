import { useEffect, useRef, useState } from 'react'
import { InfoTip } from '../../shared/InfoTip'
import { LevelBadge } from '../../shared/LevelBadge'
import { NotMeasured } from '../../shared/NotMeasured'
import type { MonitorMetric, PulseDay, WallBand } from '../../services/api'
import { count, hhmm, money, pct } from './format'

/** The ⓘ for a server-read number: its info line, and its limit when it has one. */
export function MetricTip({ label, metric, limit }: { label: string; metric: Pick<MonitorMetric, 'info' | 'unmeasured_reason'>; limit?: string }) {
  return <InfoTip label={`How ${label.toLowerCase()} is calculated`} source={metric.unmeasured_reason ?? undefined} calculation={metric.info} limit={limit} />
}

/** A value, or "Not measured yet" with why, never a guessed zero. */
export function Measured({ metric, render }: { metric: MonitorMetric; render: (value: number) => string }) {
  if (metric.unmeasured_text || metric.value == null) {
    return metric.unmeasured_reason ? <NotMeasured tip={metric.unmeasured_reason} /> : <span className="mn-dash">—</span>
  }
  return <>{render(metric.value)}</>
}

const PAD = { l: 44, r: 14, t: 12, b: 8 }
const AXIS_H = 22

/** Draw at the rendered width so text and marks keep their pixel size instead of stretching with the card. */
function useWidth(fallback = 1000) {
  const ref = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(fallback)
  useEffect(() => {
    const el = ref.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(([entry]) => {
      if (entry && entry.contentRect.width > 0) setWidth(Math.round(entry.contentRect.width))
    })
    observer.observe(el)
    return () => observer.disconnect()
  }, [])
  return { ref, width }
}

/** A round top for an axis: 1, 2 or 5 times a power of ten, at or above the value. */
function niceTop(value: number): number {
  if (value <= 0) return 1
  const step = 10 ** Math.floor(Math.log10(value))
  return ([1, 2, 5, 10].map((m) => m * step).find((m) => m >= value) ?? 10 * step)
}

/**
 * The day's spine as two panels on one hour axis, each on its own scale: cases running
 * against the cap (a line), then alerts in beside cases closed (bars). One crosshair and
 * tooltip spans both, so every value at an hour is read in one place.
 */
export function DayChart({ day, tall = false }: { day: PulseDay; tall?: boolean }) {
  const { ref, width } = useWidth()
  const [hover, setHover] = useState<number | null>(null)
  const n = day.hours.length
  const closed = day.closed_by_agents.map((a, i) => a + (day.closed_by_person[i] ?? 0))
  const panelH = tall ? 120 : 92
  const gap = 34
  const runTop = niceTop(Math.max(1, day.cap ?? 0, ...day.running) * 1.12)
  const flowTop = niceTop(Math.max(1, ...day.alerts_in, ...closed))
  const runY0 = PAD.t
  const flowY0 = PAD.t + panelH + gap
  const height = flowY0 + panelH + AXIS_H
  const plotW = Math.max(1, width - PAD.l - PAD.r)
  const slot = plotW / Math.max(1, n)
  const bar = Math.max(2, Math.min(14, slot * 0.32))
  const x = (i: number) => PAD.l + slot * i + slot / 2
  const yRun = (v: number) => runY0 + panelH - (v / runTop) * panelH
  const yFlow = (v: number) => flowY0 + panelH - (v / flowTop) * panelH
  const line = day.running.map((v, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${yRun(v).toFixed(1)}`).join(' ')
  const peakIndex = day.peak.at ? day.hours.indexOf(day.peak.at) : -1
  const ticks = [0, 6, 12, 18].map((h) => Math.round((h / 24) * n)).filter((i) => i < n)
  // The peak's label sits above its dot unless the cap line is there, then below.
  const peakY = peakIndex >= 0 ? yRun(day.peak.running) : 0
  const capY = day.cap != null ? yRun(day.cap) : null
  const peakLabelY = capY != null && Math.abs(peakY - 10 - capY) < 12 ? peakY + 18 : peakY - 10
  const pick = (clientX: number, rect: DOMRect) => {
    const i = Math.floor((clientX - rect.left - PAD.l) / slot)
    setHover(i >= 0 && i < n ? i : null)
  }
  const tipLeft = hover != null ? Math.min(Math.max(x(hover), 120), width - 120) : 0
  return (
    <div className="mn-day" ref={ref}>
      <div className="mn-legend-row" aria-hidden="true">
        <span><i className="key line" />Cases running</span>
        {day.cap != null && <span><i className="key cap" />Cap · {count(day.cap)}</span>}
        <span><i className="key in" />Alerts in</span>
        <span><i className="key closed" />Cases closed</span>
      </div>
      <div className="mn-day-plot">
        <svg
          className="mn-chart"
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
          role="img"
          aria-label="Alerts in, cases closed and cases running each hour, the last 24 hours"
          tabIndex={0}
          onPointerMove={(e) => pick(e.clientX, e.currentTarget.getBoundingClientRect())}
          onPointerLeave={() => setHover(null)}
          onFocus={() => setHover((h) => h ?? n - 1)}
          onBlur={() => setHover(null)}
          onKeyDown={(e) => {
            if (e.key === 'ArrowLeft') setHover((h) => Math.max(0, (h ?? n) - 1))
            if (e.key === 'ArrowRight') setHover((h) => Math.min(n - 1, (h ?? -1) + 1))
          }}
        >
          {/* Panel 1: cases running against the cap */}
          <text x={PAD.l - 8} y={runY0 + 4} className="mn-axis" textAnchor="end">{count(runTop)}</text>
          <text x={PAD.l - 8} y={runY0 + panelH} className="mn-axis" textAnchor="end">0</text>
          <line x1={PAD.l} x2={width - PAD.r} y1={runY0 + panelH} y2={runY0 + panelH} className="mn-base" />
          {capY != null && <line x1={PAD.l} x2={width - PAD.r} y1={capY} y2={capY} className="mn-cap" />}
          <path d={line} className="mn-line" />
          {peakIndex >= 0 && day.peak.running > 0 && (
            <g>
              <circle cx={x(peakIndex)} cy={peakY} r="4" className="mn-peak" />
              <text x={peakIndex > n * 0.6 ? x(peakIndex) - 8 : x(peakIndex) + 8} y={peakLabelY} className="mn-peak-label" textAnchor={peakIndex > n * 0.6 ? 'end' : 'start'}>
                {`Peak ${day.peak.running} at ${hhmm(day.peak.at)}`}
              </text>
            </g>
          )}

          {/* Panel 2: alerts in beside cases closed */}
          <text x={PAD.l - 8} y={flowY0 + 4} className="mn-axis" textAnchor="end">{count(flowTop)}</text>
          <text x={PAD.l - 8} y={flowY0 + panelH} className="mn-axis" textAnchor="end">0</text>
          <line x1={PAD.l} x2={width - PAD.r} y1={flowY0 + panelH} y2={flowY0 + panelH} className="mn-base" />
          {day.hours.map((hour, i) => (
            <g key={hour} className={hover === i ? 'on' : undefined}>
              <rect x={x(i) - bar - 1} y={yFlow(day.alerts_in[i] ?? 0)} width={bar} height={Math.max(0, flowY0 + panelH - yFlow(day.alerts_in[i] ?? 0))} className="mn-bar-in" rx="2" />
              <rect x={x(i) + 1} y={yFlow(closed[i] ?? 0)} width={bar} height={Math.max(0, flowY0 + panelH - yFlow(closed[i] ?? 0))} className="mn-bar-closed" rx="2" />
            </g>
          ))}

          {hover != null && (
            <g pointerEvents="none">
              <line x1={x(hover)} x2={x(hover)} y1={runY0} y2={flowY0 + panelH} className="mn-crosshair" />
              <circle cx={x(hover)} cy={yRun(day.running[hover] ?? 0)} r="4" className="mn-dot" />
            </g>
          )}

          {ticks.map((i) => (
            <text key={i} x={x(i)} y={height - 6} className="mn-axis" textAnchor="middle">{hhmm(day.hours[i])}</text>
          ))}
          <text x={x(n - 1)} y={height - 6} className="mn-axis" textAnchor="middle">now</text>
        </svg>
        {hover != null && (
          <div className="mn-tip" style={{ left: tipLeft, top: 0 }} role="status">
            <div className="mn-tip-head">{hover === n - 1 ? 'This hour' : `${hhmm(day.hours[hover])}–${hhmm(day.hours[hover + 1])}`}</div>
            <div><i className="key line" /><b>{count(day.running[hover])}</b> running{day.cap != null ? ` of ${count(day.cap)}` : ''}</div>
            <div><i className="key in" /><b>{count(day.alerts_in[hover])}</b> alerts in</div>
            <div><i className="key closed" /><b>{count(closed[hover])}</b> closed · {count(day.closed_by_person[hover])} by a person</div>
          </div>
        )}
      </div>
    </div>
  )
}

/** Alerts waiting at intake at the end of each hour, against the high-water mark. */
export function QueueChart({ hours, queued, highWater }: { hours: string[]; queued: number[]; highWater: number | null }) {
  const { ref, width } = useWidth()
  const height = 150
  const top = niceTop(Math.max(1, highWater ?? 0, ...queued) * 1.1)
  const plotW = Math.max(1, width - PAD.l - PAD.r)
  const plotH = height - PAD.t - AXIS_H
  const x = (i: number) => PAD.l + (plotW / Math.max(1, hours.length - 1)) * i
  const y = (v: number) => PAD.t + plotH - (v / top) * plotH
  const line = queued.map((v, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ')
  return (
    <div className="mn-day" ref={ref}>
      <svg className="mn-chart" width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Alerts waiting to be picked up, each hour, the last 24 hours">
        <text x={PAD.l - 8} y={PAD.t + 4} className="mn-axis" textAnchor="end">{count(top)}</text>
        <text x={PAD.l - 8} y={PAD.t + plotH} className="mn-axis" textAnchor="end">0</text>
        <line x1={PAD.l} x2={width - PAD.r} y1={PAD.t + plotH} y2={PAD.t + plotH} className="mn-base" />
        {highWater != null && (
          <g>
            <line x1={PAD.l} x2={width - PAD.r} y1={y(highWater)} y2={y(highWater)} className="mn-cap fair" />
            <text x={width - PAD.r} y={y(highWater) - 6} className="mn-axis fair" textAnchor="end">{`high-water mark · ${count(highWater)}`}</text>
          </g>
        )}
        <path d={line} className="mn-line fair" />
        {queued.length > 0 && <circle cx={x(queued.length - 1)} cy={y(queued[queued.length - 1] ?? 0)} r="4" className="mn-now" />}
        {[0, 6, 12, 18].map((h) => Math.round((h / 24) * hours.length)).filter((i) => i < hours.length).map((i) => (
          <text key={i} x={x(i)} y={height - 6} className="mn-axis" textAnchor="middle">{hhmm(hours[i])}</text>
        ))}
      </svg>
    </div>
  )
}

/** One row per workflow: running, today, cost per case, steps, waiting, and the share closed without a person. */
export function KindsTable({ kinds, info }: { kinds: WallBand[]; info: string }) {
  return (
    <section className="mn-card">
      <header className="mn-card-head">
        <h2>By kind of work</h2>
        <InfoTip label="How by kind of work is calculated" calculation={info} limit="No per-workflow concurrency cap is recorded yet." />
      </header>
      {kinds.length === 0 ? (
        <p className="mn-empty">No open cases yet.</p>
      ) : (
        <table className="mn-table">
          <thead>
            <tr>
              <th>Kind</th>
              <th className="num">Running</th>
              <th className="num">Open</th>
              <th className="num">Runs today</th>
              <th className="num">Cost per case</th>
              <th className="num">Avg steps</th>
              <th className="num">Waiting</th>
              <th>Handled without a person</th>
            </tr>
          </thead>
          <tbody>
            {kinds.map((kind) => (
              <tr key={kind.workflow_id}>
                <td className="mn-kind">{kind.name}</td>
                <td className="num" title={String(kind.running.cap_text ?? '')}>{count(kind.running.value)}</td>
                <td className="num">{count(kind.open)}</td>
                <td className="num">{count(kind.today)}</td>
                <td className="num">{money(kind.cost_per_case)}</td>
                <td className="num">{kind.mean_steps ?? '—'}</td>
                <td className="num">{count(kind.waiting)}</td>
                <td>
                  <span className="mn-rate">
                    <span className="num">{pct(kind.without_person.value)}</span>
                    <LevelBadge level={kind.without_person.level} variant="pill" />
                    {kind.without_person.value == null && <span className="mn-muted">no closes today</span>}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}
