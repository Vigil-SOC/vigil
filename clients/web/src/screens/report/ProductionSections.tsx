import { Fragment } from 'react'
import type { Schema } from '../../services/apiTypes'
import { InfoTip } from '../../shared/InfoTip'
import { reportStamp } from './stamp'

type Check = Schema<'Day1Check'>
type Outcome = Check['outcome']
type Kind = 'measured' | 'estimated'

type Figure = {
  state: string
  label: string
  k: number
  n: number
  kind: Kind
  definition: string
  window_start: string
  window_end: string
  window_length: string
  low: number | null
  high: number | null
  bound: string | null
  failed_triage: number | null
}

type LeftOut = {
  key: string
  label: string
  count: number
  kind: Kind
  definition: string
  window_start: string
  window_end: string
  window_length: string
}

type AlertSource = {
  source: string
  groups: Figure[]
  left_out: LeftOut[]
  floor: string
  min_severity: string | null
  dropped: number | null
}

type Alerts = {
  not_checked?: string
  duplicates?: string
  close_held?: string | null
  sources?: AlertSource[]
}

type SilentRow = {
  tool: string
  name: string
  result: string
  reason: string | null
}

type WeekRow = { name?: string; rule_id?: number }

type FirstWeek = {
  silent?: number
  no_data?: number
  never_firing?: number
  week_over?: boolean
  unknown?: string[]
  source?: string
  rows?: Record<string, WeekRow[]>
}

type QuietEvidence = {
  silent?: SilentRow[]
  first_week?: FirstWeek | null
}

const QUIET_TIP = {
  source: 'Switched-on alert sources, the newest stored alert or last-data time, and stored Silent and first-week results',
  calculation:
    'A source passes when an alert arrived in the last 24 hours of the window. None in that day, or none ever, needs a person. An unhealthy source needs a person, with the health record reason and latest time.',
  limit:
    'Hidden without findings.read. A duplicate or an alert under the floor is not stored. Not checked is never a pass.',
}

const ALERTS_TIP = {
  source: 'Findings stored since Production Start, each source health record, and its current severity floor',
  calculation:
    'Each alert is in one group, by the same rule as Overview. Failed triage is counted inside waiting and scoring. Restricted cases, sample alerts, and probes are left out and counted.',
  limit:
    'Hidden without findings.read. Duplicates and alerts under a severity floor are not recorded. Nothing is added across sources. Not checked is never a pass.',
}

function outcomeWord(outcome: Outcome): string {
  switch (outcome) {
    case 'pass':
      return 'Pass'
    case 'fail':
      return 'Fail'
    case 'not_checked':
      return 'Not checked'
    case 'needs_a_person':
      return 'Needs a person'
    default: {
      const never: never = outcome
      return never
    }
  }
}

