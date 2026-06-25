/* ============================================================
   SVG chart builders — clean, calm, no library look.
   Ported from the design's charts.js to React components.
   Colors are passed through as `var(--*)` strings so charts
   recolor live when the accent tweak changes.
   ============================================================ */
import { useEffect, useRef, useState } from 'react'

export interface DonutSeg {
  v: number // fraction 0..1
  color: string
  /** optional key used to link hover state with a legend row */
  label?: string
}

/* clean donut — crisp segments, uniform gaps, soft track behind.
   Colours are set via `style` (not the `stroke` attribute) because CSS
   var() is only resolved in CSS property values, not presentation
   attributes — as an attribute the arcs render as `none` (invisible).

   Segments draw with rounded caps and grow in on mount (staggered
   stroke-dashoffset transition). When `active` matches a segment's
   `label` that arc lifts (scale + soft glow) and the rest dim slightly —
   the parent screen owns `active` so a calm legend can drive it too.
   Hover/animation are pure CSS so we keep the redesign library-free. */
export function Donut({
  segs,
  size = 200,
  stroke = 16,
  active = null,
  onActiveChange,
}: {
  segs: DonutSeg[]
  size?: number
  stroke?: number
  active?: string | null
  onActiveChange?: (label: string | null) => void
}) {
  const r = (size - stroke) / 2
  const cx = size / 2
  const c = 2 * Math.PI * r
  const gapPx = 4
  // drop empty segments so a 0-count severity doesn't leave a stray sliver
  const visible = segs.filter((s) => s.v > 0.0001)
  const multi = visible.length > 1
  const hoverable = !!onActiveChange

  // grow-in reveal: arcs start fully hidden (offset === circumference,
  // mirroring the shadcn approach where the off-gap is the full
  // circumference) then transition to their cumulative position once
  // mounted, staggered per segment.
  const [mounted, setMounted] = useState(false)
  useEffect(() => {
    const id = requestAnimationFrame(() => setMounted(true))
    return () => cancelAnimationFrame(id)
  }, [])

  let off = 0
  const arcs = visible.map((s, i) => {
    const segLen = s.v * c
    const draw = multi ? Math.max(segLen - gapPx, 1) : segLen
    const posOffset = -off
    const isActive = active != null && s.label != null && s.label === active
    const dimmed = active != null && !isActive
    const arc = (
      <circle
        key={s.label ?? i}
        cx={cx}
        cy={cx}
        r={r}
        fill="none"
        strokeWidth={stroke}
        strokeLinecap={multi ? 'round' : 'butt'}
        // off-gap is the full circumference so `offset = c` parks the arc
        // entirely off the path (hidden) until mount, then it slides to pos
        strokeDasharray={`${draw} ${c}`}
        strokeDashoffset={mounted ? posOffset : c}
        onMouseEnter={hoverable ? () => onActiveChange?.(s.label ?? null) : undefined}
        style={{
          stroke: s.color,
          cursor: hoverable ? 'pointer' : 'default',
          // scale/glow about the donut centre — fill-box keeps the origin
          // on the geometry rather than the (0,0) SVG corner
          transformBox: 'fill-box',
          transformOrigin: 'center',
          transform: isActive ? 'scale(1.04)' : 'scale(1)',
          filter: isActive ? `drop-shadow(0 0 5px ${s.color})` : 'none',
          opacity: dimmed ? 0.5 : 1,
          transition:
            `stroke-dashoffset .8s cubic-bezier(.4,0,.2,1) ${(i * 0.07).toFixed(2)}s,` +
            ' transform .2s ease-out, filter .2s ease-out, opacity .2s ease-out',
        }}
      />
    )
    off += segLen
    return arc
  })
  return (
    <svg
      width="100%"
      viewBox={`0 0 ${size} ${size}`}
      // `size` now only sets the viewBox coordinate space (geometry/stroke
      // math); the rendered donut fills its container width and stays a
      // perfect circle via aspect-ratio, so it grows to use the available
      // card space instead of sitting small. preflight is off in the
      // redesign, so pin display/aspect explicitly.
      style={{ display: 'block', width: '100%', height: 'auto', aspectRatio: '1 / 1' }}
      onMouseLeave={hoverable ? () => onActiveChange?.(null) : undefined}
    >
      {/* -90° rotation lives on the wrapper <g> (not the arcs) so per-arc
          CSS transforms stay free for the hover scale */}
      <g transform={`rotate(-90 ${cx} ${cx})`}>
        <circle cx={cx} cy={cx} r={r} fill="none" strokeWidth={stroke} style={{ stroke: 'var(--bg-3)' }} />
        {arcs}
      </g>
    </svg>
  )
}

