import { useId, useLayoutEffect, useRef, useState, type CSSProperties } from 'react'
import { Link } from 'react-router-dom'
import { Icon, type IconName } from '../../shared/icons'
import { InfoTip } from '../../shared/InfoTip'
import { useSourceBadge } from '../../shared/useSourceBadge'
import { VigilMark } from '../../shared/VigilLogo'
import type { OverviewOutcome, OverviewPayload } from '../../services/api'
import './overview.css'

const CONNECT_DATA = '/settings?section=data'
const EMPTY_TEXT =
  'Nothing is connected yet. Connect a source on the left and its alerts flow through the Vigil engine to the outcomes on the right.'
const MAX_SOURCES = 7
const MIN_W = 640
const SRC_DOT_X = 206
const SRC_NODE_W = 196
const SLOT_W = 236
const SLOT_DOT_X = 246
const ENGINE_R = 52
const BAND_TOTAL = 150

// The three served states that carry a sub-line, and the colour token of each outcome.
const OUTCOME_SUB: Record<string, string> = {
  resolved_auto: 'closed without a person',
  working: 'cases in progress',
  needs_you: 'alerts waiting on a person',
}
const OUTCOME_COLOR: Record<string, string> = {
  resolved_auto: 'var(--good)',
  resolved_person: 'var(--vio)',
  working: 'var(--ac)',
  needs_you: 'var(--poor)',
  waiting: 'var(--tx3)',
}
const SLOTS: { label: string; sub: string; icon: IconName }[] = [
  { label: 'Connect a SIEM', sub: 'Splunk, Sentinel or Elastic', icon: 'search' },
  { label: 'Connect an EDR', sub: 'CrowdStrike or Defender', icon: 'shield' },
  { label: 'Connect identity', sub: 'Okta or Entra ID', icon: 'user' },
  { label: 'Connect the LogLM pipeline', sub: 'Scores your network flows', icon: 'bolt' },
]

type Hover = { kind: 'src' | 'out'; key: string } | null
interface SrcNode { key: string; label: string; icon: IconName; count: number; href: string | null }

const fmt = (n: number) => n.toLocaleString('en-US')
const alerts = (n: number) => `${fmt(n)} alert${n === 1 ? '' : 's'}`
const rowY = (i: number, n: number, h: number, pad: number, cy: number) =>
  n <= 1 ? cy : pad + (i * (h - 2 * pad)) / (n - 1)

function strand(xa: number, ya: number, xb: number, yb: number) {
  const dx = xb - xa
  return `M ${xa} ${ya} C ${xa + dx * 0.52} ${ya}, ${xb - dx * 0.46} ${yb}, ${xb} ${yb}`
}

function useWidth() {
  const ref = useRef<HTMLDivElement>(null)
  const [w, setW] = useState(0)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    setW(el.clientWidth)
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => setW(el.clientWidth))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  return [ref, Math.max(MIN_W, w)] as const
}

function Particle({ path, color, dur, delay }: { path: string; color: string; dur: number; delay: number }) {
  return (
    <g className="ov-particle" style={{ color }}>
      <circle r="3.6" fill="currentColor" opacity="0.22" />
      <circle r="1.5" fill="currentColor" />
      <circle r="0.7" fill="#fff" />
      <animateMotion dur={`${dur}s`} begin={`${-delay}s`} repeatCount="indefinite" path={path} />
    </g>
  )
}

function Engine({ cx, cy, idle }: { cx: number; cy: number; idle: boolean }) {
  return (
    <div className={`ov-engine${idle ? ' idle' : ''}`} style={{ left: cx - 92, top: cy - 92 }} aria-hidden="true">
      <svg width="184" height="184" viewBox="-92 -92 184 184">
        <defs>
          <radialGradient id="ov-glow">
            <stop offset="0" style={{ stopColor: 'var(--ac)', stopOpacity: idle ? 0.1 : 0.34 }} />
            <stop offset="0.55" style={{ stopColor: 'var(--ac)', stopOpacity: 0.1 }} />
            <stop offset="1" style={{ stopColor: 'var(--ac)', stopOpacity: 0 }} />
          </radialGradient>
        </defs>
        <circle className="ov-glow" r="62" fill="url(#ov-glow)" />
        <circle r="88" className="ov-ring" stroke="var(--tx3)" strokeOpacity={idle ? 0.2 : 0.35} strokeWidth="5" strokeDasharray="0.9 5.3" />
        {idle && <circle r="66" className="ov-ring" stroke="var(--tx2)" strokeOpacity="0.22" strokeWidth="2" strokeDasharray="0 9" />}
        {!idle && (
          <>
            <circle r="78" className="ov-ring spin" style={{ animationDuration: '110s' }} stroke="var(--tx2)" strokeOpacity="0.5" strokeWidth="2.2" strokeDasharray="0 7.2" />
            <circle r="66" className="ov-ring spin rev" style={{ animationDuration: '80s' }} stroke="var(--tx1)" strokeOpacity="0.35" strokeWidth="2" strokeDasharray="0 9" />
            <circle r="54" className="ov-ring spin" style={{ animationDuration: '48s' }} stroke="var(--ac)" strokeOpacity="0.85" strokeWidth="2.4" strokeDasharray="0 11.3" />
            <g className="ov-ring spin rev" style={{ animationDuration: '9s' }}>
              {[[54, 0.18], [26, 0.45], [7, 1]].map(([len, op]) => (
                <circle key={len} r="66" stroke="var(--ac)" strokeOpacity={op} strokeWidth="2" strokeDasharray={`${len} 500`} />
              ))}
            </g>
          </>
        )}
      </svg>
      <VigilMark className="ov-mark" style={{ opacity: idle ? 0.45 : 1 }} />
    </div>
  )
}

