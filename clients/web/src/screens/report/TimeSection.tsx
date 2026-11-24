import { InfoTip } from '../../shared/InfoTip'
import { reportStamp } from './stamp'

type Band = { label: string; count: number }
type LeftOut = { restricted?: number; sample?: number; probe?: number }
type Figure = {
  count: number
  not_recorded: number
  median: string | null
  p90: string | null
  bands: Band[] | null
  left_out: LeftOut
}
type StoredTime = {
  definition?: string
  kind?: string
  count?: number
  median?: string | null
  p90?: string | null
  bands?: Band[] | null
  opened_missing?: number
}
type Kind = 'measured' | 'estimated'
type Source = {
  source: string
  kind: Kind
  fetch_time: string | null
  time_to_first_verdict: Figure
  time_to_close: Figure
}
type Before = {
  recorded_at: string | null
  not_checked: string | null
  sources: { source: string; time_to_close: StoredTime[] }[]
}
type Body = {
  window?: { start?: string | null; end?: string | null; length?: string | null }
  close_note?: string
  sources?: Source[]
  before?: Before
}

const TIP = {
  source: 'Findings whose source time falls in the production window, and cases closed in that window',
  calculation:
    'Time to first verdict is the source time to the first recorded verdict. Time to close is the source time to the earliest case close in the window. Median and p90 from 20 timed alerts; under 20, counts in three bands.',
  limit:
    'Hidden without findings.read. A verdict from before this column existed reads not recorded. Defender, Splunk, CrowdStrike, Elastic, Security Hub, OpenSearch, Sentinel and Cloudflare store the fetch time, and those alerts cannot be told apart. A short window over-represents quick closes.',
}

