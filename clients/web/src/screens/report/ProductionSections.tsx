import { Fragment } from 'react'
import { Link } from 'react-router-dom'
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
    case 'no_value':
      return 'No cited value'
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
    case 'no_value':
      return 'idle'
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

type TuningGroup = 'rule' | 'title'

type TuningCauses = {
  expected: number
  too_broad: number
  data: number
  other: number
  not_given: number
  false_person: number
  false_agent: number
  case_close: number
}

type TuningRow = {
  group: TuningGroup
  source: string
  title: string | null
  name: string | null
  native_key: string | null
  address: string | null
  alerts: number
  real: number
  judged: number
  analyst: number
  agent: number
  window_start: string
  window_end: string
  benign_k?: number
  benign_n?: number
  benign?: number
  true_positive_share?: number | null
  low?: number | null
  high?: number | null
  causes?: TuningCauses
  on_history_tuning?: boolean
  history_recorded_at?: string
}

type TuningLeftOut = {
  sample: number
  probe: number
  restricted: number
}

type Tuning = {
  not_checked?: string
  line?: string
  left_out?: TuningLeftOut
  groups?: TuningRow[]
  candidates?: TuningRow[]
}

const TUNING_TIP = {
  source: 'Findings stored since Production Start, and the latest Day 1 tuning list',
  calculation:
    'A resolved Kibana, Wazuh or Splunk rule is one group, across versions. Every other alert is its source and title. A person\'s judgement wins, else Vigil\'s. At 20 judgements, under 10% true positive is a candidate. Under 20 anywhere, the line is too few to judge yet.',
  limit:
    'Hidden without findings.read. Sample alerts, probes and restricted cases are left out and counted. The report does not change a rule or add an exclusion. Not checked is never a pass.',
}

const CAUSE_LABEL: Record<keyof TuningCauses, string> = {
  expected: 'expected',
  too_broad: 'too broad',
  data: 'data',
  other: 'other',
  not_given: 'not given',
  false_person: 'marked by a person',
  false_agent: 'marked by Vigil',
  case_close: 'false-positive close',
}

function groupWord(group: TuningGroup): string {
  switch (group) {
    case 'rule':
      return 'Rule'
    case 'title':
      return 'Title'
    default: {
      const never: never = group
      return never
    }
  }
}

function tuningLabel(row: TuningRow): string {
  if (row.group === 'rule') return row.name || row.native_key || 'Unnamed rule'
  return row.title ? row.title : 'blank title'
}

function excludeHref(address: string, reportId: string, runId: string): string {
  const query = new URLSearchParams({ exclude: address, report: reportId, run: runId })
  return `/dashboard?${query.toString()}`
}

function spanOf(row: TuningRow): string {
  return `${reportStamp(row.window_start)} to ${reportStamp(row.window_end)}`
}

function historyLine(row: TuningRow): string | null {
  if (!row.history_recorded_at) return null
  const named = `run ${reportStamp(row.history_recorded_at)}`
  return row.on_history_tuning
    ? `On the Day 1 tuning list, ${named}.`
    : `Not on the Day 1 tuning list, ${named}.`
}

function causeLine(causes: TuningCauses): string {
  return (Object.keys(CAUSE_LABEL) as (keyof TuningCauses)[])
    .map((key) => `${CAUSE_LABEL[key]} ${causes[key] ?? 0}`)
    .join(', ')
}

function TuningLines({
  row,
  reportId,
  runId,
  candidate,
}: {
  row: TuningRow
  reportId: string
  runId: string
  candidate: boolean
}) {
  const range =
    row.low != null && row.high != null ? ` (${pct(row.low)}–${pct(row.high)})` : ''
  const share =
    row.true_positive_share != null ? `, ${pct(row.true_positive_share)}` : ''
  const history = historyLine(row)
  return (
    <div className="d1-row">
      <p>
        {groupWord(row.group)} {tuningLabel(row)}
      </p>
      {candidate ? (
        <p>
          {row.real} of {row.judged} true positives{share}
          {range}. Benign {row.benign ?? row.judged - row.real}. Person {row.analyst}, Vigil {row.agent}.
        </p>
      ) : (
        <p>
          {row.alerts} {row.alerts === 1 ? 'alert' : 'alerts'}. Benign {row.benign_k ?? 0} of {row.benign_n ?? 0}.
        </p>
      )}
      {candidate && row.causes && <p className="d1-facts">{causeLine(row.causes)}.</p>}
      <p className="d1-facts">
        {row.source} · window {spanOf(row)}
      </p>
      {history && <p className="d1-facts">{history}</p>}
      {row.address && (
        <Link to={excludeHref(row.address, reportId, runId)}>Exclude this address</Link>
      )}
    </div>
  )
}

export function tuningEvidence(value: unknown): Tuning | null {
  return isRecord(value) ? (value as Tuning) : null
}

export function TuningSection({
  evidence,
  reportId,
  runId,
  tip,
}: {
  evidence: Tuning | null
  reportId: string
  runId: string
  tip: typeof TUNING_TIP
}) {
  const left = evidence?.left_out
  const candidates = (evidence?.candidates ?? []).filter((row) => row.group === 'rule' || row.group === 'title')
  const groups = (evidence?.groups ?? []).filter((row) => row.group === 'rule' || row.group === 'title')
  return (
    <section className="d1-card" aria-label="Tuning since Production Start">
      <div className="d1-card-head">
        <h2>Tuning since Production Start</h2>
        <InfoTip label="How tuning since Production Start is judged" align="start" {...tip} />
      </div>
      {!evidence ? (
        <p className="d1-quiet">Nothing to report in this section.</p>
      ) : evidence.not_checked ? (
        <p className="d1-quiet">{evidence.not_checked}</p>
      ) : (
        <>
          <div className="d1-rows">
            {left && (
              <div className="d1-row">
                <p>
                  Left out of every group: {left.sample} sample, {left.probe} probe, {left.restricted} on
                  restricted cases.
                </p>
              </div>
            )}
            {evidence.line && (
              <div className="d1-row">
                <p>{evidence.line}</p>
              </div>
            )}
            {candidates.map((row, index) => (
              <TuningLines
                key={`candidate-${index}`}
                row={row}
                reportId={reportId}
                runId={runId}
                candidate
              />
            ))}
            {groups.map((row, index) => (
              <TuningLines key={`group-${index}`} row={row} reportId={reportId} runId={runId} candidate={false} />
            ))}
          </div>
          {!evidence.line && candidates.length === 0 && groups.length === 0 && (
            <p className="d1-quiet">Nothing to report in this section.</p>
          )}
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

export { ALERTS_TIP, QUIET_TIP, TUNING_TIP }
