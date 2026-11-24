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

const CHANGES_TIP = {
  source: 'The audit trail, exclusion rows, and integration health, since the Day 1 Report this run is read against',
  calculation:
    'Act flags, per-workflow tiers, and scheduled collection come from config changes. Exclusions are rows added or removed in that window. An alert source that was checked then and is off now, on either collection path, is switched off since that report.',
  limit:
    'Audit changes need audit.read. Exclusions need findings.read. Switched-off sources need integrations.read. A missing permission names what is withheld. Not checked is never a pass.',
}

type ChangeFlag = { name: string; old: boolean; new: boolean }
type TierWord = 'act' | 'assist'
type ChangeTier = { workflow: string; old: TierWord | null; new: TierWord | null }
type ChangePair = { old: unknown; new: unknown }

type ChangeItem =
  | { kind: 'autonomy'; at: string | null; screen: string; flags: ChangeFlag[]; tiers: ChangeTier[] }
  | { kind: 'collection'; at: string | null; screen: string; old: unknown; new: unknown }
  | {
      kind: 'source'
      at: string | null
      screen: string
      source_id: string
      enabled: ChangePair
      interval_seconds: ChangePair
      cap: ChangePair
      min_severity: ChangePair
      current_min_severity: string | null
      current_floor_unread: boolean
    }
  | { kind: 'exclusion'; at: string | null; screen: string; action: 'added' | 'removed'; ip: string; hidden_findings: number | null }
  | { kind: 'switched_off'; at: string | null; screen: string; source_id: string }

type Changes = {
  not_checked?: string
  anchor_at: string | null
  before_not_shown: boolean
  audit_not_checked?: string
  exclusions_not_checked?: string
  health_not_checked?: string
  health_anchor_unread: boolean
  items: ChangeItem[]
  withheld: string[]
}

function textOf(value: unknown): string | null {
  return typeof value === 'string' && value ? value : null
}

function pairOf(value: unknown): ChangePair {
  if (!isRecord(value)) return { old: null, new: null }
  return { old: value.old ?? null, new: value.new ?? null }
}

function readItem(raw: Record<string, unknown>): ChangeItem | null {
  const screen = textOf(raw.screen) ?? ''
  const at = textOf(raw.at)
  switch (raw.kind) {
    case 'autonomy':
      return {
        kind: 'autonomy',
        at,
        screen,
        flags: Array.isArray(raw.flags)
          ? raw.flags.flatMap((flag) => {
              if (!isRecord(flag) || typeof flag.name !== 'string') return []
              if (typeof flag.old !== 'boolean' || typeof flag.new !== 'boolean') return []
              return [{ name: flag.name, old: flag.old, new: flag.new }]
            })
          : [],
        tiers: Array.isArray(raw.tiers)
          ? raw.tiers.flatMap((tier) => {
              if (!isRecord(tier) || typeof tier.workflow !== 'string') return []
              const old = tier.old === 'act' || tier.old === 'assist' ? tier.old : null
              const next = tier.new === 'act' || tier.new === 'assist' ? tier.new : null
              return [{ workflow: tier.workflow, old, new: next }]
            })
          : [],
      }
    case 'collection':
      return { kind: 'collection', at, screen, old: raw.old ?? null, new: raw.new ?? null }
    case 'source':
      if (typeof raw.source_id !== 'string' || !raw.source_id) return null
      return {
        kind: 'source',
        at,
        screen,
        source_id: raw.source_id,
        enabled: pairOf(raw.enabled),
        interval_seconds: pairOf(raw.interval_seconds),
        cap: pairOf(raw.cap),
        min_severity: pairOf(raw.min_severity),
        current_min_severity: textOf(raw.current_min_severity),
        current_floor_unread: raw.current_floor_unread === true,
      }
    case 'exclusion':
      if (typeof raw.ip !== 'string' || !raw.ip) return null
      if (raw.action !== 'added' && raw.action !== 'removed') return null
      return {
        kind: 'exclusion',
        at,
        screen,
        action: raw.action,
        ip: raw.ip,
        hidden_findings: typeof raw.hidden_findings === 'number' ? raw.hidden_findings : null,
      }
    case 'switched_off':
      if (typeof raw.source_id !== 'string' || !raw.source_id) return null
      return { kind: 'switched_off', at, screen, source_id: raw.source_id }
    default:
      return null
  }
}

export function changesEvidence(value: unknown): Changes | null {
  if (!isRecord(value)) return null
  const items = Array.isArray(value.items)
    ? value.items.flatMap((item) => {
        if (!isRecord(item)) return []
        const read = readItem(item)
        return read ? [read] : []
      })
    : []
  return {
    not_checked: textOf(value.not_checked) ?? undefined,
    anchor_at: textOf(value.anchor_at),
    before_not_shown: value.before_not_shown === true,
    audit_not_checked: textOf(value.audit_not_checked) ?? undefined,
    exclusions_not_checked: textOf(value.exclusions_not_checked) ?? undefined,
    health_not_checked: textOf(value.health_not_checked) ?? undefined,
    health_anchor_unread: value.health_anchor_unread === true,
    items,
    withheld: Array.isArray(value.withheld)
      ? value.withheld.filter((line): line is string => typeof line === 'string')
      : [],
  }
}

function settingName(path: string): string {
  if (path.includes('section=autoinvestigate')) return 'Limits & autonomy'
  if (path.includes('section=federation')) return 'Alert collection'
  if (path.includes('section=integrations')) return 'Integrations'
  if (path.startsWith('/dashboard')) return 'Findings'
  return 'Settings'
}

