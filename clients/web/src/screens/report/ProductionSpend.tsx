import { InfoTip } from '../../shared/InfoTip'
import { reportStamp } from './stamp'

type Kind = 'investigation' | 'hunt' | 'chat'
type RateKind = 'measured' | 'estimated'

type Share = {
  label: string
  k: number
  n: number
  kind: RateKind
  definition: string
  window_start: string
  window_end: string
  window_length: string
  low: number | null
  high: number | null
  bound: string | null
}

type SpendRun = {
  kind: Kind
  name: string | null
  calls: number
  unpriced_calls: number | null
  cost_usd: number | null
  input: number
  output: number
  cache_read: number
  cache_write: number
  wall_ms: number | null
}

type ForecastSource = { source: string; week: number }

type ForecastBeside = {
  not_checked: string | null
  run_id: string | null
  recorded_at: string | null
  sources: ForecastSource[]
  arrivals_reason: string | null
  busiest_hour: number | null
  busiest_at: string | null
  investigations: number | null
  window_start: string
  window_end: string
  window_length: string
  limits_stated: boolean
  covers: number | null
  per_investigation: number | null
}

const SPEND_TIP = {
  source:
    'Spend events on investigations, hunts and chat created in the window, and the latest Day 1 forecast',
  calculation:
    'Wholly priced means at least one call and no unpriced call. Dollars are the sum of those runs. Under 20, a share is k of n. At 20 or above it is a range.',
  limit:
    'Triage and insight are not recorded. A run created before Production Start is left out, because the spend read returns the whole run. Chat stores no conversation.',
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function num(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value ? value : null
}

function figure(value: number): string {
  if (Number.isInteger(value)) return String(value)
  return value.toFixed(2).replace(/0+$/, '').replace(/\.$/, '')
}

function noun(value: number, singular: string, plural: string): string {
  return `${figure(value)} ${figure(value) === '1' ? singular : plural}`
}

function money(value: number): string {
  const shown = value.toFixed(4).replace(/0+$/, '').replace(/\.$/, '')
  return `$${shown}`
}

function pct(value: number): string {
  const shown = value * 100
  const rounded = Math.round(shown)
  return Math.abs(shown - rounded) < 0.05 ? `${rounded}%` : `${shown.toFixed(1)}%`
}

function kindWord(kind: RateKind): string {
  switch (kind) {
    case 'measured':
      return 'Measured'
    case 'estimated':
      return 'Estimated'
    default: {
      const never: never = kind
      return never
    }
  }
}

function duration(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const seconds = total % 60
  if (hours > 0) return `${hours} h ${minutes} min`
  if (minutes > 0) return seconds > 0 ? `${minutes} min ${seconds} s` : `${minutes} min`
  return `${seconds} s`
}

function windowText(start: string, end: string, length: string): string {
  if (!start || !end) return 'window not recorded'
  const span = `${reportStamp(start)} to ${reportStamp(end)}`
  return length ? `${span}, ${length}` : span
}

function rate(share: Share): string {
  const ratio = `${share.k} of ${share.n}`
  if (share.n >= 20 && share.low != null && share.high != null) {
    const point = share.n > 0 ? pct(share.k / share.n) : '0%'
    return `${ratio}, ${point} (${pct(share.low)}–${pct(share.high)})`
  }
  if (share.k === 0 && share.n === 0 && share.bound) return `0. ${share.bound}`
  if (share.k === 0 && share.high != null) return `${ratio}. Upper bound ${pct(share.high)}`
  return ratio
}

function runName(kind: Kind, name: string | null): string {
  switch (kind) {
    case 'investigation':
      return name ?? 'Investigation'
    case 'hunt':
      return name ?? 'Hunt'
    case 'chat':
      return 'Chat'
    default: {
      const never: never = kind
      return never
    }
  }
}

function tokens(run: SpendRun): string {
  return `input ${run.input}, output ${run.output}, cache read ${run.cache_read}, cache write ${run.cache_write}`
}

function wallLine(kind: Kind, wall: number | null): string {
  if (kind === 'chat' || wall == null) return 'Wall time not recorded.'
  return `Wall time ${duration(wall)}.`
}

function kindOf(value: unknown): Kind | null {
  if (value === 'investigation' || value === 'hunt' || value === 'chat') return value
  return null
}

function runOf(value: unknown, dollars: boolean): SpendRun | null {
  if (!isRecord(value)) return null
  const kind = kindOf(value.kind)
  const calls = num(value.calls)
  const input = num(value.input)
  const output = num(value.output)
  const cacheRead = num(value.cache_read)
  const cacheWrite = num(value.cache_write)
  if (kind === null || calls === null || input === null || output === null || cacheRead === null || cacheWrite === null) {
    return null
  }
  const cost = num(value.cost_usd)
  if (dollars && cost === null) return null
  return {
    kind,
    name: typeof value.name === 'string' ? value.name : null,
    calls,
    unpriced_calls: num(value.unpriced_calls),
    cost_usd: cost,
    input,
    output,
    cache_read: cacheRead,
    cache_write: cacheWrite,
    wall_ms: num(value.wall_ms),
  }
}

function runsOf(value: unknown, dollars: boolean): SpendRun[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((row) => {
    const run = runOf(row, dollars)
    return run ? [run] : []
  })
}

function shareOf(value: unknown): Share | null {
  if (!isRecord(value)) return null
  const k = num(value.k)
  const n = num(value.n)
  const kind = value.kind === 'measured' || value.kind === 'estimated' ? value.kind : null
  if (typeof value.label !== 'string' || k === null || n === null || kind === null) return null
  return {
    label: value.label,
    k,
    n,
    kind,
    definition: typeof value.definition === 'string' ? value.definition : '',
    window_start: typeof value.window_start === 'string' ? value.window_start : '',
    window_end: typeof value.window_end === 'string' ? value.window_end : '',
    window_length: typeof value.window_length === 'string' ? value.window_length : '',
    low: num(value.low),
    high: num(value.high),
    bound: typeof value.bound === 'string' ? value.bound : null,
  }
}

function forecastOf(value: unknown): ForecastBeside | null {
  if (!isRecord(value)) return null
  const sources = Array.isArray(value.sources)
    ? value.sources.flatMap((row) => {
        if (!isRecord(row) || typeof row.source !== 'string') return []
        const week = num(row.week)
        return week === null ? [] : [{ source: row.source, week }]
      })
    : []
  return {
    not_checked: text(value.not_checked),
    run_id: text(value.run_id),
    recorded_at: text(value.recorded_at),
    sources,
    arrivals_reason: text(value.arrivals_reason),
    busiest_hour: num(value.busiest_hour),
    busiest_at: text(value.busiest_at),
    investigations: num(value.investigations),
    window_start: typeof value.window_start === 'string' ? value.window_start : '',
    window_end: typeof value.window_end === 'string' ? value.window_end : '',
    window_length: typeof value.window_length === 'string' ? value.window_length : '',
    limits_stated: value.limits_stated === true,
    covers: num(value.covers),
    per_investigation: num(value.per_investigation),
  }
}

function scopeLine(value: unknown): string {
  if (typeof value !== 'string' || !value.trim()) return 'The whole install.'
  const trimmed = value.trim().replace(/\.$/, '')
  return `${trimmed.charAt(0).toUpperCase()}${trimmed.slice(1)}.`
}

function definition(body: Record<string, unknown>, key: string, fallback: string): string {
  const definitions = isRecord(body.definitions) ? body.definitions : null
  const stored = definitions ? text(definitions[key]) : null
  return stored ?? fallback
}

function PricedRow({ run }: { run: SpendRun }) {
  return (
    <div className="d1-row">
      <p>
        {runName(run.kind, run.name)}: {noun(run.calls, 'call', 'calls')}, {money(run.cost_usd ?? 0)}. Measured.
      </p>
      <p className="d1-facts">{tokens(run)}.</p>
      <p className="d1-facts">{wallLine(run.kind, run.wall_ms)}</p>
    </div>
  )
}

function UnpricedRow({ run, partial }: { run: SpendRun; partial: boolean }) {
  const calls = partial
    ? run.unpriced_calls != null
      ? `${figure(run.unpriced_calls)} of ${noun(run.calls, 'call', 'calls')} unpriced, so no dollar figure.`
      : `${noun(run.calls, 'call', 'calls')}, some unpriced, so no dollar figure.`
    : `${noun(run.calls, 'call', 'calls')}, all unpriced, so no dollar figure.`
  return (
    <div className="d1-row">
      <p>
        {runName(run.kind, run.name)}: {calls}
      </p>
      <p className="d1-facts">{tokens(run)}.</p>
      <p className="d1-facts">{wallLine(run.kind, run.wall_ms)}</p>
    </div>
  )
}

function Figures({ body }: { body: Record<string, unknown> }) {
  const share = shareOf(body.share)
  const priced = runsOf(body.priced, true)
  const unpriced = runsOf(body.unpriced, false)
  const partial = runsOf(body.partial, false)
  const totals = isRecord(body.totals) ? body.totals : null
  const cost = totals ? num(totals.cost_usd) : null
  const calls = totals ? num(totals.calls) : null
  const noCalls = num(body.no_calls)
  const unread = num(body.unread)
  const left = isRecord(body.left_out) ? body.left_out : null
  const window = share
    ? windowText(share.window_start, share.window_end, share.window_length)
    : windowText(text(body.window_start) ?? '', text(body.window_end) ?? '', text(body.window_length) ?? '')
  const wallMs = totals ? num(totals.wall_ms) : null
  const wallRecorded = totals ? num(totals.wall_recorded) : null
  const wallMissing = totals ? num(totals.wall_unrecorded) : null
  return (
    <>
      <p>{scopeLine(body.scope)}</p>
      {share && (
        <p>
          {share.label}: {rate(share)}. {kindWord(share.kind)}.
          {share.definition ? ` ${share.definition}.` : ''} Window {window}.
        </p>
      )}
      {cost != null && (
        <p>
          Dollars: {money(cost)}. Measured. {definition(body, 'dollars', 'sum of wholly priced runs')}. Window {window}.
        </p>
      )}
      {priced.length > 0 && calls != null && (
        <p>
          Calls: {noun(calls, 'call', 'calls')}. Measured. {definition(body, 'calls', 'calls on wholly priced runs')}. Window {window}.
        </p>
      )}
      {priced.length > 0 && totals && (
        <p>
          Tokens: input {num(totals.input) ?? 0}, output {num(totals.output) ?? 0}, cache read {num(totals.cache_read) ?? 0}, cache write {num(totals.cache_write) ?? 0}. Measured. {definition(body, 'tokens', 'token totals on wholly priced runs')}. Window {window}.
        </p>
      )}
      {priced.length > 0 && (
        <p>
          Wall time: {wallMs == null || wallRecorded === 0 ? 'not recorded' : `${duration(wallMs)} on ${noun(wallRecorded ?? 0, 'run', 'runs')}`}.
          {wallMs != null && wallMissing != null && wallMissing > 0 ? ` ${noun(wallMissing, 'run', 'runs')} not recorded.` : ''} Measured. {definition(body, 'wall', 'wall time on wholly priced runs')}. Window {window}.
        </p>
      )}
      {(priced.length > 0 || unpriced.length > 0 || partial.length > 0) && (
        <div className="d1-rows">
          {priced.map((run, index) => (
            <PricedRow key={`priced-${run.kind}-${run.name ?? index}`} run={run} />
          ))}
          {unpriced.map((run, index) => (
            <UnpricedRow key={`unpriced-${run.kind}-${run.name ?? index}`} run={run} partial={false} />
          ))}
          {partial.map((run, index) => (
            <UnpricedRow key={`partial-${run.kind}-${run.name ?? index}`} run={run} partial />
          ))}
        </div>
      )}
      {noCalls != null && noCalls > 0 && (
        <p className="d1-facts">
          {noun(noCalls, 'run', 'runs')} with no recorded calls, not counted.
        </p>
      )}
      {unread != null && unread > 0 && (
        <p className="d1-facts">{noun(unread, 'run', 'runs')} could not be read, not counted.</p>
      )}
      {left && num(left.investigations) != null && num(left.hunts) != null && num(left.chat) != null && (
        <p className="d1-facts">
          Left out: {noun(num(left.investigations) ?? 0, 'investigation', 'investigations')}, {noun(num(left.hunts) ?? 0, 'hunt', 'hunts')} and {noun(num(left.chat) ?? 0, 'chat', 'chats')} created before Production Start. The spend read returns the whole run.
        </p>
      )}
    </>
  )
}

function busiestLine(hour: number, at: string | null): string {
  const when = at ? ` (${reportStamp(at)})` : ''
  return `The busiest hour had ${noun(hour, 'alert', 'alerts')}${when}, a lower bound summed across sources.`
}

function ForecastBlock({ forecast }: { forecast: ForecastBeside }) {
  if (forecast.not_checked) return <p>{forecast.not_checked}</p>
  const window = windowText(forecast.window_start, forecast.window_end, forecast.window_length)
  const count = forecast.investigations
  return (
    <>
      <p className="d1-sub">Day 1 forecast</p>
      {forecast.run_id && (
        <p className="d1-facts">
          Day 1 run {forecast.run_id}
          {forecast.recorded_at ? `, recorded ${reportStamp(forecast.recorded_at)}` : ''}.
        </p>
      )}
      {forecast.sources.map((source) => (
        <p key={source.source}>
          {source.source}: A normal week is {noun(source.week, 'alert', 'alerts')}, estimated.
        </p>
      ))}
      {forecast.sources.length === 0 && forecast.arrivals_reason && <p>{forecast.arrivals_reason}</p>}
      {forecast.sources.length === 0 && !forecast.arrivals_reason && (
        <p className="d1-quiet">No per-source week on that forecast.</p>
      )}
      {forecast.busiest_hour != null && <p>{busiestLine(forecast.busiest_hour, forecast.busiest_at)}</p>}
      {count != null && (
        <p>
          {noun(count, 'investigation', 'investigations')} in this window, measured.
          {count === 0 ? ' Bound: none in this window.' : ''} Window {window}.
        </p>
      )}
      {forecast.limits_stated && forecast.covers != null && (
        <p>
          {forecast.per_investigation != null
            ? `The hourly cap covers ${noun(forecast.covers, 'investigation', 'investigations')} at the full ${money(forecast.per_investigation)} per investigation.`
            : `The hourly cap covers ${noun(forecast.covers, 'investigation', 'investigations')}.`}
        </p>
      )}
      {forecast.limits_stated && forecast.covers == null && (
        <p>The hourly cap could not be stated from the saved per-investigation cost.</p>
      )}
    </>
  )
}

export function SpendSection({ evidence }: { evidence: unknown }) {
  const body = isRecord(evidence) ? evidence : null
  const forecast = body ? forecastOf(body.forecast) : null
  return (
    <section className="d1-card" aria-label="Spend since Production Start">
      <div className="d1-card-head">
        <h2>Spend since Production Start</h2>
        <InfoTip label="How spend since Production Start is counted" align="start" {...SPEND_TIP} />
      </div>
      {!body && <p className="d1-quiet">Nothing to report in this section.</p>}
      {body && typeof body.not_checked === 'string' && <p>{body.not_checked}</p>}
      {body && typeof body.not_checked !== 'string' && <Figures body={body} />}
      {body && typeof body.triage_insight === 'string' && <p>{body.triage_insight}</p>}
      {forecast && <ForecastBlock forecast={forecast} />}
    </section>
  )
}