/* solid pie — filled wedges (no center hole). Thin panel-colored strokes
   separate the slices, mirroring the donut's gap treatment. Colours are set
   via the `fill` style so CSS var() resolves (see Donut note above). */
export function Pie({ segs, size = 200 }: { segs: DonutSeg[]; size?: number }) {
  const r = size / 2
  const visible = segs.filter((s) => s.v > 0.0001)
  // preflight is off; pin size/display so the pie stays a fixed circle
  const svgStyle = { display: 'block', width: size, height: size, flex: '0 0 auto' as const }
  // a single full slice can't be expressed as an arc path (start === end);
  // render it as a plain filled circle instead
  if (visible.length === 1) {
    return (
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} style={svgStyle}>
        <circle cx={r} cy={r} r={r} style={{ fill: visible[0].color }} />
      </svg>
    )
  }
  let a0 = -Math.PI / 2
  const arcs = visible.map((s, i) => {
    const a1 = a0 + s.v * 2 * Math.PI
    const x0 = r + r * Math.cos(a0)
    const y0 = r + r * Math.sin(a0)
    const x1 = r + r * Math.cos(a1)
    const y1 = r + r * Math.sin(a1)
    const large = s.v > 0.5 ? 1 : 0
    const d = `M${r},${r} L${x0.toFixed(2)},${y0.toFixed(2)} A${r},${r} 0 ${large} 1 ${x1.toFixed(2)},${y1.toFixed(2)} Z`
    a0 = a1
    return <path key={i} d={d} style={{ fill: s.color }} stroke="var(--panel)" strokeWidth={2} strokeLinejoin="round" />
  })
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} style={svgStyle}>
      {arcs}
    </svg>
  )
}

