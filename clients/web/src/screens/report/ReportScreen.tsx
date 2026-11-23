import { useCallback, useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { day1Api } from '../../services/api'
import type { Schema } from '../../services/apiTypes'
import { useAuth } from '../../contexts/AuthContext'
import { errorText } from '../../shared/errorText'
import { InfoTip } from '../../shared/InfoTip'
import { ToggleRow } from '../../shared/ui'
import { useToast } from '../../shell/toast'
import { stepUpDetailOf } from '../../services/stepUp'
import { reportStamp } from './stamp'
import './report.css'

type Report = Schema<'Day1Report'>
type Check = Schema<'Day1Check'>
type Evidence = Schema<'Day1Evidence'>
type Comparison = Schema<'Day1Comparison'>
type ModuleSetting = Schema<'Day1Module'>
type Outcome = Check['outcome']
type Trigger = Report['trigger']

const MODULE_TITLE: Record<string, string> = {
  visibility: 'What Vigil can see',
  settings: "Vigil's own settings",
}

const SETTINGS_TIP = {
  source: 'The session policy and the audit-trail retention period',
  calculation:
    'A value passes when it is at or stricter than the cited value. An unset period fails. The MFA count is shown beside the setting and does not change the outcome.',
  limit: 'Session checks need sessions.manage. Retention needs audit.read. Not checked is never a pass.',
}

const OUTCOMES: Outcome[] = ['pass', 'fail', 'not_checked', 'needs_a_person']

const TIP = {
  source: 'Integration Health and the install readiness read',
  calculation:
    'A switched-on source passes when it is healthy, needs a person when it requires a test, is not checked when its health says so, and fails otherwise. One failure does not fail it. Last data and webhook labels are information.',
  limit: 'Hidden without integrations.read. Not checked is never a pass.',
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

function comparisonWord(comparison: Comparison, outcome: Outcome): string {
  switch (comparison.kind) {
    case 'new':
      return 'New'
    case 'fixed':
      return 'Fixed'
    case 'unchanged':
      return 'Unchanged'
    case 'not_in_compared_run':
      return 'Not in the compared run'
    case 'named':
      return comparison.prior_outcome
        ? `${outcomeWord(comparison.prior_outcome)} then ${outcomeWord(outcome)}`
        : outcomeWord(outcome)
    default: {
      const never: never = comparison.kind
      return never
    }
  }
}

function isStatus(error: unknown, code: number): boolean {
  return (error as { response?: { status?: number } } | null)?.response?.status === code
}

function facts(check: Check): string | null {
  const evidence = check.evidence
  if (!evidence?.source_id) return null
  const parts = [`Brings ${evidence.brings ?? 'lookups'}`]
  parts.push(evidence.last_data ? `Last data ${reportStamp(evidence.last_data)}` : 'Nothing received yet')
  if (evidence.labels && evidence.labels.length > 0) parts.push(`Labels ${evidence.labels.join(', ')}`)
  return parts.join(' · ')
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
  return parts.length > 0 ? parts.join(' · ') : null
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

function Versus({ check, comparedAt }: { check: Check; comparedAt: string | null }) {
  const comparison = check.comparison
  if (!comparison) return null
  const showEvidence =
    comparison.kind === 'new' || comparison.kind === 'fixed' || comparison.kind === 'unchanged'
  const currentEvidence = showEvidence ? storedLine(check.evidence) : null
  const priorEvidence = showEvidence ? storedLine(comparison.prior_evidence) : null
  return (
    <div className="d1-versus">
      <p className="d1-versus-kind">{comparisonWord(comparison, check.outcome)}</p>
      {currentEvidence && <p className="d1-facts">This report: {currentEvidence}</p>}
      {priorEvidence && comparedAt && <p className="d1-facts">{reportStamp(comparedAt)}: {priorEvidence}</p>}
    </div>
  )
}

function SettingsRow({ check, comparedAt }: { check: Check; comparedAt: string | null }) {
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
      <Versus check={check} comparedAt={comparedAt} />
      {screen && <Link to={screen}>{screenName(screen)}</Link>}
    </div>
  )
}

function CheckRow({ check, comparedAt }: { check: Check; comparedAt: string | null }) {
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
      <Versus check={check} comparedAt={comparedAt} />
    </div>
  )
}

/** The first three non-pass checks, in the order the checklist shows them. */
function summaryActions(report: Report): Check[] {
  return report.checks.filter((check) => check.outcome !== 'pass').slice(0, 3)
}

function Summary({ report }: { report: Report }) {
  const hidden = new Set(report.withheld ?? [])
  const areas = (report.modules ?? []).filter((id) => !hidden.has(id))
  const actions = summaryActions(report)
  const visibilityHidden = hidden.has('visibility')
  const visibilityRan = (report.modules ?? []).includes('visibility')
  const unmet = visibilityHidden
    ? []
    : report.checks.filter((check) => check.module === 'visibility' && check.outcome !== 'pass')
  const switchedOff = visibilityHidden ? [] : (report.switched_off ?? [])
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
                  if (check.module === id) counts[check.outcome] += 1
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
      {!visibilityHidden && (
        <div className="d1-print-block">
          <h2>What Vigil can&apos;t see</h2>
          {!visibilityRan && <p className="d1-quiet">What Vigil can see did not run.</p>}
          {visibilityRan && unmet.length === 0 && switchedOff.length === 0 && (
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
  const [report, setReport] = useState<Report | null>(null)
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
      (res) => ({ ok: true as const, data: res.data }),
      (err: unknown) => ({ ok: false as const, err }),
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

  const checks = report?.checks ?? []
  const visibility = checks.filter((check) => check.module !== 'settings')
  const settings = checks.filter((check) => check.module === 'settings')
  const problems = visibility.filter((check) => check.outcome !== 'pass')
  const settled = visibility.filter(
    (check) => check.outcome === 'pass' && !check.id.startsWith('visibility.source.'),
  )
  const sources = visibility.filter(
    (check) => check.outcome === 'pass' && check.id.startsWith('visibility.source.'),
  )
  const settingProblems = settings.filter((check) => check.outcome !== 'pass')
  const settingSettled = settings.filter((check) => check.outcome === 'pass')
  const withheld = (report?.withheld ?? []).filter((id) => id === 'visibility')
  const showVisibility = report !== null && !withheld.includes('visibility') && (report.modules ?? []).includes('visibility')
  const comparedAt = report?.compared_at ?? null

  return (
    <div className="d1-page">
      <div className="d1-screen">
        {report?.preface && <p className="d1-preface">{report.preface}</p>}
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
      {report && <Summary report={report} />}
      <div className="d1-screen">
        {report && withheld.length > 0 && (
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
                  <SettingsRow key={check.id} check={check} comparedAt={comparedAt} />
                ))}
              </div>
            )}
            {settingSettled.length > 0 && (
              <div className="d1-rows">
                {settingSettled.map((check) => (
                  <SettingsRow key={check.id} check={check} comparedAt={comparedAt} />
                ))}
              </div>
            )}
          </section>
        )}
      </div>
    </div>
  )
}
