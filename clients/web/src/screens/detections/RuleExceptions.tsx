import { useEffect, useState, type FormEvent } from 'react'
import { useAuth } from '../../contexts/AuthContext'
import { errorText } from '../../shared/errorText'
import { utcDayClock } from '../../shared/utc'
import { detectionRulesApi } from '../../services/api'

interface RuleException {
  exception_id: number
  match: Record<string, unknown>
  note: string
  created_by: string | null
  created_at: string | null
  removed_at: string | null
  removed_by: string | null
  removal_reason: string | null
  /** Matches per UTC day, as Vigil's runner counted them (the last 14 days). */
  days: Record<string, number>
  /** The runner's last run that fetched rows: how many, and which match fields none carried. */
  seen: { rows: number; missing: string[]; at: string } | null
}

interface Listing {
  handed_off: boolean
  exceptions: RuleException[]
}

/** The days the runner keeps per-day counts for (rollout.KEEP_DAYS). */
const KEEP_DAYS = 14

function Match({ match }: { match: Record<string, unknown> }) {
  return (
    <div className="de-x-match">
      {Object.entries(match).map(([f, v]) => <code key={f}>{f} = {String(v)}</code>)}
    </div>
  )
}

/** A match field absent from every row of the last run (a transforming search's input field, say). */
function Missing({ seen }: { seen: RuleException['seen'] }) {
  if (!seen?.missing?.length) return null
  const one = seen.rows === 1
  const verb = seen.missing.length === 1 ? 'was' : 'were'
  return (
    <div className="de-x-miss" title={`Last run with rows: ${utcDayClock(seen.at)}`}>
      {seen.missing.map((f, i) => <span key={f}>{i > 0 && ', '}<code>{f}</code></span>)}{' '}
      {one
        ? `${verb} not in the 1 row this rule's last run fetched, so this exception can't match it.`
        : `${verb} in none of the ${seen.rows.toLocaleString()} rows this rule's last run fetched, so this exception can't match them.`}
    </div>
  )
}

function matched(days: Record<string, number>): { total: number; title: string } {
  const entries = Object.entries(days).sort(([a], [b]) => a.localeCompare(b))
  return {
    total: entries.reduce((a, [, v]) => a + (Number(v) || 0), 0),
    title:
      'Hits this exception dropped before they became findings, so none of them count as fires.\n\n' +
      (entries.length ? entries.map(([d, v]) => `${d}: ${v}`).join('\n') : 'None counted yet'),
  }
}