/* sparkline path + soft area */
export function Spark({ pts, color, w = 78, h = 30 }: { pts: number[]; color: string; w?: number; h?: number }) {
  const max = Math.max(...pts)
  const min = Math.min(...pts)
  const span = max - min || 1
  const xy = pts.map((p, i) => [(i / (pts.length - 1)) * (w - 2) + 1, h - 2 - ((p - min) / span) * (h - 6)])
  const d = xy.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ')
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none">
      <path d={`${d} L${w - 1},${h} L1,${h} Z`} fill={color} opacity={0.12} />
      <path d={d} fill="none" stroke={color} strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

/* dual area trend — clean, borderless: gradient-filled areas, soft strokes,
   faint dashed gridlines for reference, no axis box / markers. */
export function Trend({
  seriesA,
  seriesB,
  labels,
  pointLabels,
  names = ['Series A', 'Series B'],
  w = 760,
  h = 220,
}: {
  seriesA: number[]
  seriesB: number[]
  labels: string[]
  /** full per-bucket labels for the hover tooltip (defaults to `labels`) */
  pointLabels?: string[]
  /** series names shown in the hover tooltip */
  names?: [string, string]
  w?: number
  h?: number
}) {
  const svgRef = useRef<SVGSVGElement>(null)
  const [hover, setHover] = useState<number | null>(null)
  const n = seriesA.length
  const padB = 24
  const padL = 28 // gutter so the y-axis labels sit beside, not under, the area
  const padR = 6
  const top = 12
  const all = [...seriesA, ...seriesB]
  const max = Math.ceil(Math.max(...all) / 10) * 10 || 10
  const plotH = h - padB - top
  const plotW = w - padL - padR
  const X = (i: number) => padL + (i / Math.max(seriesA.length - 1, 1)) * plotW
  const Y = (v: number) => top + plotH - (v / max) * plotH

  // smooth the line with monotone cubic Hermite interpolation
  // (Fritsch–Carlson). A plain Catmull-Rom fit overshoots flat→climb runs
  // — e.g. 0,0 → 5 used to dip below 0 before rising — so we clamp the
  // tangents to keep each segment monotonic (no over/undershoot).
  const linePath = (arr: number[]) => {
    const n = arr.length
    if (n === 0) return ''
    const xs = arr.map((_, i) => X(i))
    const ys = arr.map((v) => Y(v))
    if (n < 3) return xs.map((x, i) => `${i ? 'L' : 'M'}${x.toFixed(1)},${ys[i].toFixed(1)}`).join(' ')

    const dx: number[] = []
    const slope: number[] = []
    for (let i = 0; i < n - 1; i++) {
      dx[i] = xs[i + 1] - xs[i]
      slope[i] = (ys[i + 1] - ys[i]) / dx[i]
    }
    // tangents: average of adjacent secants, flattened at local extrema
    const m: number[] = new Array(n)
    m[0] = slope[0]
    m[n - 1] = slope[n - 2]
    for (let i = 1; i < n - 1; i++) {
      m[i] = slope[i - 1] * slope[i] <= 0 ? 0 : (slope[i - 1] + slope[i]) / 2
    }
    // Fritsch–Carlson clamp so the cubic can't overshoot a segment
    for (let i = 0; i < n - 1; i++) {
      if (slope[i] === 0) {
        m[i] = 0
        m[i + 1] = 0
        continue
      }
      const a = m[i] / slope[i]
      const b = m[i + 1] / slope[i]
      const s = a * a + b * b
      if (s > 9) {
        const t = 3 / Math.sqrt(s)
        m[i] = t * a * slope[i]
        m[i + 1] = t * b * slope[i]
      }
    }
    let d = `M${xs[0].toFixed(1)},${ys[0].toFixed(1)}`
    for (let i = 0; i < n - 1; i++) {
      const c1x = xs[i] + dx[i] / 3
      const c1y = ys[i] + (m[i] * dx[i]) / 3
      const c2x = xs[i + 1] - dx[i] / 3
      const c2y = ys[i + 1] - (m[i + 1] * dx[i]) / 3
      d += ` C${c1x.toFixed(1)},${c1y.toFixed(1)} ${c2x.toFixed(1)},${c2y.toFixed(1)} ${xs[i + 1].toFixed(1)},${ys[i + 1].toFixed(1)}`
    }
    return d
  }

  const base = top + plotH
  const right = X(seriesA.length - 1)
  const area = (line: string) => `${line} L${right.toFixed(1)},${base.toFixed(1)} L${padL},${base.toFixed(1)} Z`
  const aLine = linePath(seriesA)
  const bLine = linePath(seriesB)

  // faint horizontal gridlines + right-aligned y-value labels in the gutter
  const grid = []
  for (let i = 0; i <= 4; i++) {
    const gy = top + (plotH / 4) * i
    const val = Math.round(max - (max / 4) * i)
    grid.push(
      <g key={`g${i}`}>
        <line x1={padL} y1={gy} x2={w - padR} y2={gy} stroke="var(--line-soft)" strokeWidth={1} strokeDasharray="2 5" />
        <text x={padL - 6} y={gy + 3} textAnchor="end" fontSize={6.5} fill="var(--tx-faint)" fontFamily="var(--mono)">{val}</text>
      </g>
    )
  }

  // show ~7 labels so the axis stays readable at any bucket count
  const xlab = labels.map((l, i) => {
    if (!l) return null
    return (
      <text key={`x${i}`} x={X(i)} y={h - 6} textAnchor="middle" fontSize={6.5} fill="var(--tx-faint)">{l}</text>
    )
  })

  // map a pointer position to the nearest bucket index (accounting for the
  // left gutter so the first/last points line up with the cursor)
  const onMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const rect = svgRef.current?.getBoundingClientRect()
    if (!rect || rect.width === 0 || n === 0) return
    const svgX = ((e.clientX - rect.left) / rect.width) * w
    const dataFrac = (svgX - padL) / plotW
    setHover(Math.max(0, Math.min(n - 1, Math.round(dataFrac * (n - 1)))))
  }

  const tipLabels = pointLabels ?? labels
  const hx = hover != null ? X(hover) : 0
  // anchor the tooltip so it never runs off the edges
  const frac = hover != null ? hx / w : 0.5
  const tipShift = frac > 0.7 ? '-100%' : frac < 0.3 ? '0%' : '-50%'

  return (
    <div className="trend-wrap" style={{ position: 'relative' }}>
      <svg
        ref={svgRef}
        width="100%"
        viewBox={`0 0 ${w} ${h}`}
        preserveAspectRatio="xMidYMid meet"
        onPointerMove={onMove}
        onPointerLeave={() => setHover(null)}
      >
        <defs>
          <linearGradient id="trendA" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--accent)" stopOpacity={0.32} />
            <stop offset="100%" stopColor="var(--accent)" stopOpacity={0} />
          </linearGradient>
          <linearGradient id="trendB" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--ok)" stopOpacity={0.24} />
            <stop offset="100%" stopColor="var(--ok)" stopOpacity={0} />
          </linearGradient>
        </defs>
        <path d={area(bLine)} fill="url(#trendB)" />
        <path d={area(aLine)} fill="url(#trendA)" />
        {/* grid + axis labels render above the fill so the area never covers them */}
        {grid}
        <path d={bLine} fill="none" stroke="var(--ok)" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" opacity={0.85} />
        <path d={aLine} fill="none" stroke="var(--accent)" strokeWidth={2.2} strokeLinecap="round" strokeLinejoin="round" />
        {xlab}
        {hover != null && (
          <g pointerEvents="none">
            <line x1={hx} y1={top} x2={hx} y2={base} stroke="var(--tx-faint)" strokeWidth={1} strokeDasharray="3 3" />
            <circle cx={hx} cy={Y(seriesB[hover])} r={3.2} fill="var(--panel)" stroke="var(--ok)" strokeWidth={2} />
            <circle cx={hx} cy={Y(seriesA[hover])} r={3.2} fill="var(--panel)" stroke="var(--accent)" strokeWidth={2} />
          </g>
        )}
      </svg>
      {hover != null && (
        <div
          className="trend-tip"
          style={{ left: `${(frac * 100).toFixed(2)}%`, transform: `translateX(${tipShift})` }}
        >
          {tipLabels[hover] && <div className="tt-h">{tipLabels[hover]}</div>}
          <div className="tt-row"><span className="tt-dot" style={{ background: 'var(--accent)' }} />{names[0]}<b>{seriesA[hover]}</b></div>
          <div className="tt-row"><span className="tt-dot" style={{ background: 'var(--ok)' }} />{names[1]}<b>{seriesB[hover]}</b></div>
        </div>
      )}
    </div>
  )
}

