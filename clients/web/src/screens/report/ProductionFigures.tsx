import type { Schema } from '../../services/apiTypes'
import { InfoTip } from '../../shared/InfoTip'
import { reportStamp } from './stamp'

type ProductionReport = Schema<'ProductionReport'>
type Kind = 'measured' | 'estimated'
type Figure = {
  label: string
  k: number
  n: number
  kind: Kind
  definition: string
  window_start: string
  window_end: string
  low: number | null
  high: number | null
}
type LevelRow = { level: string; count: number; abstained: number }
type SourceRow = { source: string; dispositions: Figure[] }

const CONFIDENCE_TIP = {
  source: 'Investigation terminals stored in the window',
  calculation:
    'Counted by the confidence word on the terminal. Null, empty, and any other word are unstated. Abstained is inconclusive or needs review.',
  limit:
    "The model's own estimate, not checked against outcomes. Triage confidence is left out.",
}

const AGREEMENT_TIP = {
  source: 'Closures in the window scored against the investigation, and the latest Day 1 Report',
  calculation:
    'Under 20, a share is k of n. At 20 or above it is that count with a range. A close by Vigil alone is its own count.',
  limit:
    "A case that names its readers is left out and counted. A person's view of a triage verdict is not recorded.",
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

function pct(value: number): string {
  const shown = value * 100
  const rounded = Math.round(shown)
  return Math.abs(shown - rounded) < 0.05 ? `${rounded}%` : `${shown.toFixed(1)}%`
}

function share(figure: Figure): string {
  if (figure.low == null || figure.high == null) return `${figure.k} of ${figure.n}`
  const point = figure.n > 0 ? pct(figure.k / figure.n) : '0%'
  return `${figure.k} of ${figure.n}, ${point} (${pct(figure.low)}–${pct(figure.high)})`
}

function windowOf(start: string, end: string): string {
  if (!start || !end) return 'window not recorded'
  return `${reportStamp(start)} to ${reportStamp(end)}`
}

function windowLength(start: string, end: string): string {
  const ms = Date.parse(end) - Date.parse(start)
  if (!Number.isFinite(ms) || ms < 0) return 'window'
  const totalMinutes = Math.round(ms / 60000)
  const hours = Math.floor(totalMinutes / 60)
  const minutes = totalMinutes % 60
  if (hours > 0 && minutes === 0) return hours === 1 ? '1 hour' : `${hours} hours`
  if (hours > 0) return `${hours} h ${minutes} min`
  return minutes === 1 ? '1 min' : `${minutes} min`
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function num(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function figureOf(value: unknown): Figure | null {
  if (!isRecord(value)) return null
  const k = num(value.k)
  const n = num(value.n)
  const kind = value.kind === 'measured' || value.kind === 'estimated' ? value.kind : null
  if (typeof value.label !== 'string' || k === null || n === null || kind === null) return null
  const low = num(value.low)
  const high = num(value.high)
  return {
    label: value.label,
    k,
    n,
    kind,
    definition: typeof value.definition === 'string' ? value.definition : '',
    window_start: typeof value.window_start === 'string' ? value.window_start : '',
    window_end: typeof value.window_end === 'string' ? value.window_end : '',
    low,
    high,
  }
}

function figuresOf(value: unknown): Figure[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((row) => {
    const figure = figureOf(row)
    return figure ? [figure] : []
  })
}

function FigureLine({ figure }: { figure: Figure }) {
  const length = figure.window_start && figure.window_end
    ? `, ${windowLength(figure.window_start, figure.window_end)}`
    : ''
  return (
    <p>
      {figure.label}: {share(figure)}. {kindWord(figure.kind)}.
      {figure.definition ? ` ${figure.definition}.` : ''} Window {windowOf(figure.window_start, figure.window_end)}
      {length}.
    </p>
  )
}

function NotChecked({ text }: { text: string }) {
  return <p>{text}</p>
}

function evidenceOf(report: ProductionReport, id: string): Record<string, unknown> | null {
  const body = report.evidence?.[id]
  return isRecord(body) ? body : null
}

function Confidence({
  body,
  beside,
}: {
  body: Record<string, unknown>
  beside: Record<string, Figure[]> | null
}) {
  if (typeof body.not_checked === 'string') return <NotChecked text={body.not_checked} />
  const levels = Array.isArray(body.levels) ? body.levels.filter(isRecord) : []
  const rows: LevelRow[] = levels.flatMap((row) => {
    const count = num(row.count)
    const abstained = num(row.abstained)
    if (typeof row.level !== 'string' || count === null || abstained === null) return []
    return [{ level: row.level, count, abstained }]
  })
  const start = typeof body.window_start === 'string' ? body.window_start : ''
  const end = typeof body.window_end === 'string' ? body.window_end : ''
  return (
    <>
      {typeof body.label === 'string' && <p>{body.label}</p>}
      {typeof body.triage === 'string' && <p className="d1-facts">{body.triage}</p>}
      {start && end && (
        <p className="d1-facts">
          Window {windowOf(start, end)}, {windowLength(start, end)}.
        </p>
      )}
      <div className="d1-rows">
        {rows.map((row) => (
          <div className="d1-row" key={row.level}>
            <p>
              {row.level}: {row.count}. Abstained {row.abstained}.
            </p>
            {(beside?.[row.level] ?? []).map((figure) => (
              <FigureLine key={figure.label} figure={figure} />
            ))}
          </div>
        ))}
      </div>
      {typeof body.abstained === 'number' && (
        <p className="d1-facts">Abstained {body.abstained}, from inconclusive and needs review.</p>
      )}
    </>
  )
}

function Analysts({ body }: { body: Record<string, unknown> }) {
  const figures = figuresOf(body.figures)
  const leftOut = num(body.restricted_left_out)
  return (
    <>
      {typeof body.note === 'string' && <p>{body.note}</p>}
      {typeof body.window_start === 'string' && typeof body.window_end === 'string' && (
        <p className="d1-facts">
          Window {windowOf(body.window_start, body.window_end)}, {windowLength(body.window_start, body.window_end)}.
        </p>
      )}
      <div className="d1-rows">
        {figures.map((figure) => (
          <div className="d1-row" key={figure.label}>
            <FigureLine figure={figure} />
          </div>
        ))}
      </div>
      {leftOut !== null && (
        <p className="d1-facts">Left out: {leftOut}. Each names its readers.</p>
      )}
    </>
  )
}

function sourceRows(value: unknown): SourceRow[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((row) => {
    if (!isRecord(row) || typeof row.source !== 'string') return []
    return [{ source: row.source, dispositions: figuresOf(row.dispositions) }]
  })
}

function SourceList({ title, rows }: { title: string; rows: SourceRow[] }) {
  return (
    <>
      <p className="d1-sub">{title}</p>
      {rows.length === 0 ? (
        <p className="d1-quiet">None.</p>
      ) : (
        <div className="d1-rows">
          {rows.map((row) => (
            <div className="d1-row" key={`${title}-${row.source}`}>
              <p>{row.source}</p>
              {row.dispositions.length === 0 ? (
                <p className="d1-facts">No disposition figures.</p>
              ) : (
                row.dispositions.map((figure) => (
                  <FigureLine key={figure.label} figure={figure} />
                ))
              )}
            </div>
          ))}
        </div>
      )}
    </>
  )
}

function Previous({ body }: { body: unknown }) {
  if (!isRecord(body)) return null
  if (typeof body.not_checked === 'string') return <NotChecked text={body.not_checked} />
  const skipped = isRecord(body.skipped) ? body.skipped : null
  const sample = skipped ? num(skipped['vigil-sample']) : null
  const probe = skipped ? num(skipped.probe) : null
  const leftOut = num(body.restricted_left_out)
  return (
    <>
      <p className="d1-sub">With the previous process</p>
      {typeof body.label === 'string' && <p>{body.label}</p>}
      {typeof body.run_id === 'string' && (
        <p className="d1-facts">
          Day 1 run {body.run_id}
          {typeof body.recorded_at === 'string' ? `, recorded ${reportStamp(body.recorded_at)}` : ''}.
        </p>
      )}
      <SourceList title="Before" rows={sourceRows(body.before)} />
      <SourceList title="Since Production Start" rows={sourceRows(body.since)} />
      {sample !== null && probe !== null && (
        <p className="d1-facts">
          vigil-sample findings left out: {sample}. probe findings left out: {probe}.
        </p>
      )}
      {leftOut !== null && (
        <p className="d1-facts">Cases that name their readers, left out: {leftOut}.</p>
      )}
    </>
  )
}

function byConfidence(body: Record<string, unknown> | null): Record<string, Figure[]> | null {
  if (!body || typeof body.not_checked === 'string' || !isRecord(body.by_confidence)) return null
  const out: Record<string, Figure[]> = {}
  for (const [level, rows] of Object.entries(body.by_confidence)) {
    const figures = figuresOf(rows)
    if (figures.length > 0) out[level] = figures
  }
  return out
}

export function ProductionFigures({ report }: { report: ProductionReport }) {
  const modules = report.modules ?? []
  const withheld = new Set(report.withheld ?? [])
  const confidenceOn = modules.includes('production.confidence')
  const agreementOn = modules.includes('production.agreement')
  if (!confidenceOn && !agreementOn) return null
  const confidence = evidenceOf(report, 'production.confidence')
  const agreement = evidenceOf(report, 'production.agreement')
  const censusFailed = confidence !== null && typeof confidence.not_checked === 'string'
  const beside = !censusFailed && agreementOn && !withheld.has('production.agreement')
    ? byConfidence(agreement)
    : null
  return (
    <>
      {confidenceOn && withheld.has('production.confidence') && (
        <section className="d1-card" aria-label="How sure the investigations were">
          <h2>How sure the investigations were</h2>
          <p className="d1-withheld">Not included. This section needs platform.read.</p>
        </section>
      )}
      {confidenceOn && !withheld.has('production.confidence') && confidence && (
        <section className="d1-card" aria-label="How sure the investigations were">
          <div className="d1-card-head">
            <h2>How sure the investigations were</h2>
            <InfoTip label="How sure the investigations were" align="start" {...CONFIDENCE_TIP} />
          </div>
          <Confidence body={confidence} beside={beside} />
        </section>
      )}
      {agreementOn && withheld.has('production.agreement') && (
        <section className="d1-card" aria-label="Agreement">
          <h2>Agreement</h2>
          <p className="d1-withheld">Not included. This section needs platform.read.</p>
        </section>
      )}
      {agreementOn && !withheld.has('production.agreement') && agreement && (
        <section className="d1-card" aria-label="Agreement">
          <div className="d1-card-head">
            <h2>Agreement</h2>
            <InfoTip label="How agreement is counted" align="start" {...AGREEMENT_TIP} />
          </div>
          {typeof agreement.not_checked === 'string' ? (
            <NotChecked text={agreement.not_checked} />
          ) : (
            <>
              <p className="d1-sub">With analysts</p>
              <Analysts body={agreement} />
              <Previous body={agreement.previous} />
            </>
          )}
        </section>
      )}
    </>
  )
}