function onOff(value: unknown): string {
  if (value === true) return 'on'
  if (value === false) return 'off'
  if (typeof value === 'number') return String(value)
  if (typeof value === 'string' && value) return value
  return 'none'
}

function flagName(name: string): string {
  switch (name) {
    case 'reversible':
      return 'Reversible changes'
    case 'irreversible':
      return 'Irreversible changes'
    default:
      return name
  }
}

function seconds(value: unknown): string {
  if (typeof value === 'number') return `${value} s`
  if (typeof value === 'string' && value) return `${value} s`
  return 'none'
}

function tierName(value: TierWord | null): string {
  switch (value) {
    case 'act':
      return 'Act'
    case 'assist':
      return 'Assist'
    case null:
      return 'the fleet'
    default: {
      const never: never = value
      return never
    }
  }
}

function changeText(item: ChangeItem, anchor: string | null): string {
  switch (item.kind) {
    case 'autonomy':
      return [
        ...item.flags.map(
          (flag) => `${flagName(flag.name)}: ${flag.old ? 'on' : 'off'}, then ${flag.new ? 'on' : 'off'}.`,
        ),
        ...item.tiers.map(
          (tier) => `${tier.workflow}: ${tierName(tier.old)}, then ${tierName(tier.new)}.`,
        ),
      ].join(' ')
    case 'collection':
      if (item.new === true && item.old !== true) return 'Scheduled collection turned on.'
      if (item.new === false && item.old !== false) return 'Scheduled collection turned off.'
      return `Scheduled collection: ${onOff(item.old)}, then ${onOff(item.new)}.`
    case 'source':
      return (
        `${item.source_id}: enabled ${onOff(item.enabled.old)} then ${onOff(item.enabled.new)}, ` +
        `interval ${seconds(item.interval_seconds.old)} then ${seconds(item.interval_seconds.new)}, ` +
        `cap ${onOff(item.cap.old)} then ${onOff(item.cap.new)}, ` +
        `severity floor ${onOff(item.min_severity.old)} then ${onOff(item.min_severity.new)}.`
      )
    case 'exclusion': {
      const verb = item.action === 'added' ? 'Added' : 'Removed'
      const hides =
        item.action === 'added' && item.hidden_findings != null
          ? ` Hides ${item.hidden_findings} ${item.hidden_findings === 1 ? 'alert' : 'alerts'}.`
          : ''
      return `${verb} exclusion ${item.ip}.${hides}`
    }
    case 'switched_off': {
      const when = anchor ? ` of ${reportStamp(anchor)}` : ''
      return `${item.source_id}: switched off since the Day 1 Report${when}.`
    }
    default: {
      const never: never = item
      return never
    }
  }
}

function floorLine(item: ChangeItem): string | null {
  if (item.kind !== 'source') return null
  const floor = item.current_floor_unread ? 'not checked' : (item.current_min_severity ?? 'none')
  return `Current severity floor: ${floor}.`
}

function ChangeRow({ item, anchor }: { item: ChangeItem; anchor: string | null }) {
  const floor = floorLine(item)
  return (
    <div className="d1-row has-link">
      <p>{changeText(item, anchor)}</p>
      {floor && <p className="d1-facts">{floor}</p>}
      {item.at && <p className="d1-facts">{reportStamp(item.at)}</p>}
      {item.screen && <Link to={item.screen}>{settingName(item.screen)}</Link>}
    </div>
  )
}

export function ChangesSection({ evidence, tip }: { evidence: Changes | null; tip: typeof CHANGES_TIP }) {
  const anchor = evidence?.anchor_at ?? null
  const items = evidence?.items ?? []
  const notes = [
    evidence?.audit_not_checked,
    evidence?.exclusions_not_checked,
    evidence?.health_not_checked,
  ].filter((line): line is string => Boolean(line))
  const withheld = evidence?.withheld ?? []
  const onlySentence = Boolean(evidence?.not_checked && !anchor)
  const nothing =
    !onlySentence &&
    items.length === 0 &&
    notes.length === 0 &&
    !evidence?.health_anchor_unread &&
    withheld.length === 0
  return (
    <section className="d1-card" aria-label="What changed">
      <div className="d1-card-head">
        <h2>What changed</h2>
        <InfoTip label="How what changed is read" align="start" {...tip} />
      </div>
      {onlySentence && <p className="d1-quiet">{evidence?.not_checked}</p>}
      {evidence?.before_not_shown && anchor && (
        <p className="d1-facts">
          Changes before the Day 1 Report of {reportStamp(anchor)} are not shown.
        </p>
      )}
      {evidence?.health_anchor_unread && anchor && (
        <p className="d1-quiet">
          not checked: the Day 1 Report of {reportStamp(anchor)} couldn&apos;t read integration health
        </p>
      )}
      {notes.map((line) => (
        <p className="d1-quiet" key={line}>
          {line}
        </p>
      ))}
      {items.length > 0 && (
        <div className="d1-rows">
          {items.map((item, index) => (
            <ChangeRow key={`${item.kind}-${item.at ?? index}-${index}`} item={item} anchor={anchor} />
          ))}
        </div>
      )}
      {nothing && anchor && (
        <p className="d1-quiet">Nothing changed since the Day 1 Report of {reportStamp(anchor)}.</p>
      )}
      {nothing && !anchor && !onlySentence && <p className="d1-quiet">Nothing to report in this section.</p>}
      {withheld.map((line) => (
        <p className="d1-withheld" key={line}>
          {line}
        </p>
      ))}
    </section>
  )
}

export { CHANGES_TIP }
