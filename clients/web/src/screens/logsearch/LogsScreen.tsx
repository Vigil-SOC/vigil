import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'
import { useSearchParams } from 'react-router-dom'
import { EmptyState } from '../../shared/ui'
import { logsApi, type LogRow, type LogSearchArgs, type LogSearchResult, type LogSourceInfo } from '../../services/api'
import {
  DAY, MIN, RANGES, formatQuery, isRelative, parseQuery, rangeFromParam, rangeToParam, sourceName, termLabel, toCsv, windowOf,
  type Range,
} from './logsQuery'
import './logs.css'

/* Every telemetry event Vigil holds, every source in one schema: search it, narrow
   it by source and time, open one event to see the sender's own record. Read-only;
   events arrive through POST /api/logs/events. */

const PAGE = 150
const BUCKETS = 60
const LIVE_EVERY = 5000

const SAVED: { label: string; q: string }[] = [
  { label: 'All events', q: '' },
  { label: 'Tied to a case', q: 'incident:*' },
  { label: 'Unusual per LogLM (≥ 0.7)', q: 'loglm>0.7' },
  { label: 'High and critical', q: 'sev:high,critical' },
  { label: 'Failed sign-ins', q: 'action:logon_failed,signin_failed' },
  { label: 'Firewall denies', q: 'action:deny,drop,block' },
]

const HELP: [string, string][] = [
  ['powershell', 'the word anywhere in the event, including the original record'],
  ['"failed logon"', 'the exact phrase'],
  ['host:web-01', 'source or destination host contains web-01'],
  ['ip:10.0.10.20  ip:10.0.10.', 'an exact IP, or every IP that starts that way'],
  ['user:j.doe  port:445', 'a person, a destination port'],
  ['source:sysmon,crowdstrike  beacon,c2', 'a comma means either'],
  ['sev:high  class:auth  action:deny', 'severity, event class, what happened'],
  ['mitre:T1059  incident:CASE-7', 'an ATT&CK technique, a case'],
  ['loglm>0.7', 'LogLM scored it at least this unusual'],
  ['-source:entra  -chicago', 'a minus sign leaves matches out'],
]

const iso = (t: number) => new Date(t).toISOString()
const hm = (t: number) => iso(t).slice(11, 16)
const dayName = (t: number) => new Date(t).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' })
const dayHm = (t: number) => `${dayName(t)} ${hm(t)}`

/** "today 18:30–22:00", "26 Oct 18:30–22:00" or "20 Oct 16:55 – 27 Oct 16:55". */
function spanLabel(a: number, b: number, now: number): string {
  const day = (t: number) => Math.floor(t / DAY)
  if (day(a) !== day(b - 1)) return `${dayHm(a)} – ${dayHm(b)}`
  const d = day(now) - day(a)
  const name = d === 0 ? 'today' : d === 1 ? 'yesterday' : dayName(a)
  return `${name} ${hm(a)}–${hm(b)}`
}
const fromInput = (v: string) => Date.parse(`${v}:00Z`)
const toInput = (t: number) => iso(t).slice(0, 16)

function ago(t: number | null, now: number): string {
  if (!t) return 'no events yet'
  const s = Math.max(0, Math.round((now - t) / 1000))
  if (s < 60) return `${s} s ago`
  if (s < 3600) return `${Math.round(s / 60)} min ago`
  if (s < 86400) return `${Math.round(s / 3600)} h ago`
  return `${Math.round(s / 86400)} d ago`
}