function kindWord(kind: Kind): string {
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function figureOf(value: unknown): Figure | null {
  if (!isRecord(value) || typeof value.count !== 'number') return null
  const bands = Array.isArray(value.bands)
    ? value.bands.filter(isRecord).map((band) => ({
        label: String(band.label ?? ''),
        count: typeof band.count === 'number' ? band.count : 0,
      }))
    : null
  const left = isRecord(value.left_out) ? value.left_out : {}
  return {
    count: value.count,
    not_recorded: typeof value.not_recorded === 'number' ? value.not_recorded : 0,
    median: typeof value.median === 'string' ? value.median : null,
    p90: typeof value.p90 === 'string' ? value.p90 : null,
    bands,
    left_out: {
      restricted: typeof left.restricted === 'number' ? left.restricted : 0,
      sample: typeof left.sample === 'number' ? left.sample : 0,
      probe: typeof left.probe === 'number' ? left.probe : 0,
    },
  }
}

function sourceOf(value: unknown): Source | null {
  if (!isRecord(value) || typeof value.source !== 'string') return null
  if (value.kind !== 'measured' && value.kind !== 'estimated') return null
  const verdict = figureOf(value.time_to_first_verdict)
  const close = figureOf(value.time_to_close)
  if (!verdict || !close) return null
  return {
    source: value.source,
    kind: value.kind,
    fetch_time: typeof value.fetch_time === 'string' ? value.fetch_time : null,
    time_to_first_verdict: verdict,
    time_to_close: close,
  }
}

function beforeOf(value: unknown): Before | null {
  if (!isRecord(value)) return null
  const sources = Array.isArray(value.sources)
    ? value.sources.filter(isRecord).flatMap((row) => {
        if (typeof row.source !== 'string') return []
        const times = Array.isArray(row.time_to_close) ? row.time_to_close.filter(isRecord) : []
        return [{ source: row.source, time_to_close: times as StoredTime[] }]
      })
    : []
  return {
    recorded_at: typeof value.recorded_at === 'string' ? value.recorded_at : null,
    not_checked: typeof value.not_checked === 'string' ? value.not_checked : null,
    sources,
  }
}

function readBody(value: unknown): Body | null {
  if (!isRecord(value)) return null
  const window = isRecord(value.window) ? value.window : undefined
  return {
    window: window
      ? {
          start: typeof window.start === 'string' ? window.start : null,
          end: typeof window.end === 'string' ? window.end : null,
          length: typeof window.length === 'string' ? window.length : null,
        }
      : undefined,
    close_note: typeof value.close_note === 'string' ? value.close_note : undefined,
    sources: Array.isArray(value.sources) ? value.sources.flatMap((row) => {
      const source = sourceOf(row)
      return source ? [source] : []
    }) : [],
    before: beforeOf(value.before) ?? undefined,
  }
}

function numbers(figure: Figure): string {
  const timed = `${figure.count} timed`
  const missing = figure.not_recorded > 0 ? `, ${figure.not_recorded} not recorded` : ''
  if (figure.median) {
    return `Median ${figure.median}, p90 ${figure.p90 ?? '—'}. ${timed}${missing}`
  }
  if (figure.bands && figure.count > 0) {
    const bands = figure.bands.map((band) => `${band.count} ${band.label}`).join(', ')
    return `${bands}. ${timed}${missing}`
  }
  if (figure.not_recorded > 0) return `${figure.not_recorded} not recorded`
  return 'None in this window'
}

function leftOut(left: LeftOut): string | null {
  const parts: string[] = []
  if (left.restricted) parts.push(`${left.restricted} on a restricted case`)
  if (left.sample) parts.push(`${left.sample} vigil-sample`)
  if (left.probe) parts.push(`${left.probe} probe`)
  if (parts.length === 0) return null
  return `Left out: ${parts.join(', ')}.`
}

function windowLine(body: Body): string {
  const span = body.window
  const length = span?.length ? `Window ${span.length}` : 'Window'
  if (span?.start && span.end) {
    return `${length}, ${reportStamp(span.start)} to ${reportStamp(span.end)}.`
  }
  return `${length}.`
}

function storedLine(row: StoredTime): string {
  const bands = (row.bands ?? [])
    .filter((band) => band.count > 0)
    .map((band) => `${band.count} ${band.label}`)
    .join(', ')
  const headline = row.median
    ? `Median ${row.median}, p90 ${row.p90 ?? '—'}`
    : bands || 'No timed closures'
  const kind = row.kind === 'estimated' ? 'Estimated' : row.kind === 'measured' ? 'Measured' : ''
  const missing = row.opened_missing ? `, ${row.opened_missing} left out with no open time` : ''
  return `Time to close: ${headline}. ${row.count ?? 0} timed${missing}.${kind ? ` ${kind}.` : ''} ${row.definition ?? ''}`
}

function BeforeBlock({ before }: { before: Before }) {
  if (before.not_checked) return <p>{before.not_checked}</p>
  return (
    <>
      {before.recorded_at && <p>Day 1 Report {reportStamp(before.recorded_at)}</p>}
      {before.sources.map((source) => (
        <div key={source.source}>
          <p className="d1-facts">{source.source}</p>
          {source.time_to_close.map((row, index) => (
            <p key={`${source.source}-${row.definition ?? index}`}>{storedLine(row)}</p>
          ))}
        </div>
      ))}
      {before.sources.length === 0 && <p className="d1-quiet">No time to close was stored.</p>}
    </>
  )
}

export function TimeSection({ evidence }: { evidence: unknown }) {
  const body = readBody(evidence)
  return (
    <section className="d1-card" aria-label="Time to first verdict and time to close">
      <div className="d1-card-head">
        <h2>Time to first verdict and time to close</h2>
        <InfoTip label="How time to first verdict and time to close are judged" align="start" {...TIP} />
      </div>
      {!body && <p className="d1-quiet">Nothing to report in this section.</p>}
      {body && (
        <>
          <p>{windowLine(body)}</p>
          {(body.sources ?? []).length === 0 && <p className="d1-quiet">No alerts in this window.</p>}
          {(body.sources ?? []).map((source) => {
            const verdictLeft = leftOut(source.time_to_first_verdict.left_out)
            const closeLeft = leftOut(source.time_to_close.left_out)
            return (
              <div key={source.source}>
                <p className="d1-sub">
                  {source.source}
                  {source.kind === 'estimated' ? ' · Estimated' : ''}
                </p>
                {source.fetch_time && <p>{source.fetch_time}</p>}
                <div className="d1-rows">
                  <div className="d1-row">
                    <p>
                      Time to first verdict: {numbers(source.time_to_first_verdict)}. {kindWord(source.kind)}.
                    </p>
                    {verdictLeft && <p className="d1-facts">{verdictLeft}</p>}
                  </div>
                  <div className="d1-row">
                    <p>
                      Time to close: {numbers(source.time_to_close)}. {kindWord(source.kind)}.
                    </p>
                    {closeLeft && <p className="d1-facts">{closeLeft}</p>}
                  </div>
                </div>
              </div>
            )
          })}
          {body.close_note && <p>{body.close_note}</p>}
          {body.before && (
            <div>
              <p className="d1-sub">Before</p>
              <BeforeBlock before={body.before} />
            </div>
          )}
        </>
      )}
    </section>
  )
}
