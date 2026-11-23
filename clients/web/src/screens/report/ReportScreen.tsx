import { useCallback, useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
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
}

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
  const problems = checks.filter((check) => check.outcome !== 'pass')
  const settled = checks.filter(
    (check) => check.outcome === 'pass' && !check.id.startsWith('visibility.source.'),
  )
  const sources = checks.filter(
    (check) => check.outcome === 'pass' && check.id.startsWith('visibility.source.'),
  )
  const withheld = (report?.withheld ?? []).filter((id) => id === 'visibility')
  const showVisibility = report !== null && !withheld.includes('visibility')

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
    </div>
  )
}