interface Props {
  data: OverviewPayload
  wall: boolean
  onToggleWall: () => void
}

export default function FlowDiagram({ data, wall, onToggleWall }: Props) {
  const badgeOf = useSourceBadge()
  const uid = useId().replace(/:/g, '')
  const [ref, W] = useWidth()
  const [hover, setHover] = useState<Hover>(null)
  const H = wall ? 600 : 400
  const cx = W / 2
  const cy = H / 2 - 4
  const empty = data.empty
  const x1 = cx - ENGINE_R // strands end here
  const xr = cx + ENGINE_R // bands start here
  const x2 = W - 198

  const sources = ((): SrcNode[] => {
    const sorted = [...data.arrivals].sort((a, b) => b.count - a.count || a.data_source.localeCompare(b.data_source))
    const one = (a: (typeof sorted)[number]): SrcNode => {
      const b = badgeOf(a.data_source)
      return {
        key: a.data_source,
        label: b.label,
        icon: b.icon as IconName,
        count: a.count,
        href: `/triage?source=${encodeURIComponent(a.data_source)}`,
      }
    }
    if (sorted.length <= MAX_SOURCES) return sorted.map(one)
    const rest = sorted.slice(MAX_SOURCES - 1)
    return [
      ...sorted.slice(0, MAX_SOURCES - 1).map(one),
      { key: '__more', label: `+${rest.length} more sources`, icon: 'more', count: rest.reduce((n, a) => n + a.count, 0), href: null },
    ]
  })()

  const total = data.arrivals.reduce((n, a) => n + a.count, 0)
  const measured = data.outcomes.filter((o): o is OverviewOutcome & { count: number } => o.count !== null)
  const unmeasured = data.outcomes.filter((o) => o.count === null)
  const outTotal = measured.reduce((n, o) => n + o.count, 0)

  // Source strands: each source gets 1..12 strands, spread 1.4px apart where they meet the engine.
  const maxCount = Math.max(1, ...sources.map((s) => s.count))
  const counts = sources.map((s) => (s.count === 0 ? 1 : Math.min(12, Math.max(1, Math.round((12 * s.count) / maxCount)))))
  const strandTotal = counts.reduce((n, c) => n + c, 0)
  let seen = 0
  const strands = sources.map((_, i) => {
    const ya = rowY(i, sources.length, H, 30, cy)
    const paths = Array.from({ length: counts[i] }, (_, k) =>
      strand(SRC_DOT_X, ya, x1, cy + (seen + k - (strandTotal - 1) / 2) * 1.4),
    )
    seen += counts[i]
    return { ya, paths }
  })

  // Outcome bands: stacked at the engine's right edge, landing on five evenly spaced rows.
  let top = cy - BAND_TOTAL / 2
  const bands = measured.map((o, j) => {
    const th = o.count === 0 ? 0 : Math.max(2.5, (BAND_TOTAL * o.count) / (outTotal || 1))
    const y = rowY(j, measured.length, H, 52, cy)
    const t0 = top
    top += th
    const dx = x2 - xr
    const c = (a: number, b: number) => `${xr + dx * 0.5} ${a}, ${x2 - dx * 0.5} ${b}`
    const line = `M ${xr} ${t0 + th / 2} C ${c(t0 + th / 2, y)}, ${x2} ${y}`
    const shape =
      th > 0
        ? `M ${xr} ${t0} C ${c(t0, y - th / 2)}, ${x2} ${y - th / 2} L ${x2} ${y + th / 2} C ${x2 - dx * 0.5} ${y + th / 2}, ${xr + dx * 0.5} ${t0 + th}, ${xr} ${t0 + th} Z`
        : ''
    return { o, y, th, line, shape, zero: strand(xr, cy, x2, y), color: OUTCOME_COLOR[o.state] ?? 'var(--tx3)' }
  })

  const srcOn = (key: string) => hover?.kind === 'src' && hover.key === key
  const outOn = (key: string) => hover?.kind === 'out' && hover.key === key
  const srcDim = (key: string) => (hover?.kind === 'src' && !srcOn(key) ? 0.45 : 1)
  const outDim = (key: string) => (hover?.kind === 'out' && !outOn(key) ? 0.45 : 1)
  const leave = () => setHover(null)

  return (
    <section className="ov-flow" aria-label="How alerts flow through Vigil">
      <div className="ov-flow-h">
        <h2>How alerts flow through Vigil</h2>
        {!empty && (
          <InfoTip
            align="start"
            label="About this diagram"
            source={data.arrivals[0]?.source_text}
            calculation="Line thickness is the number of alerts. Each alert is counted once, at the outcome it reached today (UTC)"
            limit="Hover a source or an outcome to follow it"
          />
        )}
        {wall && (
          <button type="button" className="ov-btn ml-auto" aria-pressed="true" onClick={onToggleWall}>
            <Icon name="fit" size={13} />
            Exit full screen
          </button>
        )}
      </div>
      <p className="ov-flow-sub">
        {empty
          ? EMPTY_TEXT
          : 'Today (UTC). Thicker lines carry more alerts; each alert is counted once, at the outcome it reached. Hover a source or an outcome to follow it.'}
      </p>

      <div ref={ref} className="ov-canvas-wrap">
        <div className="ov-canvas" style={{ width: W, height: H }}>
          <svg className="ov-lines" width={W} height={H} aria-hidden="true">
            <defs>
              {bands.map((b) => {
                const hot = outOn(b.o.state)
                return (
                  <linearGradient key={b.o.state} id={`${uid}-${b.o.state}`} x1="0" x2="1" y1="0" y2="0">
                    <stop offset="0" style={{ stopColor: 'var(--tx1)', stopOpacity: 0.06 }} />
                    <stop offset="0.55" style={{ stopColor: b.color, stopOpacity: hot ? 0.45 : 0.2 }} />
                    <stop offset="1" style={{ stopColor: b.color, stopOpacity: hot ? 0.8 : 0.5 }} />
                  </linearGradient>
                )
              })}
            </defs>

            {empty &&
              SLOTS.map((_, i) => {
                const y = rowY(i, SLOTS.length, H, 30, cy)
                return <path key={i} className="ov-dotted" d={strand(SLOT_DOT_X, y, x1, cy)} />
              })}
            {empty &&
              data.outcomes
                .filter((o) => o.count !== null)
                .map((o, j, all) => (
                  <path key={o.state} className="ov-dotted" d={strand(cx + ENGINE_R, cy, x2, rowY(j, all.length, H, 52, cy))} />
                ))}

            {!empty &&
              sources.map((s, i) => {
                const { ya, paths } = strands[i]
                const on = srcOn(s.key)
                const faded = hover?.kind === 'src' && !on
                return (
                  <g key={s.key}>
                    {paths.map((d, k) => (
                      <path
                        key={k}
                        d={d}
                        className={`ov-strand${s.count === 0 ? ' zero' : ''}`}
                        style={on ? { stroke: 'var(--ac)', strokeOpacity: 0.6, strokeWidth: 1.1 } : faded ? { strokeOpacity: 0.05 } : undefined}
                      />
                    ))}
                    <circle className="ov-dot" cx={SRC_DOT_X} cy={ya} r="4" />
                  </g>
                )
              })}

            {!empty &&
              bands.map((b) => {
                const hot = outOn(b.o.state)
                const faded = hover?.kind === 'out' && !hot
                return (
                  <g key={b.o.state} style={{ opacity: faded ? 0.3 : 1 }} className="ov-band">
                    {b.th > 0 ? (
                      <path
                        d={b.shape}
                        fill={`url(#${uid}-${b.o.state})`}
                        onMouseEnter={() => setHover({ kind: 'out', key: b.o.state })}
                        onMouseLeave={leave}
                      />
                    ) : (
                      <path d={b.zero} className="ov-zero" />
                    )}
                    <circle cx={x2} cy={b.y} r="5" className="ov-dot-out" style={{ stroke: b.color }} />
                  </g>
                )
              })}

            {empty &&
              data.outcomes
                .filter((o) => o.count !== null)
                .map((o, j, all) => (
                  <circle key={o.state} className="ov-dot" cx={x2} cy={rowY(j, all.length, H, 52, cy)} r="4" />
                ))}
            {empty &&
              SLOTS.map((_, i) => <circle key={i} className="ov-dot" cx={SLOT_DOT_X} cy={rowY(i, SLOTS.length, H, 30, cy)} r="4" />)}

            {!empty &&
              sources.map((s, i) =>
                s.count > 0 ? (
                  <Particle key={s.key} path={strands[i].paths[Math.floor(strands[i].paths.length / 2)]} color="var(--ac)" dur={3.8 + (i % 4) * 0.7} delay={i * 0.9} />
                ) : null,
              )}
            {!empty &&
              bands.map((b, j) =>
                b.th > 0 ? <Particle key={b.o.state} path={b.line} color={b.color} dur={4.2 + (j % 3) * 0.9} delay={j * 1.1} /> : null,
              )}
          </svg>

          <Engine cx={cx} cy={cy} idle={empty} />
          {!empty && (
            <div className="ov-total" style={{ left: cx - 270, top: cy - 96 }}>
              <b>{fmt(total)}</b>
              <span>alerts in</span>
            </div>
          )}
          <div className="ov-engine-label" style={{ left: cx - 92, top: cy + 96 }}>
            <b>Vigil engine</b>
            {empty && <span>Waiting for data</span>}
          </div>

          {empty
            ? SLOTS.map((s, i) => (
                <Link
                  key={s.label}
                  to={CONNECT_DATA}
                  className="ov-slot"
                  style={{ left: 0, top: rowY(i, SLOTS.length, H, 30, cy) - 22, width: SLOT_W }}
                >
                  <span className="ov-tile accent"><Icon name={s.icon} size={15} /></span>
                  <span className="ov-txt">
                    <b>{s.label}</b>
                    <span>{s.sub}</span>
                  </span>
                </Link>
              ))
            : sources.map((s, i) => {
                const body = (
                  <>
                    <span className="ov-tile"><Icon name={s.icon} size={15} /></span>
                    <span className="ov-txt">
                      <b>{s.label}</b>
                      <span>{alerts(s.count)}</span>
                    </span>
                  </>
                )
                const style: CSSProperties = { left: 0, top: strands[i].ya - 19, width: SRC_NODE_W, opacity: srcDim(s.key) }
                const title = `${s.label}: ${alerts(s.count)} today.${s.href ? ' Opens the Triage queue for this source.' : ''}`
                return s.href ? (
                  <Link
                    key={s.key}
                    to={s.href}
                    className="ov-src"
                    style={style}
                    title={title}
                    onMouseEnter={() => setHover({ kind: 'src', key: s.key })}
                    onMouseLeave={leave}
                    onFocus={() => setHover({ kind: 'src', key: s.key })}
                    onBlur={leave}
                  >
                    {body}
                  </Link>
                ) : (
                  <div
                    key={s.key}
                    className="ov-src"
                    style={style}
                    title={title}
                    onMouseEnter={() => setHover({ kind: 'src', key: s.key })}
                    onMouseLeave={leave}
                  >
                    {body}
                  </div>
                )
              })}

          {empty
            ? data.outcomes
                .filter((o) => o.count !== null)
                .map((o, j, all) => (
                  <div key={o.state} className="ov-oslot" style={{ left: x2 + 14, top: rowY(j, all.length, H, 52, cy) - 21 }}>
                    {o.label}
                  </div>
                ))
            : bands.map((b) => (
                <div
                  key={b.o.state}
                  className="ov-out"
                  role="group"
                  aria-label={b.o.label}
                  style={{ left: x2 + 14, top: b.y - 19, opacity: outDim(b.o.state) }}
                  onMouseEnter={() => setHover({ kind: 'out', key: b.o.state })}
                  onMouseLeave={leave}
                >
                  <span className="ov-out-l">
                    <b style={{ color: b.color }}>{fmt(b.o.count)}</b>
                    <span>{b.o.label}</span>
                  </span>
                  {(OUTCOME_SUB[b.o.state] || b.o.info) && (
                    <span className="ov-out-s">
                      {OUTCOME_SUB[b.o.state]}
                      {b.o.info && <InfoTip label={b.o.info} text={b.o.info} />}
                    </span>
                  )}
                </div>
              ))}
        </div>
      </div>

      {!empty && unmeasured.length > 0 && (
        <p className="ov-unmeasured">
          {unmeasured[0].unmeasured_text ?? 'Not measured yet'}:{' '}
          {unmeasured.map((o, i) => (
            <span key={o.state}>
              {i > 0 && ' · '}
              <span title={o.info ?? undefined}>{o.label}</span>
            </span>
          ))}
        </p>
      )}
    </section>
  )
}