function outcomeTone(outcome: Outcome): string {
  switch (outcome) {
    case 'pass':
      return 'good'
    case 'fail':
      return 'needs'
    case 'not_checked':
      return 'idle'
    case 'needs_a_person':
      return 'needs'
    default: {
      const never: never = outcome
      return never
    }
  }
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

function rate(figure: Figure): string {
  const ratio = `${figure.k} of ${figure.n}`
  if (figure.n >= 20 && figure.low != null && figure.high != null) {
    const point = figure.n > 0 ? pct(figure.k / figure.n) : '0%'
    return `${ratio}, ${point} (${pct(figure.low)}–${pct(figure.high)})`
  }
  if (figure.k === 0 && figure.n === 0 && figure.bound) return `0. ${figure.bound}`
  if (figure.k === 0 && figure.high != null) return `${ratio}. Upper bound ${pct(figure.high)}`
  return ratio
}

function windowOf(start: string, end: string, length: string): string {
  return `${reportStamp(start)} to ${reportStamp(end)}, ${length}`
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

export function quietEvidence(value: unknown): QuietEvidence | null {
  return isRecord(value) ? (value as QuietEvidence) : null
}

export function alertsEvidence(value: unknown): Alerts | null {
  return isRecord(value) ? (value as Alerts) : null
}

function CheckLine({ check }: { check: Check }) {
  const tone = outcomeTone(check.outcome)
  return (
    <div className="d1-row">
      <span className={`state-pill ${tone}`}>
        <span className="state-dot" aria-hidden="true" />
        {outcomeWord(check.outcome)}
      </span>
      <p>{check.reason}</p>
      {check.evidence?.fix && <p className="d1-fix">{check.evidence.fix}</p>}
    </div>
  )
}

function SilentLines({ rows }: { rows: SilentRow[] }) {
  if (rows.length === 0) return <p className="d1-quiet">No Silent result is stored.</p>
  return (
    <div className="d1-rows">
      {rows.map((row) => (
        <div className="d1-row" key={`${row.tool}:${row.name}`}>
          <p>
            {row.tool} · {row.name} · {row.result}
            {row.reason ? `. ${row.reason}` : ''}
          </p>
        </div>
      ))}
    </div>
  )
}

const WEEK_KINDS = [
  ['silent', 'Silent'],
  ['no_data', 'No data'],
  ['never_firing', 'Never firing'],
] as const

function FirstWeekLines({ week }: { week: FirstWeek }) {
  return (
    <>
      <p>
        Silent {week.silent ?? 0}. No data {week.no_data ?? 0}. Never firing {week.never_firing ?? 0}.{' '}
        {week.week_over ? 'The first week is over.' : 'The first week is not over.'}
      </p>
      {week.source && <p className="d1-facts">{week.source}</p>}
      {(week.unknown ?? []).map((line) => (
        <p className="d1-facts" key={line}>
          {line}
        </p>
      ))}
      {WEEK_KINDS.map(([key, label]) => {
        const rows = week.rows?.[key] ?? []
        if (rows.length === 0) return null
        return (
          <div className="d1-rows" key={key}>
            {rows.map((row) => (
              <div className="d1-row" key={`${key}:${row.rule_id ?? row.name}`}>
                <p>
                  {label}: {row.name || 'Unnamed rule'}
                </p>
              </div>
            ))}
          </div>
        )
      })}
    </>
  )
}

export function QuietSection({
  checks,
  evidence,
  tip,
}: {
  checks: Check[]
  evidence: QuietEvidence | null
  tip: typeof QUIET_TIP
}) {
  const silent = evidence?.silent ?? []
  const week = evidence?.first_week
  return (
    <section className="d1-card" aria-label="Sources and rules gone quiet">
      <div className="d1-card-head">
        <h2>Sources and rules gone quiet</h2>
        <InfoTip label="How sources and rules gone quiet are judged" align="start" {...tip} />
      </div>
      {checks.length > 0 ? (
        <div className="d1-rows">
          {checks.map((check) => (
            <CheckLine key={check.id} check={check} />
          ))}
        </div>
      ) : (
        <p className="d1-quiet">No alert source is switched on.</p>
      )}
      {evidence && (
        <>
          <p className="d1-sub">Silent</p>
          <SilentLines rows={silent} />
          <p className="d1-sub">First week</p>
          {week ? <FirstWeekLines week={week} /> : <p className="d1-quiet">No first-week result is stored.</p>}
        </>
      )}
    </section>
  )
}

export function AlertsSection({ evidence, tip }: { evidence: Alerts; tip: typeof ALERTS_TIP }) {
  const held =
    evidence.close_held != null
      ? evidence.close_held
      : 'A model may close a case on its own.'
  return (
    <section className="d1-card" aria-label="What happened to every alert">
      <div className="d1-card-head">
        <h2>What happened to every alert</h2>
        <InfoTip label="How every alert is counted" align="start" {...tip} />
      </div>
      {evidence.not_checked ? (
        <p className="d1-quiet">{evidence.not_checked}</p>
      ) : (
        <>
          {evidence.duplicates && (
            <div className="d1-rows">
              <div className="d1-row">
                <p>{evidence.duplicates}</p>
              </div>
            </div>
          )}
          {(evidence.sources ?? []).length === 0 && <p className="d1-quiet">No alert stored in the window.</p>}
          {(evidence.sources ?? []).map((source) => (
            <Fragment key={source.source}>
              <p className="d1-sub">{source.source}</p>
              <div className="d1-rows">
                {source.groups.map((figure) => (
                  <div className="d1-row" key={figure.state}>
                    <p>
                      {figure.label}: {rate(figure)}. {kindWord(figure.kind)}. {figure.definition} Window{' '}
                      {windowOf(figure.window_start, figure.window_end, figure.window_length)}.
                    </p>
                    {figure.failed_triage != null && (
                      <p className="d1-facts">
                        Failed triage: {figure.failed_triage}. Measured. The latest intake row ran out of time in the queue.
                        Window {windowOf(figure.window_start, figure.window_end, figure.window_length)}.
                      </p>
                    )}
                    {figure.state === 'resolved_auto' && <p className="d1-facts">{held}</p>}
                  </div>
                ))}
                {source.left_out.map((row) => (
                  <div className="d1-row" key={row.key}>
                    <p>
                      {row.label}: {row.count}. {kindWord(row.kind)}. {row.definition} Window{' '}
                      {windowOf(row.window_start, row.window_end, row.window_length)}.
                    </p>
                  </div>
                ))}
                <div className="d1-row">
                  <p>
                    Alerts under the severity floor: {source.floor}. Floor: {source.min_severity ?? 'none'}.
                  </p>
                </div>
                <div className="d1-row">
                  <p>Dropped records: {source.dropped == null ? 'not recorded' : source.dropped}.</p>
                </div>
              </div>
            </Fragment>
          ))}
        </>
      )}
    </section>
  )
}

export { ALERTS_TIP, QUIET_TIP }