/** An overlay copy's exceptions: Vigil's runner drops what they match before counting or alerting. */
export default function RuleExceptions({ ruleId }: { ruleId: number }) {
  const canWrite = useAuth().hasPermission('detections.write')
  const [data, setData] = useState<Listing | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const [removing, setRemoving] = useState<number | null>(null)

  const load = () =>
    detectionRulesApi.exceptions(ruleId)
      .then((res) => { setData(res.data as Listing); setError(null) })
      .catch((e) => setError(errorText(e, 'Exceptions did not load.')))
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load() }, [ruleId])

  if (!data) return <div className="de-x">{error ? <span className="de-x-err" role="alert">{error}</span> : <span className="x1-muted">Reading exceptions…</span>}</div>

  const active = data.exceptions.filter((x) => !x.removed_at)
  const removed = data.exceptions.filter((x) => x.removed_at)
  return (
    <div className="de-x">
      <div className="de-x-head">
        <b>Exceptions <span>· {active.length} active</span></b>
        {data.handed_off && <span className="de-x-off">Not applied: the SIEM runs this rule</span>}
        {canWrite && !adding && <button type="button" className="x1-link de-x-add" onClick={() => setAdding(true)}>Add exception</button>}
      </div>
      {error && <span className="de-x-err" role="alert">{error}</span>}
      {active.length === 0 && !adding && <p className="de-x-none">No active exceptions. Vigil counts every document this rule matches.</p>}
      {active.length > 0 && (
        <ul className={`de-x-list${data.handed_off ? ' off' : ''}`}>
          {active.map((x) => {
            const m = matched(x.days)
            return (
              <li key={x.exception_id}>
                <div className="de-x-body">
                  <Match match={x.match} />
                  <div className="de-x-note" title={x.note}>{x.note}</div>
                  <div className="de-x-by">Added by {x.created_by ?? 'unknown'}{x.created_at && ` · ${utcDayClock(x.created_at)}`}</div>
                  <Missing seen={x.seen} />
                </div>
                <div className="de-x-side">
                  <span className="de-x-n" title={m.title}><b>{m.total.toLocaleString()}</b> {m.total === 1 ? 'hit' : 'hits'} silenced in {KEEP_DAYS}d</span>
                  {canWrite && removing !== x.exception_id && (
                    <button type="button" className="x1-link de-x-rm" onClick={() => setRemoving(x.exception_id)}>Remove</button>
                  )}
                </div>
                {removing === x.exception_id && (
                  <RemoveForm ruleId={ruleId} exceptionId={x.exception_id} onDone={(changed) => { if (changed) load().finally(() => setRemoving(null)); else setRemoving(null) }} />
                )}
              </li>
            )
          })}
        </ul>
      )}
      {adding && <AddForm ruleId={ruleId} onDone={(changed) => { if (changed) load().finally(() => setAdding(false)); else setAdding(false) }} />}
      {removed.length > 0 && (
        <details className="de-x-removed">
          <summary>Removed · {removed.length}</summary>
          <ul className="de-x-list">
            {removed.map((x) => (
              <li key={x.exception_id}>
                <div className="de-x-body">
                  <Match match={x.match} />
                  <div className="de-x-note" title={x.note}>{x.note}</div>
                  <div className="de-x-by">Added by {x.created_by ?? 'unknown'}{x.created_at && ` · ${utcDayClock(x.created_at)}`}</div>
                  <div className="de-x-by" title={x.removal_reason ?? ''}>Removed by {x.removed_by ?? 'unknown'} · {utcDayClock(x.removed_at)} · {x.removal_reason}</div>
                </div>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}

function AddForm({ ruleId, onDone }: { ruleId: number; onDone: (changed: boolean) => void }) {
  const [pairs, setPairs] = useState<[string, string][]>([['', '']])
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const set = (i: number, k: 0 | 1, v: string) => setPairs((p) => p.map((pair, j) => (j === i ? (k ? [pair[0], v] : [v, pair[1]]) : pair)))

  const submit = (e: FormEvent) => {
    e.preventDefault()
    const match = Object.fromEntries(pairs.filter(([f]) => f.trim()).map(([f, v]) => [f.trim(), v]))
    setBusy(true)
    detectionRulesApi.addException(ruleId, { match, note })
      .then(() => onDone(true))
      .catch((err) => { setError(errorText(err, 'The exception was not added.')); setBusy(false) })
  }

  return (
    <form className="de-x-form" onSubmit={submit}>
      {pairs.map(([f, v], i) => (
        <div key={i} className="de-x-pair">
          <input className="de-x-in mono" aria-label="Field" placeholder="Field, as the rule names it (host.name)" value={f} onChange={(e) => set(i, 0, e.target.value)} />
          <span aria-hidden="true">=</span>
          <input className="de-x-in mono" aria-label="Value" placeholder="Value, or a CIDR for an address" value={v} onChange={(e) => set(i, 1, e.target.value)} />
          {pairs.length > 1 && <button type="button" className="x1-link de-x-rm" aria-label="Remove this pair" onClick={() => setPairs((p) => p.filter((_, j) => j !== i))}>Remove</button>}
        </div>
      ))}
      <button type="button" className="x1-link de-x-more" onClick={() => setPairs((p) => [...p, ['', '']])}>+ Another field (all must match)</button>
      <input className="de-x-in" aria-label="Note" placeholder="Why this is expected here (required)" value={note} onChange={(e) => setNote(e.target.value)} />
      {error && <span className="de-x-err" role="alert">{error}</span>}
      <div className="de-x-actions">
        <button type="submit" className="btn primary de-x-btn" disabled={busy}>{busy ? 'Adding…' : 'Add exception'}</button>
        <button type="button" className="btn ghost de-x-btn" onClick={() => onDone(false)}>Cancel</button>
      </div>
    </form>
  )
}

function RemoveForm({ ruleId, exceptionId, onDone }: { ruleId: number; exceptionId: number; onDone: (changed: boolean) => void }) {
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const submit = (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    detectionRulesApi.removeException(ruleId, exceptionId, reason)
      .then(() => onDone(true))
      .catch((err) => { setError(errorText(err, 'The exception was not removed.')); setBusy(false) })
  }
  return (
    <form className="de-x-form de-x-remove" onSubmit={submit}>
      <input className="de-x-in" aria-label="Reason" placeholder="Why remove it (required)" value={reason} onChange={(e) => setReason(e.target.value)} autoFocus />
      {error && <span className="de-x-err" role="alert">{error}</span>}
      <div className="de-x-actions">
        <button type="submit" className="btn de-x-btn danger" disabled={busy}>{busy ? 'Removing…' : 'Remove exception'}</button>
        <button type="button" className="btn ghost de-x-btn" onClick={() => onDone(false)}>Cancel</button>
      </div>
    </form>
  )
}