export interface GroupRow {
  label: string
  a: number
  b: number
}

/* grouped vertical bars — two series per category (e.g. total vs closed).
   Calm SVG with dashed y-grid + value labels, matching Trend's grid. */
export function GroupedBars({
  rows,
  w = 760,
  h = 240,
  colorA = 'var(--accent)',
  colorB = 'var(--ok)',
}: {
  rows: GroupRow[]
  w?: number
  h?: number
  colorA?: string
  colorB?: string
}) {
  const padB = 26
  const padL = 26
  const top = 18
  const vals = rows.flatMap((r) => [r.a, r.b])
  const max = Math.max(1, ...vals)
  const niceMax = max <= 4 ? max : Math.ceil(max / 5) * 5
  const plotH = h - padB - top
  const plotW = w - padL - 8
  const n = rows.length || 1
  const slot = plotW / n
  const barW = Math.min(28, slot * 0.26)
  const gap = Math.min(8, slot * 0.06)

  const gridLines = []
  for (let i = 0; i <= 4; i++) {
    const gy = top + (plotH / 4) * i
    const val = Math.round(niceMax - (niceMax / 4) * i)
    gridLines.push(
      <g key={`g${i}`}>
        <line x1={padL} y1={gy} x2={w - 8} y2={gy} stroke="var(--line-soft)" strokeWidth={1} strokeDasharray={i ? '2 5' : undefined} />
        <text x={padL - 6} y={gy + 3} textAnchor="end" fontSize={8.5} fill="var(--tx-faint)" fontFamily="var(--mono)">{val}</text>
      </g>
    )
  }

  const bar = (cx: number, v: number, color: string, key: string) => {
    const bh = (v / niceMax) * plotH
    const y = top + plotH - bh
    return (
      <g key={key}>
        <rect x={cx} y={y} width={barW} height={Math.max(bh, 0)} rx={3} style={{ fill: color }} opacity={0.9} />
        {v > 0 && (
          <text x={cx + barW / 2} y={y - 4} textAnchor="middle" fontSize={9.5} fontWeight={600} fill="var(--tx-2)" fontFamily="var(--mono)">{v}</text>
        )}
      </g>
    )
  }

  return (
    <svg width="100%" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="xMidYMid meet">
      {gridLines}
      {rows.map((r, i) => {
        const center = padL + slot * i + slot / 2
        const aX = center - gap / 2 - barW
        const bX = center + gap / 2
        return (
          <g key={i}>
            {bar(aX, r.a, colorA, `a${i}`)}
            {bar(bX, r.b, colorB, `b${i}`)}
            <text x={center} y={h - 8} textAnchor="middle" fontSize={10} fill="var(--tx-3)">{r.label}</text>
          </g>
        )
      })}
    </svg>
  )
}