function Histogram({ result, onZoom }: { result: LogSearchResult; onZoom: (start: number, end: number) => void }) {
  const { counts, incident } = result.histogram
  const n = counts.length || 1
  const svg = useRef<SVGSVGElement>(null)
  const [drag, setDrag] = useState<[number, number] | null>(null)
  const [hover, setHover] = useState<number | null>(null)
  const max = Math.max(1, ...counts)
  const W = 840
  const H = 92
  const bw = W / n
  const span = (result.end - result.start) / n
  const long = result.end - result.start > DAY * 0.9
  const at = (e: ReactPointerEvent) => {
    const box = svg.current!.getBoundingClientRect()
    return Math.min(n - 1, Math.max(0, Math.floor(((e.clientX - box.left) / box.width) * n)))
  }
  const sel = drag ? [Math.min(...drag), Math.max(...drag)] : null
  return (
    <div className="lg-histwrap">
      <svg
        ref={svg}
        className="lg-hist"
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={`Events over time, ${counts.reduce((a, b) => a + b, 0)} in all; events tied to a case in red. Drag across the bars to zoom in.`}
        onPointerDown={(e) => { (e.target as Element).setPointerCapture?.(e.pointerId); const i = at(e); setDrag([i, i]) }}
        onPointerMove={(e) => { const i = at(e); setHover(i); if (drag) setDrag([drag[0], i]) }}
        onPointerLeave={() => setHover(null)}
        onPointerUp={() => {
          if (sel) onZoom(result.start + sel[0] * span, result.start + (sel[1] + 1) * span)
          setDrag(null)
        }}
      >
        {counts.map((c, i) => {
          const h = (c / max) * (H - 20)
          const hi = ((incident[i] ?? 0) / max) * (H - 20)
          return (
            <g key={i}>
              <rect x={i * bw} y={0} width={bw} height={H - 14} fill="transparent" />
              <rect x={i * bw + 1} y={H - 14 - h} width={Math.max(1, bw - 2)} height={h} rx={1.5} fill="var(--ac)" opacity={hover === i ? 0.6 : 0.35} />
              {hi > 0 && <rect x={i * bw + 1} y={H - 14 - hi} width={Math.max(1, bw - 2)} height={hi} rx={1.5} fill="var(--poor)" />}
            </g>
          )
        })}
        {sel && <rect x={sel[0] * bw} y={0} width={(sel[1] - sel[0] + 1) * bw} height={H - 14} fill="var(--ac)" opacity={0.15} stroke="var(--ac)" />}
        {[0, 0.25, 0.5, 0.75, 1].map((f) => (
          <text key={f} x={Math.min(W - (long ? 62 : 30), f * W)} y={H - 2} fontSize="10" fill="var(--tx2)">{(long ? dayHm : hm)(result.start + f * (result.end - result.start))}</text>
        ))}
      </svg>
      {hover !== null && !drag && (
        <span className="lg-histtip" style={{ left: `${((hover + 0.5) / n) * 100}%` }}>
          {(long ? dayHm : hm)(result.start + hover * span)}–{hm(result.start + (hover + 1) * span)} · {counts[hover].toLocaleString()} events{incident[hover] ? ` · ${incident[hover]} tied to a case` : ''}
        </span>
      )}
    </div>
  )
}

/** Add-to-search (+) and leave-out (−) for one field of an event. */
function Pivot({ term, onAdd }: { term: string; onAdd: (term: string) => void }) {
  return (
    <span className="lg-pivot">
      <button type="button" onClick={() => onAdd(term)} aria-label={`Search for ${term}`} title={`Search for ${term}`}>+</button>
      <button type="button" onClick={() => onAdd(`-${term}`)} aria-label={`Leave out ${term}`} title={`Leave out ${term}`}>−</button>
    </span>
  )
}

function Score({ value }: { value: number | null }) {
  if (value === null) return <span className="lg-muted">—</span>
  const level = value >= 0.7 ? 'poor' : value >= 0.4 ? 'fair' : 'good'
  return (
    <span className="lg-score" title={`LogLM anomaly score ${value.toFixed(2)}`}>
      <span className="lg-meter"><i className={level} style={{ width: `${Math.round(value * 100)}%` }} /></span>
      {value.toFixed(2)}
    </span>
  )
}

const dash = <span className="lg-muted">—</span>

