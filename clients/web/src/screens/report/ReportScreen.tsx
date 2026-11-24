import { Fragment, useCallback, useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { day1Api, productionReportApi } from '../../services/api'
import type { Schema } from '../../services/apiTypes'
import { useAuth } from '../../contexts/AuthContext'
import { errorText } from '../../shared/errorText'
import { InfoTip } from '../../shared/InfoTip'
import { ToggleRow } from '../../shared/ui'
import { useToast } from '../../shell/toast'
import { stepUpDetailOf } from '../../services/stepUp'
import { EXPERIMENTAL_NOTE } from '../settings/integrationsData'
import { HistorySections } from './HistorySections'
import { ProductionFigures } from './ProductionFigures'
import {
  ALERTS_TIP,
  AlertsSection,
  CHANGES_TIP,
  ChangesSection,
  QUIET_TIP,
  QuietSection,
  alertsEvidence,
  changesEvidence,
  quietEvidence,
} from './ProductionSections'
import { reportStamp } from './stamp'
import './report.css'

type Report = Schema<'Day1Report'>
type ProductionReport = Schema<'ProductionReport'>
type Loaded = Report | ProductionReport
type Check = Schema<'Day1Check'>
type Evidence = Schema<'Day1Evidence'>
type Comparison = Schema<'Day1Comparison'>
type ModuleSetting = Schema<'Day1Module'>
type Forecast = Schema<'Day1Forecast'>
type ForecastSource = Schema<'Day1ForecastSource'>
type ForecastLimits = Schema<'Day1ForecastLimits'>
type ForecastSpend = Schema<'Day1ForecastSpend'>
type Outcome = Check['outcome']
type Trigger = Report['trigger'] | ProductionReport['trigger']

function isProduction(report: Loaded): report is ProductionReport {
  return report.trigger === 'production' || report.trigger === 'production_on_demand'
}

const MODULE_TITLE: Record<string, string> = {
  visibility: 'What Vigil can see',
  settings: "Vigil's own settings",
  coverage: 'Detection coverage',
  estate: 'What CrowdStrike, Defender and Wazuh still list',
  'history.before': 'Your "before"',
  'history.recheck': 'Re-check list',
  'history.tuning': 'Tuning candidates',
  'history.forecast': 'Cost forecast',
  'production.running': 'What is running',
  'production.quiet': 'Sources and rules gone quiet',
  'production.alerts': 'What happened to every alert',
  'production.confidence': 'How sure the investigations were',
  'production.agreement': 'Agreement',
  'production.changes': 'What changed',
}

const MODULE_PERMISSION: Record<string, string> = {
  visibility: 'integrations.read',
  settings: 'platform.read',
  coverage: 'detections.read',
  estate: 'integrations.read',
  'history.before': 'findings.read',
  'history.recheck': 'findings.read',
  'history.tuning': 'findings.read',
  'history.forecast': 'platform.read',
  'production.running': 'platform.read',
  'production.quiet': 'findings.read',
  'production.alerts': 'findings.read',
  'production.confidence': 'platform.read',
  'production.agreement': 'platform.read',
  'production.changes': 'platform.read',
}

// A report section has figures and no outcomes, so it stays out of the summary grid.
const REPORT_SECTIONS = new Set([
  'history.before',
  'history.recheck',
  'history.tuning',
  'history.forecast',
  'production.confidence',
  'production.agreement',
  'production.changes',
])

const SETTINGS_TIP = {
  source: 'The session policy and the audit-trail retention period',
  calculation:
    'A value passes when it is at or stricter than the value for your selection. No cited value is not a pass or a fail. An unset period fails when a value is in force. The MFA count is shown beside the setting and does not change the outcome.',
  limit: 'Session checks need sessions.manage. Retention needs audit.read. Not checked is never a pass.',
}

type ScoredOutcome = Exclude<Outcome, 'no_value'>
const OUTCOMES: ScoredOutcome[] = ['pass', 'fail', 'not_checked', 'needs_a_person']

const TIP = {
  source: 'Integration Health and the install readiness read',
  calculation:
    'A switched-on source passes when it is healthy, needs a person when it requires a test, is not checked when its health says so, and fails otherwise. One failure does not fail it. Last data and webhook labels are information.',
  limit: 'Hidden without integrations.read. Not checked is never a pass.',
}

const FORECAST_TIP = {
  source: 'Imported ticket history, the saved investigation limits, and priced investigation runs',
  calculation:
    "A normal week is the median day's openings times 7. The hourly cap covers the hourly limit divided by the full per-investigation cost. From 20 wholly priced investigations the cost is their median; under 20 it is the range.",
  limit:
    'With the background service stopped, arrivals only. Dollars come only from priced calls. An unpriced model is not $0.',
}

const ESTATE_TIP = {
  source: 'CrowdStrike, Defender and the Wazuh manager, with the credential already saved',
  calculation:
    'A host last heard 7 days or more ago needs a person. Degraded protection the tool itself states fails. Zero of either passes. A tool that lists nothing is not checked.',
  limit:
    "Hidden without integrations.read. A read that cannot cover the list is not checked, and no partial count is kept. Not checked is never a pass.",
}

const COVERAGE_TIP = {
  source: 'The coverage map, the Rules listing, and the gap report',
  calculation:
    'Techniques that are not proven, blind spots, techniques covered on paper only, rules that fail to run or are data silent, and Quiet, Noisy or Silent results that are not a pass. A category with none passes.',
  limit:
    'Hidden without detections.read. Quiet and Sees refresh only as far as the hour\'s search budget. A missing result is not checked: search budget. Under 24 hours, or under 7 days for Noisy and Silent, is not checked: too new. Not checked is never a pass.',
}

const COVERAGE_GROUPS: { id: string; title: string }[] = [
  { id: 'coverage.technique', title: 'Techniques' },
  { id: 'coverage.blind_spot', title: 'Blind spots' },
  { id: 'coverage.paper', title: 'Covered on paper' },
  { id: 'coverage.run', title: "Rules that don't run" },
  { id: 'coverage.quiet', title: 'Quiet' },
  { id: 'coverage.noisy', title: 'Noisy' },
  { id: 'coverage.silent', title: 'Silent' },
]

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

function triggerWord(trigger: Trigger, name: string | null): string {
  switch (trigger) {
    case 'readiness':
      return 'Readiness'
    case 'fallback':
      return 'Fallback time'
    case 'on_demand':
      return name ? `On demand, by ${name}` : 'On demand'
    case 'production':
      return 'Automatic'
    case 'production_on_demand':
      return name ? `On demand, by ${name}` : 'On demand'
    default: {
      const never: never = trigger
      return never
    }
  }
}

function comparisonWord(
  comparison: Comparison,
  outcome: Outcome,
  module: string,
  sinceAt: string | null,
): string {
  const change = comparison.change
  const setting = module === 'settings'
  switch (comparison.kind) {
    case 'new':
      if (sinceAt) {
        const lead = `New since ${reportStamp(sinceAt)}`
        return change ? `${lead}. ${change}` : lead
      }
      return change ?? (setting ? 'New. The setting changed.' : 'New')
    case 'fixed':
      if (sinceAt) {
        const lead = `Fixed since ${reportStamp(sinceAt)}`
        return change ? `${lead}. ${change}` : lead
      }
      return change ?? (setting ? 'Fixed. The setting changed.' : 'Fixed')
    case 'unchanged':
      return 'Unchanged'
    case 'not_in_compared_run':
      return 'Not in the compared run'
    case 'named': {
      const base = comparison.prior_outcome
        ? `${outcomeWord(comparison.prior_outcome)} then ${outcomeWord(outcome)}`
        : outcomeWord(outcome)
      return change ? `${base}. ${change}` : base
    }
    default: {
      const never: never = comparison.kind
      return never
    }
  }
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

function productionWindow(report: ProductionReport): { start: string; end: string } | null {
  const body = report.evidence?.production
  if (!body || typeof body !== 'object') return null
  const span = (body as { window?: { start?: unknown; end?: unknown } }).window
  if (!span || typeof span.start !== 'string' || typeof span.end !== 'string') return null
  return { start: span.start, end: span.end }
}

const RUNNING_TIP = {
  source: 'Install readiness and the automatic-triage setting on this process',
  calculation:
    'The background service off is not checked, because nothing is collected. Automatic triage is the value supplied to this process. No alert source switched on needs a person.',
  limit: 'Not checked is never a pass.',
}

function isStatus(error: unknown, code: number): boolean {
  return (error as { response?: { status?: number } } | null)?.response?.status === code
}

function facts(check: Check): string | null {
  const evidence = check.evidence
  const parts: string[] = []
  if (evidence?.source_id) {
    parts.push(`Brings ${evidence.brings ?? 'lookups'}`)
    parts.push(evidence.last_data ? `Last data ${reportStamp(evidence.last_data)}` : 'Nothing received yet')
    if (evidence.labels && evidence.labels.length > 0) parts.push(`Labels ${evidence.labels.join(', ')}`)
  }
  if (evidence?.checked_at) parts.push(`Checked ${reportStamp(evidence.checked_at)}`)
  return parts.length > 0 ? parts.join(' · ') : null
}

function ofGroup(checks: Check[], id: string): Check[] {
  return checks.filter((check) => check.id === id || check.id.startsWith(`${id}.`))
}

/** Host-count integers the estate module stored. Zero is a count; an unset field is not. */
const ESTATE_COUNTS: { key: keyof Evidence; label: string }[] = [
  { key: 'listed', label: 'listed' },
  { key: 'days_7_to_30', label: 'in the last 7–30 days' },
  { key: 'over_30_days', label: 'over 30 days' },
  { key: 'never_connected', label: 'never connected' },
  { key: 'degraded', label: 'degraded' },
]

function countParts(evidence: Evidence): string[] {
  const parts: string[] = []
  for (const { key, label } of ESTATE_COUNTS) {
    const value = evidence[key]
    if (typeof value === 'number') parts.push(`${value} ${label}`)
  }
  return parts
}

/** The evidence a record stored, beside new, fixed, or unchanged. */
function storedLine(evidence: Evidence | null | undefined): string | null {
  if (!evidence) return null
  const parts: string[] = []
  if (evidence.fix) parts.push(evidence.fix)
  if (evidence.comparison) parts.push(evidence.comparison)
  if (evidence.coverage) parts.push(evidence.coverage)
  for (const line of evidence.prerequisites ?? []) parts.push(line)
  if (evidence.source_id) {
    parts.push(evidence.source_id)
    parts.push(`Brings ${evidence.brings ?? 'lookups'}`)
    if (evidence.state) parts.push(evidence.state.replace(/_/g, ' '))
    parts.push(evidence.last_data ? `Last data ${reportStamp(evidence.last_data)}` : 'Nothing received yet')
    if (evidence.labels && evidence.labels.length > 0) parts.push(`Labels ${evidence.labels.join(', ')}`)
  }
  parts.push(...countParts(evidence))
  if (evidence.checked_at) parts.push(`Checked ${reportStamp(evidence.checked_at)}`)
  return parts.length > 0 ? parts.join(' · ') : null
}

function pieces(value: string | null | undefined): string[] {
  if (!value) return []
  return value.split(';').map((part) => part.trim()).filter(Boolean)
}

function screenName(path: string): string {
  if (path.includes('section=data')) return 'Data & uploads'
  if (path.includes('section=audit')) return 'Audit trail'
  if (path.includes('section=federation')) return 'Alert collection'
  if (path.includes('section=autoinvestigate')) return 'Limits & autonomy'
  if (path.includes('section=integrations')) return 'Integrations'
  if (path.startsWith('/dashboard')) return 'Findings'
  return 'Session settings'
}

function figure(value: number): string {
  if (Number.isInteger(value)) return String(value)
  return value.toFixed(2).replace(/0+$/, '').replace(/\.$/, '')
}

function noun(value: number, singular: string, plural: string): string {
  return `${figure(value)} ${figure(value) === '1' ? singular : plural}`
}

function money(value: number): string {
  const text = value.toFixed(4).replace(/0+$/, '').replace(/\.$/, '')
  return `$${text}`
}

function busiestLine(hour: number, at: string | null | undefined): string {
  const when = at ? ` (${reportStamp(at)})` : ''
  return `The busiest hour had ${noun(hour, 'alert', 'alerts')}${when}, a lower bound summed across sources.`
}

function findingsLine(source: ForecastSource): string | null {
  switch (source.findings_status) {
    case 'not_connected':
      return null
    case 'measured':
      return `${noun(source.findings ?? 0, 'finding', 'findings')} stored on this install, measured here.`
    case 'not_checked':
      return source.findings_reason
        ? `Findings on this install: not checked. ${source.findings_reason}`
        : 'Findings on this install: not checked.'
    default: {
      const never: never = source.findings_status
      return never
    }
  }
}

function SourceRow({ source, showCalls }: { source: ForecastSource; showCalls: boolean }) {
  const measured = findingsLine(source)
  const alerts = `A normal week is ${noun(source.week, 'alert', 'alerts')}, estimated`
  const week = showCalls
    ? `${alerts}, and ${figure(source.week)} to ${figure(source.week * 2)} triage calls.`
    : `${alerts}.`
  return (
    <div className="d1-row">
      <p>
        {source.source}: {noun(source.opened, 'ticket', 'tickets')} the old process opened and later closed: a lower
        bound on alerts.
      </p>
      {source.opened_missing > 0 && (
        <p className="d1-facts">
          {noun(source.opened_missing, 'ticket', 'tickets')} had no open time and{' '}
          {figure(source.opened_missing) === '1' ? 'was' : 'were'} left out.
        </p>
      )}
      <p className="d1-facts">
        {reportStamp(source.window_start)} to {reportStamp(source.window_end)}.
      </p>
      {source.outside > 0 && (
        <p className="d1-facts">
          {noun(source.outside, 'older closure', 'older closures')}{' '}
          {figure(source.outside) === '1' ? 'is' : 'are'} outside this window.
        </p>
      )}
      <p className="d1-facts">{week}</p>
      {measured && <p className="d1-facts">{measured}</p>}
    </div>
  )
}

function LimitsBlock({
  limits,
  hour,
  at,
  known,
}: {
  limits: ForecastLimits
  hour: number | null | undefined
  at: string | null | undefined
  known: boolean
}) {
  if (limits.status === 'omitted') return null
  if (limits.status === 'not_checked') {
    return (
      <div className="d1-rows">
        <div className="d1-row">
          <p>{limits.reason}</p>
        </div>
      </div>
    )
  }
  const note = limitsNote(limits.service)
  return (
    <>
      <p className="d1-sub">Saved limits</p>
      <div className="d1-rows">
        <div className="d1-row">
          {note && <p>{note}</p>}
          {limits.enabled === false && <p>Investigations are not started on their own.</p>}
          <p>
            At most {noun(limits.concurrent ?? 0, 'investigation', 'investigations')} at once, each up to{' '}
            {noun(limits.minutes ?? 0, 'minute', 'minutes')}, and alerts beyond that wait.
          </p>
          {limits.covers == null ? (
            <p>The hourly cap could not be stated from the saved per-investigation cost.</p>
          ) : (
            <>
              <p>
                The hourly cap covers {noun(limits.covers, 'investigation', 'investigations')} at the full{' '}
                {money(limits.per_investigation ?? 0)} per investigation.
              </p>
              <p>If more than {figure(limits.covers)} were investigated at full cost, intake would pause.</p>
            </>
          )}
          {known && hour != null && <p>{busiestLine(hour, at)}</p>}
          {limits.triage === false && <p>No triage calls are made.</p>}
          {limits.triage == null && <p>Whether triage calls are made was not checked.</p>}
        </div>
      </div>
    </>
  )
}

function limitsNote(service: ForecastLimits['service']): string | null {
  switch (service) {
    case 'running':
    case 'stopped':
    case 'unavailable':
      return null
    case 'unknown':
      return 'The saved limits are stated, but whether the background service is running was not checked.'
    case 'not_serving':
      return 'The background service is not serving.'
    default: {
      const never: never = service
      return never
    }
  }
}

function SpendBlock({ spend }: { spend: ForecastSpend }) {
  if (spend.status === 'omitted') return null
  if (spend.status === 'not_checked') {
    return (
      <>
        <p className="d1-sub">Measured spend</p>
        <div className="d1-rows">
          <div className="d1-row">
            <p>{spend.reason}</p>
          </div>
        </div>
      </>
    )
  }
  const count = noun(spend.wholly_priced, 'wholly priced investigation', 'wholly priced investigations')
  return (
    <>
      <p className="d1-sub">Measured spend</p>
      <div className="d1-rows">
        <div className="d1-row">
          {spend.wholly_priced >= 20 && spend.median != null && (
            <p>From {count}: {money(spend.median)}.</p>
          )}
          {spend.wholly_priced > 0 && spend.wholly_priced < 20 && spend.low != null && spend.high != null && (
            <p>
              From {count}: {money(spend.low)} to {money(spend.high)}.
            </p>
          )}
          {spend.wholly_priced === 0 && <p>No wholly priced investigations.</p>}
          {spend.not_counted > 0 && (
            <p className="d1-facts">{noun(spend.not_counted, 'run', 'runs')} not counted.</p>
          )}
          {spend.unpriced_model > 0 && <p>The cap does not limit that model.</p>}
        </div>
      </div>
    </>
  )
}

function ForecastSection({ forecast }: { forecast: Forecast }) {
  const arrivals = forecast.arrivals
  const limits = forecast.limits
  const arrivalsOnly = limits.service === 'stopped' || limits.service === 'unavailable'
  const showCalls = limits.status === 'ok' && limits.triage === true
  const busiestWithArrivals = arrivals.status === 'ok' && (arrivalsOnly || limits.status !== 'ok')
  return (
    <section className="d1-card" aria-label="Cost forecast">
      <div className="d1-card-head">
        <h2>{MODULE_TITLE['history.forecast']}</h2>
        <InfoTip label="How the cost forecast is judged" align="start" {...FORECAST_TIP} />
      </div>
      {arrivalsOnly && (
        <div className="d1-rows">
          <div className="d1-row">
            <p>No automatic triage and no hourly cap run.</p>
          </div>
        </div>
      )}
      <p className="d1-sub">Arrivals</p>
      <div className="d1-rows">
        {arrivals.status === 'not_checked' ? (
          <div className="d1-row">
            <p>{arrivals.reason}</p>
            {arrivals.fix && (
              <p className="d1-fix">
                {arrivals.fix}
                {arrivals.screen && (
                  <>
                    {' '}
                    <Link to={arrivals.screen}>{screenName(arrivals.screen)}</Link>
                  </>
                )}
              </p>
            )}
          </div>
        ) : (
          <>
            {(arrivals.sources ?? []).map((source) => (
              <SourceRow key={source.source} source={source} showCalls={showCalls} />
            ))}
            {busiestWithArrivals && arrivals.busiest_hour != null && (
              <div className="d1-row">
                <p>{busiestLine(arrivals.busiest_hour, arrivals.busiest_at)}</p>
              </div>
            )}
          </>
        )}
      </div>
      <LimitsBlock
        limits={limits}
        hour={arrivals.busiest_hour}
        at={arrivals.busiest_at}
        known={arrivals.status === 'ok'}
      />
      <SpendBlock spend={forecast.spend} />
    </section>
  )
}

function Citation({ check }: { check: Check }) {
  const names = pieces(check.source_id)
  if (names.length === 0) return null
  const clauses = pieces(check.clause)
  const hrefs = pieces(check.link)
  const comparison = check.evidence?.comparison
  return (
    <p className="d1-compare">
      {comparison ? `${comparison.charAt(0).toUpperCase()}${comparison.slice(1)}; source: ` : 'Source: '}
      {names.map((name, index) => (
        <span key={`${name}-${index}`}>
          {index > 0 && '; '}
          {hrefs[index] ? (
            <a href={hrefs[index]} target="_blank" rel="noreferrer">
              {name}
            </a>
          ) : (
            name
          )}
          {clauses[index] ? `, ${clauses[index]}` : ''}
        </span>
      ))}
      .
    </p>
  )
}

function Versus({
  check,
  comparedAt,
  since,
}: {
  check: Check
  comparedAt: string | null
  since?: boolean
}) {
  const comparison = check.comparison
  if (!comparison) return null
  const showEvidence =
    comparison.kind === 'new' || comparison.kind === 'fixed' || comparison.kind === 'unchanged'
  const currentEvidence = showEvidence ? storedLine(check.evidence) : null
  const priorEvidence = showEvidence ? storedLine(comparison.prior_evidence) : null
  return (
    <div className="d1-versus">
      <p className="d1-versus-kind">
        {comparisonWord(comparison, check.outcome, check.module, since ? comparedAt : null)}
      </p>
      {currentEvidence && <p className="d1-facts">This report: {currentEvidence}</p>}
      {priorEvidence && comparedAt && <p className="d1-facts">{reportStamp(comparedAt)}: {priorEvidence}</p>}
    </div>
  )
}

function LinkedSource({ check }: { check: Check }) {
  const name = check.source_id ?? ''
  const href = pieces(check.link)[0]
  if (!href) return name
  return (
    <a href={href} target="_blank" rel="noreferrer">
      {name}
    </a>
  )
}

function ValueLine({ check }: { check: Check }) {
  const proposed = check.proposed ?? []
  return (
    <>
      {check.scope && check.cited_value && (
        <p className="d1-facts">Value for your selection: {check.cited_value}.</p>
      )}
      {check.scope && check.source_id && (
        <p className="d1-compare">
          Cited for {check.scope} by <LinkedSource check={check} />
          {check.clause ? `, ${check.clause}` : ''}.{check.date_checked ? ` Checked ${check.date_checked}.` : ''}
        </p>
      )}
      {!check.scope && (
        <>
          <Citation check={check} />
          {check.cited_value && (
            <p className="d1-facts">
              Cited value: {check.cited_value}.
              {check.date_checked ? ` Checked ${check.date_checked}.` : ''}
            </p>
          )}
        </>
      )}
      {proposed.map((item) => (
        <p className="d1-facts" key={`${item.scope}-${item.source_id}`}>
          proposed, not yet law: {item.cited_value} ({item.source_id})
        </p>
      ))}
      {check.window && <p className="d1-facts">{check.window}</p>}
      {check.gap && <p className="d1-facts">Stated gap: {check.gap}</p>}
    </>
  )
}

function selectionText(selection: NonNullable<Report['selection']>): string {
  if (selection.unreadable) return selection.unreadable
  const places = (selection.jurisdictions ?? []).join(', ')
  const kinds = selection.kinds ?? []
  if (kinds.length === 0) return places ? `${places}. No kind is stated.` : 'No kind is stated.'
  const named = kinds.join('; ')
  return places ? `${places}. ${named}.` : `${named}.`
}

function SettingsRow({
  check,
  comparedAt,
  since,
}: {
  check: Check
  comparedAt: string | null
  since?: boolean
}) {
  const silent = check.outcome === 'no_value'
  const tone = silent ? '' : outcomeTone(check.outcome)
  const screen = check.evidence?.screen
  const prerequisites = check.evidence?.prerequisites ?? []
  return (
    <div className="d1-row has-link">
      {!silent && (
        <span className={`state-pill ${tone}`}>
          <span className="state-dot" aria-hidden="true" />
          {outcomeWord(check.outcome)}
        </span>
      )}
      {silent && <p className="d1-facts">no cited value for your selection</p>}
      <p className="d1-reason">{check.reason}</p>
      <ValueLine check={check} />
      {check.evidence?.coverage && <p className="d1-facts">{check.evidence.coverage}</p>}
      {prerequisites.map((line) => (
        <p className="d1-facts" key={line}>
          {line}
        </p>
      ))}
      <Versus check={check} comparedAt={comparedAt} since={since} />
      {screen && <Link to={screen}>{screenName(screen)}</Link>}
    </div>
  )
}

function Withheld({ id }: { id: string }) {
  return (
    <section className="d1-card" aria-label={MODULE_TITLE[id] ?? id}>
      <h2>{MODULE_TITLE[id] ?? id}</h2>
      <p className="d1-withheld">Not included. This section needs {MODULE_PERMISSION[id] ?? id}.</p>
    </section>
  )
}

function CheckRow({
  check,
  comparedAt,
  note,
}: {
  check: Check
  comparedAt: string | null
  note?: string
}) {
  const tone = outcomeTone(check.outcome)
  const line = facts(check)
  return (
    <div className="d1-row">
      <span className={`state-pill ${tone}`}>
        <span className="state-dot" aria-hidden="true" />
        {outcomeWord(check.outcome)}
      </span>
      <p>{check.reason}</p>
      {note && (
        <p className="d1-facts">
          <span className="chip int-exp">Experimental</span> {note}
        </p>
      )}
      {check.evidence?.fix && <p className="d1-fix">{check.evidence.fix}</p>}
      {line && <p className="d1-facts">{line}</p>}
      <Versus check={check} comparedAt={comparedAt} />
    </div>
  )
}

/** The first three non-pass checks, in the order the checklist shows them. No cited value is left out. */
function summaryActions(report: Report): Check[] {
  return report.checks
    .filter((check) => check.outcome !== 'pass' && check.outcome !== 'no_value')
    .slice(0, 3)
}

function Summary({ report }: { report: Report }) {
  const hidden = new Set(report.withheld ?? [])
  const modules = report.modules ?? []
  // Visibility and estate share one row and one "can't see" block. Estate is not its own row.
  // History sections have figures and no outcomes, so they stay out of this grid.
  const seeVisibility = modules.includes('visibility') && !hidden.has('visibility')
  const seeEstate = modules.includes('estate') && !hidden.has('estate')
  const seeRan = seeVisibility || seeEstate
  const neitherRan = !modules.includes('visibility') && !modules.includes('estate')
  const areas: string[] = []
  let placedSee = false
  for (const id of modules) {
    if (hidden.has(id) || REPORT_SECTIONS.has(id)) continue
    if (id === 'visibility' || id === 'estate') {
      if (!placedSee && seeRan) {
        areas.push('visibility')
        placedSee = true
      }
      continue
    }
    areas.push(id)
  }
  const actions = summaryActions(report)
  const unmet = report.checks.filter(
    (check) =>
      check.outcome !== 'pass' &&
      ((seeVisibility && check.module === 'visibility') || (seeEstate && check.module === 'estate')),
  )
  const switchedOff = seeVisibility ? (report.switched_off ?? []) : []
  const showCantSee = neitherRan || seeRan
  return (
    <section className="d1-card d1-print" aria-label="Summary">
      <h2 className="d1-print-title">Summary</h2>
      <p className="d1-print-line">
        Describes this install&apos;s security gaps as of {reportStamp(report.recorded_at)}.
      </p>
      <div className="d1-print-block">
        <h2>Actions</h2>
        {actions.length > 0 ? (
          <ol className="d1-actions-list">
            {actions.map((check) => (
              <li key={check.id}>
                <span>{check.reason}</span>
                {check.evidence?.fix && <span className="d1-fix">{check.evidence.fix}</span>}
              </li>
            ))}
          </ol>
        ) : (
          <p className="d1-quiet">None.</p>
        )}
      </div>
      <div className="d1-print-block">
        <h2>Area by outcome</h2>
        {areas.length > 0 ? (
          <table className="d1-grid">
            <thead>
              <tr>
                <th scope="col">Area</th>
                {OUTCOMES.map((outcome) => (
                  <th key={outcome} scope="col">
                    {outcomeWord(outcome)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {areas.map((id) => {
                const counts = { pass: 0, fail: 0, not_checked: 0, needs_a_person: 0 }
                for (const check of report.checks) {
                  const included =
                    id === 'visibility'
                      ? (seeVisibility && check.module === 'visibility') ||
                        (seeEstate && check.module === 'estate')
                      : check.module === id
                  if (!included) continue
                  if (check.outcome === 'no_value') continue
                  counts[check.outcome] += 1
                }
                return (
                  <tr key={id}>
                    <th scope="row">{MODULE_TITLE[id] ?? id}</th>
                    {OUTCOMES.map((outcome) => (
                      <td key={outcome} data-label={outcomeWord(outcome)}>
                        {counts[outcome]}
                      </td>
                    ))}
                  </tr>
                )
              })}
            </tbody>
          </table>
        ) : (
          <p className="d1-quiet">None.</p>
        )}
      </div>
      {showCantSee && (
        <div className="d1-print-block">
          <h2>What Vigil can&apos;t see</h2>
          {neitherRan && <p className="d1-quiet">What Vigil can see did not run.</p>}
          {seeRan && unmet.length === 0 && switchedOff.length === 0 && (
            <p className="d1-quiet">None.</p>
          )}
          {(unmet.length > 0 || switchedOff.length > 0) && (
            <ul className="d1-gaps">
              {unmet.map((check) => (
                <li key={check.id}>{check.reason}</li>
              ))}
              {switchedOff.map((row) => (
                <li key={row.source_id}>
                  {row.source_id}: switched off. Brings {row.brings}.
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  )
}

export default function ReportScreen() {
  const [params, setParams] = useSearchParams()
  const record = params.get('record')
  const { hasPermission } = useAuth()
  const canRun = hasPermission('platform.write')
  const { notify } = useToast()
  const [report, setReport] = useState<Loaded | null>(null)
  const [modules, setModules] = useState<ModuleSetting[] | null>(null)
  const [savingModule, setSavingModule] = useState<string | null>(null)
  const [missing, setMissing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [runningSince, setRunningSince] = useState<string | null>(null)
  const [incompleteSince, setIncompleteSince] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    const statusRead = day1Api.status().then(
      (res) => res.data,
      (err: unknown) => (isStatus(err, 403) ? null : Promise.reject(err)),
    )
    const reportRead = day1Api.get(record ?? undefined).then(
      (res) => ({ ok: true as const, data: res.data as Loaded }),
      async (err: unknown) => {
        if (record && isStatus(err, 404)) {
          try {
            const res = await productionReportApi.get(record)
            return { ok: true as const, data: res.data as Loaded }
          } catch (prodErr) {
            return { ok: false as const, err: prodErr }
          }
        }
        return { ok: false as const, err }
      },
    )
    const moduleRead = day1Api.modules().then(
      (res) => ({ ok: true as const, data: res.data }),
      (err: unknown) => ({ ok: false as const, err }),
    )
    let lineError: string | null = null
    try {
      const line = await statusRead
      setRunningSince(line?.running_since ?? null)
      setIncompleteSince(line?.incomplete_since ?? null)
    } catch (err) {
      setRunningSince(null)
      setIncompleteSince(null)
      lineError = errorText(err, 'Could not load the Day 1 Report')
    }
    const moduleBody = await moduleRead
    if (moduleBody.ok) setModules(moduleBody.data)
    else if (!isStatus(moduleBody.err, 403)) {
      lineError = lineError ?? errorText(moduleBody.err, 'Could not load report modules')
    }
    const body = await reportRead
    if (body.ok) {
      setReport(body.data)
      setMissing(false)
      setError(lineError)
      return
    }
    if (isStatus(body.err, 404)) {
      setReport(null)
      setMissing(true)
      setError(lineError)
      return
    }
    if (!isStatus(body.err, 403)) setError(errorText(body.err, 'Could not load the Day 1 Report'))
    else setError(lineError)
  }, [record])

  useEffect(() => {
    void load()
  }, [load])

  const run = async () => {
    setBusy(true)
    try {
      const { data } = await day1Api.run()
      setParams(data.id ? { record: data.id } : {})
      notify('ok', 'Day 1 Report is ready.')
    } catch (err) {
      if (!stepUpDetailOf(err)) notify('err', errorText(err, 'Could not run the Day 1 Report'))
    } finally {
      setBusy(false)
    }
  }

  const setModule = async (id: string, enabled: boolean) => {
    setSavingModule(id)
    setModules((rows) => rows?.map((row) => (row.id === id ? { ...row, enabled } : row)) ?? rows)
    try {
      const { data } = await day1Api.setModule(id, enabled)
      setModules((rows) => rows?.map((row) => (row.id === data.id ? data : row)) ?? rows)
    } catch (err) {
      setModules((rows) => rows?.map((row) => (row.id === id ? { ...row, enabled: !enabled } : row)) ?? rows)
      if (!stepUpDetailOf(err)) notify('err', errorText(err, 'Could not save the report module'))
    } finally {
      setSavingModule(null)
    }
  }

  const day1 = report && !isProduction(report) ? report : null
  const produced = report && isProduction(report) ? report : null
  const checks = report?.checks ?? []
  const visibilityChecks = checks.filter((check) => check.module === 'visibility')
  const settings = checks.filter((check) => check.module === 'settings')
  const coverageChecks = checks.filter((check) => check.module === 'coverage')
  const estateChecks = checks.filter((check) => check.module === 'estate')
  const problems = visibilityChecks.filter((check) => check.outcome !== 'pass')
  const settled = visibilityChecks.filter(
    (check) => check.outcome === 'pass' && !check.id.startsWith('visibility.source.'),
  )
  const sources = visibilityChecks.filter(
    (check) => check.outcome === 'pass' && check.id.startsWith('visibility.source.'),
  )
  const settingProblems = settings.filter((check) => check.outcome !== 'pass')
  const settingSettled = settings.filter((check) => check.outcome === 'pass')
  const withheld = (report?.withheld ?? []).filter((id) => id in MODULE_TITLE)
  const showVisibility =
    report !== null && !withheld.includes('visibility') && (report.modules ?? []).includes('visibility')
  const coverageRan = (report?.modules ?? []).includes('coverage')
  const showCoverage = coverageRan && !withheld.includes('coverage')
  const estateRan = (report?.modules ?? []).includes('estate')
  const showEstate = estateRan && !withheld.includes('estate')
  const coverageGrouped = COVERAGE_GROUPS.map((group) => ({
    ...group,
    checks: ofGroup(coverageChecks, group.id),
  }))
  const coverageRest = coverageChecks.filter(
    (check) => !coverageGrouped.some((group) => group.checks.includes(check)),
  )
  const comparedAt = day1?.compared_at ?? null
  const settingsComparedAt = produced?.compared_at ?? comparedAt
  const span = produced ? productionWindow(produced) : null
  const runningChecks = produced
    ? produced.checks.filter((check) => check.module === 'production.running')
    : []
  const quietChecks = produced
    ? produced.checks.filter((check) => check.module === 'production.quiet')
    : []
  const quietBody = quietEvidence(produced?.evidence?.['production.quiet'])
  const alertsBody = alertsEvidence(produced?.evidence?.['production.alerts'])
  const changesBody = changesEvidence(produced?.evidence?.['production.changes'])

  return (
    <div className="d1-page">
      <div className="d1-screen">
        {day1?.selection && (
          <p className="d1-selection">
            {selectionText(day1.selection)}
            {canRun && (
              <>
                {' '}
                <Link to="/settings?section=jurisdictions">System tab</Link>
              </>
            )}
          </p>
        )}
        {report?.preface && <p className="d1-preface">{report.preface}</p>}
        {produced && span && (
          <p className="d1-preface">
            Describes this install&apos;s first {windowLength(span.start, span.end)} in production, as of{' '}
            {reportStamp(produced.recorded_at)}.
          </p>
        )}
        {report && (
          <p className="d1-meta">
            {reportStamp(report.recorded_at)} · Vigil {report.vigil_version} ·{' '}
            {triggerWord(report.trigger, report.actor_name ?? null)}
          </p>
        )}
        {comparedAt && <p className="d1-meta">Compared with {reportStamp(comparedAt)}</p>}
        {(runningSince || incompleteSince) && (
          <p className="d1-quiet" role="status">
            {runningSince && <>Running since {reportStamp(runningSince)}. </>}
            {incompleteSince && <>Didn't complete.</>}
          </p>
        )}
        {error && <p role="alert">{error}</p>}
        {missing && !report && <p className="d1-quiet">No Day 1 Report yet.</p>}
        {(canRun || report) && (
          <div className="d1-actions">
            {canRun && (
              <button type="button" className="btn primary" onClick={() => void run()} disabled={busy}>
                {busy ? 'Running…' : 'Run report'}
              </button>
            )}
            {report && (
              <button type="button" className="btn" onClick={() => window.print()}>
                Print summary
              </button>
            )}
          </div>
        )}
        {modules && modules.length > 0 && (
          <section className="d1-card" aria-label="Report modules">
            <h2>Report modules</h2>
            <p className="d1-quiet">A switched-off module is left out of the next run.</p>
            {modules.map((row) => (
              <ToggleRow
                key={row.id}
                label={MODULE_TITLE[row.id] ?? row.id}
                hint={row.enabled ? 'On for the next run' : 'Switched off'}
                checked={row.enabled}
                disabled={!canRun || savingModule !== null}
                onChange={(enabled) => void setModule(row.id, enabled)}
              />
            ))}
          </section>
        )}
      </div>
      {day1 && <Summary report={day1} />}
      <div className="d1-screen">
        {produced && withheld.includes('production.running') && <Withheld id="production.running" />}
        {produced && !withheld.includes('production.running') && (produced.modules ?? []).includes('production.running') && (
          <section className="d1-card" aria-label="What is running">
            <div className="d1-card-head">
              <h2>{MODULE_TITLE['production.running']}</h2>
              <InfoTip label="How what is running is judged" align="start" {...RUNNING_TIP} />
            </div>
            {runningChecks.length > 0 ? (
              <div className="d1-rows">
                {runningChecks.map((check) => (
                  <CheckRow key={check.id} check={check} comparedAt={null} />
                ))}
              </div>
            ) : (
              <p className="d1-quiet">Nothing to report in this section.</p>
            )}
          </section>
        )}
        {produced && withheld.includes('production.quiet') && <Withheld id="production.quiet" />}
        {produced && !withheld.includes('production.quiet') && (produced.modules ?? []).includes('production.quiet') && (
          <QuietSection checks={quietChecks} evidence={quietBody} tip={QUIET_TIP} />
        )}
        {produced && withheld.includes('production.alerts') && <Withheld id="production.alerts" />}
        {produced && !withheld.includes('production.alerts') && (produced.modules ?? []).includes('production.alerts') && alertsBody && (
          <AlertsSection evidence={alertsBody} tip={ALERTS_TIP} />
        )}
        {produced && <ProductionFigures report={produced} />}
        {day1 && withheld.includes('visibility') && <Withheld id="visibility" />}
        {day1 && showVisibility && (
          <section className="d1-card" aria-label="What Vigil can see">
            <div className="d1-card-head">
              <h2>{MODULE_TITLE.visibility}</h2>
              <InfoTip label="How what Vigil can see is judged" align="start" {...TIP} />
            </div>
            {problems.length > 0 && (
              <div className="d1-rows">
                {problems.map((check) => (
                  <CheckRow key={check.id} check={check} comparedAt={comparedAt} />
                ))}
              </div>
            )}
            {settled.length > 0 && (
              <div className="d1-rows">
                {settled.map((check) => (
                  <CheckRow key={check.id} check={check} comparedAt={comparedAt} />
                ))}
              </div>
            )}
            {sources.length > 0 && (
              <>
                <p className="d1-sub">Sources</p>
                <div className="d1-rows">
                  {sources.map((check) => (
                    <CheckRow key={check.id} check={check} comparedAt={comparedAt} />
                  ))}
                </div>
              </>
            )}
            {(day1.switched_off ?? []).length > 0 && (
              <>
                <p className="d1-sub">Switched off</p>
                <div className="d1-rows">
                  {day1.switched_off.map((row) => (
                    <div className="d1-row" key={row.source_id}>
                      <p>
                        {row.source_id}: switched off. Brings {row.brings}.
                      </p>
                    </div>
                  ))}
                </div>
              </>
            )}
            {problems.length === 0 && settled.length === 0 && sources.length === 0 && (
              <p className="d1-quiet">Nothing to report in this section.</p>
            )}
          </section>
        )}
        {settings.length > 0 && (
          <section className="d1-card" aria-label="Vigil's own settings">
            <div className="d1-card-head">
              <h2>{MODULE_TITLE.settings}</h2>
              <InfoTip label="How Vigil's own settings are judged" align="start" {...SETTINGS_TIP} />
            </div>
            {produced && !produced.compared_at && (
              <p className="d1-quiet">not checked: no Day 1 Report to compare with</p>
            )}
            {settingProblems.length > 0 && (
              <div className="d1-rows">
                {settingProblems.map((check) => (
                  <SettingsRow
                    key={check.id}
                    check={check}
                    comparedAt={settingsComparedAt}
                    since={produced !== null}
                  />
                ))}
              </div>
            )}
            {settingSettled.length > 0 && (
              <div className="d1-rows">
                {settingSettled.map((check) => (
                  <SettingsRow
                    key={check.id}
                    check={check}
                    comparedAt={settingsComparedAt}
                    since={produced !== null}
                  />
                ))}
              </div>
            )}
          </section>
        )}
        {produced && (produced.modules ?? []).includes('production.changes') && (
          <ChangesSection evidence={changesBody} tip={CHANGES_TIP} />
        )}
        {coverageRan && withheld.includes('coverage') && <Withheld id="coverage" />}
        {showCoverage && (
          <section className="d1-card" aria-label="Detection coverage">
            <div className="d1-card-head">
              <h2>{MODULE_TITLE.coverage}</h2>
              <InfoTip label="How detection coverage is judged" align="start" {...COVERAGE_TIP} />
            </div>
            {coverageGrouped.map((group) =>
              group.checks.length === 0 ? null : (
                <Fragment key={group.id}>
                  <p className="d1-sub">{group.title}</p>
                  <div className="d1-rows">
                    {group.checks.map((check) => (
                      <CheckRow key={check.id} check={check} comparedAt={comparedAt} />
                    ))}
                  </div>
                </Fragment>
              ),
            )}
            {coverageRest.length > 0 && (
              <div className="d1-rows">
                {coverageRest.map((check) => (
                  <CheckRow key={check.id} check={check} comparedAt={comparedAt} />
                ))}
              </div>
            )}
            {coverageChecks.length === 0 && <p className="d1-quiet">Nothing to report in this section.</p>}
          </section>
        )}
        {day1 && withheld.includes('history.before') && <Withheld id="history.before" />}
        {day1 && withheld.includes('history.recheck') && <Withheld id="history.recheck" />}
        {day1 && withheld.includes('history.tuning') && <Withheld id="history.tuning" />}
        {day1 && <HistorySections report={day1} />}
        {estateRan && withheld.includes('estate') && <Withheld id="estate" />}
        {showEstate && (
          <section className="d1-card" aria-label="What CrowdStrike, Defender and Wazuh still list">
            <div className="d1-card-head">
              <h2>{MODULE_TITLE.estate}</h2>
              <InfoTip
                label="How hosts CrowdStrike, Defender and Wazuh still list are judged"
                align="start"
                {...ESTATE_TIP}
              />
            </div>
            {estateChecks.length > 0 ? (
              <div className="d1-rows">
                {estateChecks.map((check) => (
                  <CheckRow
                    key={check.id}
                    check={check}
                    comparedAt={comparedAt}
                    note={check.id.startsWith('estate.crowdstrike.') ? EXPERIMENTAL_NOTE : undefined}
                  />
                ))}
              </div>
            ) : (
              <p className="d1-quiet">Nothing to report in this section.</p>
            )}
          </section>
        )}
        {day1?.forecast && (day1.modules ?? []).includes('history.forecast') && (
          <ForecastSection forecast={day1.forecast} />
        )}
      </div>
    </div>
  )
}