export interface HbarItem {
  label: string
  val: number | string
  pct: number
  cls?: string
}

/* horizontal bars */
export function Hbars({ items }: { items: HbarItem[] }) {
  return (
    <div className="hbars">
      {items.map((it, i) => (
        <div className="hbar" key={i}>
          <div className="hb-top">
            <span>{it.label}</span>
            <span className="v">{it.val}</span>
          </div>
          <div className="track">
            <i className={it.cls || ''} style={{ width: `${it.pct}%` }} />
          </div>
        </div>
      ))}
    </div>
  )
}

/* attack heatmap (deterministic) */
const HEAT_LV = [0, 1, 3, 2, 0, 1, 4, 2, 1, 0, 2, 3, 1, 0, 4, 2, 1, 3, 0, 1, 0, 2, 1, 4, 2, 0, 1, 2, 3, 0, 1, 0, 3, 1, 2, 4, 0, 1, 2, 1]
export function Heatmap() {
  return (
    <div className="heat">
      {HEAT_LV.map((lv, i) => {
        if (lv === 0) return <div className="cell" key={i} />
        const color = lv >= 4 ? 'var(--crit)' : lv >= 3 ? 'var(--high)' : 'var(--accent)'
        const op = 0.3 + lv * 0.17
        return <div className="cell" key={i} style={{ background: color, opacity: op, borderColor: 'transparent' }} />
      })}
    </div>
  )
}