export default function LogsScreen() {
  const [params, setParams] = useSearchParams()
  const [q, setQ] = useState(params.get('q') ?? '')
  const [applied, setApplied] = useState(params.get('q') ?? '')
  const [range, setRange] = useState<Range>(() => rangeFromParam(params.get('range')))
  const [prevRange, setPrevRange] = useState<Range | null>(null)
  const [off, setOff] = useState<Set<string>>(new Set())
  const [open, setOpen] = useState<string | null>(null)
  const [result, setResult] = useState<LogSearchResult | null>(null)
  const [rows, setRows] = useState<LogRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [down, setDown] = useState(false)
  const [live, setLive] = useState(false)
  const [fresh, setFresh] = useState<Set<string>>(new Set())
  const [sources, setSources] = useState<LogSourceInfo[]>([])
  const [help, setHelp] = useState(false)
  const [now, setNow] = useState(Date.now())
  const [attempt, setAttempt] = useState(0)
  const abort = useRef<AbortController | null>(null)
  const shownIds = useRef<Set<string>>(new Set())

  // follow ?q= and ?range= when another screen links here
  useEffect(() => {
    const next = params.get('q') ?? ''
    setQ(next)
    setApplied(next)
    if (params.get('range')) setRange(rangeFromParam(params.get('range')))
  }, [params])

  // every source Vigil holds, plus any the search matched that the list hasn't caught up with
  const keys = useMemo(() => {
    const all = new Set(sources.map((s) => s.source))
    for (const k of Object.keys(result?.bySource ?? {})) all.add(k)
    return [...all].sort()
  }, [sources, result])

  const argsFor = useCallback((offset: number, at = Date.now()): LogSearchArgs => {
    const [start, end] = windowOf(range, at)
    const keep = off.size ? keys.filter((s) => !off.has(s)) : null
    return { q: applied, start, end, sources: keep, offset, limit: PAGE, buckets: BUCKETS }
    // keys only matter once a source is hidden; leaving them out stops a refetch per sources poll
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [applied, range, off])

  const run = useCallback(async (quiet = false) => {
    abort.current?.abort()
    const ctl = new AbortController()
    abort.current = ctl
    if (!quiet) setLoading(true)
    try {
      const r = await logsApi.search(argsFor(0), ctl.signal)
      if (ctl.signal.aborted) return
      const seen = shownIds.current
      setFresh(quiet ? new Set(r.events.filter((x) => !seen.has(x.id)).map((x) => x.id)) : new Set())
      shownIds.current = new Set(r.events.map((x) => x.id))
      setResult(r)
      setRows(r.events)
      setError(null)
      setDown(false)
    } catch (e) {
      if (ctl.signal.aborted) return
      const res = (e as { response?: { status?: number; data?: { detail?: unknown } } }).response
      if (res?.status === 422) {
        setError(typeof res.data?.detail === 'string' ? res.data.detail : 'That search could not be read.')
      } else {
        setDown(true)
      }
    } finally {
      if (!ctl.signal.aborted) setLoading(false)
    }
  }, [argsFor])

  useEffect(() => { run() }, [run, attempt])
  useEffect(() => () => abort.current?.abort(), [])

  useEffect(() => {
    logsApi.sources().then(setSources).catch(() => setSources([]))
  }, [attempt])

  // live tail: a relative range slides forward while nobody is reading further down
  const paused = open !== null || rows.length > PAGE
  useEffect(() => {
    if (!live || !isRelative(range)) return
    const id = setInterval(() => {
      setNow(Date.now())
      if (!paused) run(true)
      logsApi.sources().then(setSources).catch(() => undefined)
    }, LIVE_EVERY)
    return () => clearInterval(id)
  }, [live, range, paused, run])
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 30_000)
    return () => clearInterval(id)
  }, [])

  const more = async () => {
    if (!result) return
    const args = { ...argsFor(rows.length), start: result.start, end: result.end }
    const r = await logsApi.search(args).catch(() => null)
    if (r) setRows((prev) => [...prev, ...r.events.filter((e) => !prev.some((p) => p.id === e.id))])
  }

  const syncUrl = (nextQ: string, nextRange: Range) => {
    const p: Record<string, string> = {}
    if (nextQ) p.q = nextQ
    if (nextRange.key !== '24h') p.range = rangeToParam(nextRange)
    setParams(p, { replace: true })
  }
  const submit = (next: string) => {
    setQ(next)
    setApplied(next)
    setOpen(null)
    setAttempt((n) => n + 1) // the same search again still searches again
    syncUrl(next, range)
  }
  const pickRange = (r: Range) => {
    setPrevRange(null)
    setRange(r)
    setOpen(null)
    syncUrl(applied, r)
  }
  const zoom = (start: number, end: number) => {
    setPrevRange((p) => p ?? range)
    const r: Range = { key: 'custom', start: Math.round(start), end: Math.round(end) }
    setRange(r)
    setLive(false)
    syncUrl(applied, r)
  }
  const addTerm = (term: string) => submit(applied.trim() ? `${applied.trim()} ${term}` : term)
  const terms = useMemo(() => parseQuery(applied), [applied])
  const dropTerm = (i: number) => submit(formatQuery(terms.filter((_, j) => j !== i)))
  const toggle = (s: string) =>
    setOff((prev) => {
      const n = new Set(prev)
      if (n.has(s)) n.delete(s)
      else n.add(s)
      return n
    })
  const only = (s: string) => setOff(new Set(keys.filter((k) => k !== s)))
  const around = (r: LogRow) => {
    setPrevRange((p) => p ?? range)
    const t = r.ts * 1000
    const next: Range = { key: 'custom', start: t - 5 * MIN, end: t + 5 * MIN }
    const host = r.srcHost ?? r.dstHost
    const nq = host ? `host:${host}` : ''
    setRange(next)
    setLive(false)
    setQ(nq)
    setApplied(nq)
    setOpen(null)
    syncUrl(nq, next)
  }
  const exportCsv = () => {
    const blob = new Blob([toCsv(rows)], { type: 'text/csv' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `vigil-logs-${iso(Date.now()).slice(0, 16).replace(/[:T]/g, '')}.csv`
    a.click()
    URL.revokeObjectURL(a.href)
  }

  const counts = result?.bySource ?? {}
  const long = result ? result.end - result.start > DAY * 0.9 : false
  const when = (r: LogRow) => (long ? `${dayName(r.ts * 1000)} ${r.time.slice(11, 19)}` : r.time.slice(11, 19))
  const total = result?.total ?? 0
  const stored = sources.reduce((a, s) => a + s.events, 0)
  const lastIn = sources.reduce((a, s) => Math.max(a, Date.parse(s.last)), 0) || result?.newest || null
  const relative = isRelative(range)
  const [ws, we] = result ? [result.start, result.end] : windowOf(range, now)
  const hostCell = (ip: string | null, label: string | null) => (label || ip ? <span title={ip ?? undefined}>{label ?? ip}</span> : dash)

  if (down && !result) {
    return (
      <div className="lg-empty">
      <EmptyState
        icon="alert"
        error
        title="Couldn't reach the log store"
        body="The Logs API didn't answer. Check that the backend is running, then try again."
        primary={{ label: 'Try again', onClick: () => setAttempt((n) => n + 1) }}
      />
      </div>
    )
  }

  if (!loading && sources.length === 0 && total === 0 && !applied) {
    return (
      <div className="lg-empty">
      <EmptyState
        icon="doc"
        title="No events yet"
        body="Vigil keeps the telemetry that connectors and forwarders post to /api/logs/events, every source in one schema. Once events arrive they show up here, searchable."
      />
      </div>
    )
  }

  return (
    <div className="lg">
      <aside className="lg-card lg-side">
        <h3>Saved searches</h3>
        <div className="lg-list">
          {SAVED.map((s) => (
            <button key={s.label} type="button" className={applied === s.q ? 'on' : undefined} onClick={() => submit(s.q)}>
              {s.label}
            </button>
          ))}
        </div>
        <h3>Sources</h3>
        <div className="lg-list">
          {keys.map((s) => {
            const info = sources.find((x) => x.source === s)
            const last = info ? Date.parse(info.last) : null
            const flowing = last !== null && now - last < 15 * MIN
            return (
              <button key={s} type="button" className="lg-srcrow" onClick={() => only(s)} title={`Show only ${sourceName(s)}`}>
                <span className="lg-src">{sourceName(s)}</span>
                {info && (
                  <span className="lg-srcmeta">
                    {info.events.toLocaleString()} events
                    <br />
                    <span className={`lg-dot${flowing ? '' : ' off'}`} />last {ago(last, now)}
                  </span>
                )}
              </button>
            )
          })}
        </div>
      </aside>
      <div className="lg-main">
        <form className="lg-query" onSubmit={(e) => { e.preventDefault(); submit(q) }}>
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Escape' && q) { e.preventDefault(); setQ(applied) } }}
            placeholder='Search: words, "a phrase", host:web-01 user:j.doe ip:10.0.10.20 port:445 -source:entra loglm>0.7'
            aria-label="Search events"
            spellCheck={false}
          />
          <select className="lg-range" aria-label="Time range" value={range.key} onChange={(e) => {
            const key = e.target.value as Range['key']
            if (key === 'custom') { const [s, en] = windowOf(range); pickRange({ key, start: s, end: en }) } else pickRange({ key })
          }}>
            {RANGES.map((r) => <option key={r.key} value={r.key}>{r.label}</option>)}
          </select>
          <button type="submit" className="btn primary">Search</button>
          <button type="button" className={`btn ghost lg-live${live && relative ? ' on' : ''}`} aria-pressed={live && relative} disabled={!relative} title={relative ? 'Keep the newest events coming in' : 'Live works with a "Last …" range'} onClick={() => setLive(!live)}>
            <span className={`lg-dot${live && relative ? ' pulse' : ' off'}`} />Live
          </button>
          <button type="button" className="btn ghost" aria-expanded={help} onClick={() => setHelp(!help)}>Search help</button>
        </form>
        {range.key === 'custom' && (
          <div className="lg-row lg-custom">
            <label>From (UTC) <input type="datetime-local" value={toInput(range.start ?? ws)} onChange={(e) => { const t = fromInput(e.target.value); if (t) pickRange({ ...range, start: t }) }} /></label>
            <label>To (UTC) <input type="datetime-local" value={toInput(range.end ?? we)} onChange={(e) => { const t = fromInput(e.target.value); if (t) pickRange({ ...range, end: t }) }} /></label>
            {prevRange && <button type="button" className="btn ghost" onClick={() => pickRange(prevRange)}>Back to {RANGES.find((r) => r.key === prevRange.key)?.label.toLowerCase()}</button>}
          </div>
        )}
        {help && (
          <div className="lg-card lg-help" role="note">
            <dl>
              {HELP.map(([k, v]) => (
                <Fragment key={k}><dt><button type="button" className="lg-link lg-mono" onClick={() => submit(k.split('  ')[0])}>{k}</button></dt><dd>{v}</dd></Fragment>
              ))}
            </dl>
            <p className="lg-muted">Everything you type has to match. Use <b>+</b> beside a value in an event to add it, or <b>−</b> to leave it out.</p>
          </div>
        )}
        {terms.length > 0 && (
          <div className="lg-row" aria-label="Search filters">
            {terms.map((t, i) => (
              <span key={`${i}-${t.key}-${t.value}`} className={`lg-term${t.negate ? ' not' : ''}`}>
                {termLabel(t)}
                <button type="button" aria-label={`Remove ${termLabel(t)}`} onClick={() => dropTerm(i)}>×</button>
              </span>
            ))}
            {terms.length > 1 && <button type="button" className="lg-link" onClick={() => submit('')}>Clear all</button>}
          </div>
        )}
        {error && <div className="lg-card lg-error" role="alert">{error}</div>}
        {down && result && <div className="lg-card lg-error" role="alert">The log store stopped answering; these are the last results it sent.</div>}
        <div className="lg-row">
          {keys.map((s) => (
            <button key={s} type="button" className="lg-chip" aria-pressed={!off.has(s)} onClick={() => toggle(s)} onDoubleClick={() => only(s)} title="Click to hide or show; double-click to show only this source">
              {sourceName(s)}
              <span className="n">{(counts[s] ?? 0).toLocaleString()}</span>
            </button>
          ))}
          {off.size > 0 && <button type="button" className="lg-link" onClick={() => setOff(new Set())}>Show all sources</button>}
        </div>
        <div className="lg-card lg-histcard" aria-busy={loading}>
          <div className="lg-row lg-between">
            <span className="lg-muted lg-small" aria-live="polite">
              {loading && !result ? 'Searching…' : `${total.toLocaleString()} events`}, {spanLabel(ws, we, now)} UTC
              {stored > 0 && <> · {stored.toLocaleString()} stored</>}
              {live && relative && <> · {paused ? 'live paused while you read' : `live, newest ${ago(lastIn, now)}`}</>}
            </span>
            <span className="lg-row">
              {range.key === 'custom' && prevRange && <button type="button" className="lg-link" onClick={() => pickRange(prevRange)}>Zoom out</button>}
              <span className="lg-legend"><span><i className="case" />tied to a case</span><span><i />other</span></span>
            </span>
          </div>
          {result && <Histogram result={result} onZoom={zoom} />}
        </div>
        <div className="lg-row lg-between">
          <span className="lg-muted lg-small">
            {rows.length ? `Newest first · showing ${rows.length.toLocaleString()} of ${total.toLocaleString()}` : ''}
          </span>
          {rows.length > 0 && <button type="button" className="lg-link" onClick={exportCsv}>Export {rows.length.toLocaleString()} rows as CSV</button>}
        </div>
        <div className={`lg-table-wrap${loading && result ? ' dim' : ''}`}>
          <table className="lg-table">
            {/* fixed columns; Event takes the rest and wraps, so the page never scrolls sideways */}
            <colgroup>
              <col className="c-time" style={long ? { width: 132 } : undefined} />
              <col className="c-src" /><col className="c-class" /><col className="c-host" /><col className="c-host" /><col className="c-user" /><col /><col className="c-score" />
            </colgroup>
            <thead>
              <tr>
                <th>Time (UTC)</th><th>Source</th><th className="c-class">Class</th><th>From</th><th>To</th><th>User</th><th>Event</th><th title="LogLM anomaly score">Unusual</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <Fragment key={r.id}>
                  <tr className={`click${r.incident ? ' hit' : ''}${open === r.id ? ' open' : ''}${fresh.has(r.id) ? ' new' : ''}`} onClick={() => setOpen(open === r.id ? null : r.id)} aria-expanded={open === r.id}>
                    <td className="lg-ts">{when(r)}</td>
                    <td>{sourceName(r.source)}</td>
                    <td className="c-class lg-nowrap"><span className={`lg-sev ${r.severity}`} aria-label={r.severity} /> {r.class || dash}</td>
                    <td>{hostCell(r.srcIp, r.srcHost)}</td>
                    <td>{hostCell(r.dstIp, r.dstHost)}{r.dstPort ? <span className="lg-muted">:{r.dstPort}</span> : null}</td>
                    <td>{r.user ?? dash}</td>
                    <td className="lg-ev">
                      {r.summary}
                      {r.tags.map((t) => <span key={t} className="lg-tag">{t}</span>)}
                    </td>
                    <td><Score value={r.loglm} /></td>
                  </tr>
                  {open === r.id && (
                    <tr className="open">
                      <td colSpan={8}>
                        <div className="lg-grid2">
                          <div>
                            <h4>Vigil schema</h4>
                            <dl className="lg-kv">
                              <dt>event.id</dt><dd className="lg-mono">{r.id}</dd>
                              <dt>event.time</dt><dd className="lg-mono">{r.time}</dd>
                              <dt>event.class</dt><dd>{r.class || '—'}{r.class && <Pivot term={`class:${r.class.split(' ')[0]}`} onAdd={addTerm} />}</dd>
                              <dt>source</dt><dd>{sourceName(r.source)} <Pivot term={`source:${r.source}`} onAdd={addTerm} /></dd>
                              <dt>src.ip / host</dt><dd>{r.srcIp ?? '—'} {r.srcHost && r.srcHost !== r.srcIp ? `(${r.srcHost})` : ''}{r.srcIp && <Pivot term={`ip:${r.srcIp}`} onAdd={addTerm} />}</dd>
                              <dt>dst.ip / host</dt><dd>{r.dstIp ?? '—'} {r.dstHost && r.dstHost !== r.dstIp ? `(${r.dstHost})` : ''}{r.dstPort ? ` port ${r.dstPort}` : ''}{r.dstIp && <Pivot term={`ip:${r.dstIp}`} onAdd={addTerm} />}</dd>
                              <dt>actor.user</dt><dd>{r.user ?? '—'}{r.user && <Pivot term={`user:${r.user}`} onAdd={addTerm} />}</dd>
                              <dt>activity</dt><dd>{r.action ?? '—'}{r.action && <Pivot term={`action:${r.action}`} onAdd={addTerm} />}</dd>
                              <dt>severity</dt><dd>{r.severity} <Pivot term={`sev:${r.severity}`} onAdd={addTerm} /></dd>
                              <dt>attack.technique</dt><dd>{r.mitre ?? '—'}{r.mitre && <Pivot term={`mitre:${r.mitre}`} onAdd={addTerm} />}</dd>
                              <dt>loglm.anomaly</dt><dd>{r.loglm ?? '—'}</dd>
                              <dt>case</dt><dd>{r.incident ?? '—'}{r.incident && <Pivot term={`incident:${r.incident}`} onAdd={addTerm} />}</dd>
                            </dl>
                            <div className="lg-row lg-actions">
                              <button type="button" className="btn ghost" onClick={() => around(r)}>Events 5 min either side{r.srcHost || r.dstHost ? ` on ${r.srcHost ?? r.dstHost}` : ''}</button>
                            </div>
                          </div>
                          <div>
                            <h4>Original record</h4>
                            <pre className="lg-pre">{JSON.stringify(r.raw, null, 2)}</pre>
                          </div>
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
        {rows.length < total && (
          <button type="button" className="btn ghost lg-more" onClick={more}>
            Show {Math.min(PAGE, total - rows.length).toLocaleString()} more of {(total - rows.length).toLocaleString()}
          </button>
        )}
        {!loading && rows.length === 0 && !error && (
          <p className="lg-muted">
            No events match{off.size ? ' with some sources hidden' : ''}, {spanLabel(ws, we, now)} UTC.
            {range.key !== '7d' && <> <button type="button" className="lg-link" onClick={() => pickRange({ key: '7d' })}>Search the last 7 days</button></>}
          </p>
        )}
      </div>
    </div>
  )
}
