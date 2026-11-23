import { Fragment, useCallback, useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { day1Api } from '../../services/api'
import type { Schema } from '../../services/apiTypes'
import { useAuth } from '../../contexts/AuthContext'
import { errorText } from '../../shared/errorText'
import { InfoTip } from '../../shared/InfoTip'
import { useToast } from '../../shell/toast'
import { stepUpDetailOf } from '../../services/stepUp'
import { reportStamp } from './stamp'
import './report.css'

type Report = Schema<'Day1Report'>
type Check = Schema<'Day1Check'>
type Outcome = Check['outcome']
type Trigger = Report['trigger']

const MODULE_TITLE: Record<string, string> = {
  visibility: 'What Vigil can see',
  settings: "Vigil's own settings",
  coverage: 'Detection coverage',
}

const SETTINGS_TIP = {
  source: 'The session policy and the audit-trail retention period',
  calculation:
    'A value passes when it is at or stricter than the cited value. An unset period fails. The MFA count is shown beside the setting and does not change the outcome.',
  limit: 'Session checks need sessions.manage. Retention needs audit.read. Not checked is never a pass.',
}

const TIP = {
  source: 'Integration Health and the install readiness read',
  calculation:
    'A switched-on source passes when it is healthy, needs a person when it requires a test, is not checked when its health says so, and fails otherwise. One failure does not fail it. Last data and webhook labels are information.',
  limit: 'Hidden without integrations.read. Not checked is never a pass.',
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

function triggerWord(trigger: Trigger, name: string | null): string {
  switch (trigger) {
    case 'readiness':
      return 'Readiness'
    case 'fallback':
      return 'Fallback time'
    case 'on_demand':
      return name ? `On demand, by ${name}` : 'On demand'
    default: {
      const never: never = trigger
      return never
    }
  }
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

function pieces(value: string | null | undefined): string[] {
  if (!value) return []
  return value.split(';').map((part) => part.trim()).filter(Boolean)
}

function screenName(path: string): string {
  return path.includes('section=audit') ? 'Audit trail' : 'Session settings'
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

function SettingsRow({ check }: { check: Check }) {
  const tone = outcomeTone(check.outcome)
  const screen = check.evidence?.screen
  const prerequisites = check.evidence?.prerequisites ?? []
  return (
    <div className="d1-row has-link">
      <span className={`state-pill ${tone}`}>
        <span className="state-dot" aria-hidden="true" />
        {outcomeWord(check.outcome)}
      </span>
      <p className="d1-reason">{check.reason}</p>
      <Citation check={check} />
      {check.cited_value && (
        <p className="d1-facts">
          Cited value: {check.cited_value}.
          {check.date_checked ? ` Checked ${check.date_checked}.` : ''}
        </p>
      )}
      {check.evidence?.coverage && <p className="d1-facts">{check.evidence.coverage}</p>}
      {prerequisites.map((line) => (
        <p className="d1-facts" key={line}>
          {line}
        </p>
      ))}
      {screen && <Link to={screen}>{screenName(screen)}</Link>}
    </div>
  )
}

function CheckRow({ check }: { check: Check }) {
  const tone = outcomeTone(check.outcome)
  const line = facts(check)
  return (
    <div className="d1-row">
      <span className={`state-pill ${tone}`}>
        <span className="state-dot" aria-hidden="true" />
        {outcomeWord(check.outcome)}
      </span>
      <p>{check.reason}</p>
      {check.evidence?.fix && <p className="d1-fix">{check.evidence.fix}</p>}
      {line && <p className="d1-facts">{line}</p>}
    </div>
  )
}

export default function ReportScreen() {
  const [params, setParams] = useSearchParams()
  const record = params.get('record')
  const { hasPermission } = useAuth()
  const canRun = hasPermission('platform.write')
  const { notify } = useToast()
  const [report, setReport] = useState<Report | null>(null)
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

  const checks = report?.checks ?? []
  const visibilityChecks = checks.filter((check) => check.module === 'visibility')
  const settings = checks.filter((check) => check.module === 'settings')
  const coverageChecks = checks.filter((check) => check.module === 'coverage')
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
  const showVisibility = report !== null && !withheld.includes('visibility')
  const coverageRan = (report?.modules ?? []).includes('coverage')
  const showCoverage = coverageRan && !withheld.includes('coverage')
  const coverageGrouped = COVERAGE_GROUPS.map((group) => ({
    ...group,
    checks: ofGroup(coverageChecks, group.id),
  }))
  const coverageRest = coverageChecks.filter(
    (check) => !coverageGrouped.some((group) => group.checks.includes(check)),
  )

  return (
    <div className="d1-page">
      {report?.preface && <p className="d1-preface">{report.preface}</p>}
      {report && (
        <p className="d1-meta">
          {reportStamp(report.recorded_at)} · Vigil {report.vigil_version} ·{' '}
          {triggerWord(report.trigger, report.actor_name ?? null)}
        </p>
      )}
      {(runningSince || incompleteSince) && (
        <p className="d1-quiet" role="status">
          {runningSince && <>Running since {reportStamp(runningSince)}. </>}
          {incompleteSince && <>Didn't complete.</>}
        </p>
      )}
      {error && <p role="alert">{error}</p>}
      {missing && !report && <p className="d1-quiet">No Day 1 Report yet.</p>}
      {canRun && (
        <div className="d1-actions">
          <button type="button" className="btn primary" onClick={() => void run()} disabled={busy}>
            {busy ? 'Running…' : 'Run report'}
          </button>
        </div>
      )}
      {report && withheld.includes('visibility') && (
        <section className="d1-card" aria-label="What Vigil can see">
          <h2>What Vigil can see</h2>
          <p className="d1-withheld">Not included. This section needs integrations.read.</p>
        </section>
      )}
      {showVisibility && (
        <section className="d1-card" aria-label="What Vigil can see">
          <div className="d1-card-head">
            <h2>{MODULE_TITLE.visibility}</h2>
            <InfoTip label="How what Vigil can see is judged" align="start" {...TIP} />
          </div>
          {problems.length > 0 && (
            <div className="d1-rows">
              {problems.map((check) => (
                <CheckRow key={check.id} check={check} />
              ))}
            </div>
          )}
          {settled.length > 0 && (
            <div className="d1-rows">
              {settled.map((check) => (
                <CheckRow key={check.id} check={check} />
              ))}
            </div>
          )}
          {sources.length > 0 && (
            <>
              <p className="d1-sub">Sources</p>
              <div className="d1-rows">
                {sources.map((check) => (
                  <CheckRow key={check.id} check={check} />
                ))}
              </div>
            </>
          )}
          {(report.switched_off ?? []).length > 0 && (
            <>
              <p className="d1-sub">Switched off</p>
              <div className="d1-rows">
                {report.switched_off.map((row) => (
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
          {settingProblems.length > 0 && (
            <div className="d1-rows">
              {settingProblems.map((check) => (
                <SettingsRow key={check.id} check={check} />
              ))}
            </div>
          )}
          {settingSettled.length > 0 && (
            <div className="d1-rows">
              {settingSettled.map((check) => (
                <SettingsRow key={check.id} check={check} />
              ))}
            </div>
          )}
        </section>
      )}
      {coverageRan && withheld.includes('coverage') && (
        <section className="d1-card" aria-label="Detection coverage">
          <h2>Detection coverage</h2>
          <p className="d1-withheld">Not included. This section needs detections.read.</p>
        </section>
      )}
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
                    <CheckRow key={check.id} check={check} />
                  ))}
                </div>
              </Fragment>
            ),
          )}
          {coverageRest.length > 0 && (
            <div className="d1-rows">
              {coverageRest.map((check) => (
                <CheckRow key={check.id} check={check} />
              ))}
            </div>
          )}
          {coverageChecks.length === 0 && <p className="d1-quiet">Nothing to report in this section.</p>}
        </section>
      )}
    </div>
  )
}
