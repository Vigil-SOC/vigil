import { useEffect, useState, type FormEvent } from 'react'
import { useAuth } from '../../contexts/AuthContext'
import { errorText } from '../../shared/errorText'
import { utcDayClock } from '../../shared/utc'
import { detectionRulesApi, type RuleStages as Stages } from '../../services/api'
import { sampledText, stageLine, unprovenNote, waitingNote, who, type Stage, type StageFields } from './ruleStage'
import DeployAgain from './DeployAgain'

const STAGE_NAME: Record<Stage, string> = { shadow: 'Shadow', live: 'Live', off: 'Off' }

/** An overlay rule's rollout moves, newest first, read when the panel opens, with
 * Revert to shadow (live) or Release (held by a person) where Add exception sits on
 * the Exceptions panel. ``row`` is the rule's listing row, refetched after a change. */
export default function RuleStages({ ruleId, row, asking: askFirst, onChanged }: {
  ruleId: number
  row: StageFields
  /** Opened from the line's Revert link: ask why straight away. */
  asking: boolean
  /** A Revert or Release was written: the listing (counts and this row) is read again. */
  onChanged: () => void
}) {
  const canWrite = useAuth().hasPermission('detections.write')
  const [asking, setAsking] = useState(askFirst)
  const [data, setData] = useState<Stages | null>(null)
  const [error, setError] = useState<string | null>(null)
  const load = () =>
    detectionRulesApi.ruleStages(ruleId)
      .then((res) => { setData(res.data); setError(null) })
      .catch((e) => setError(errorText(e, 'Its rollout did not load.')))
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load() }, [ruleId])

  const line = stageLine(row)
  const waiting = waitingNote(row, canWrite)
  const unproven = line?.held ? null : unprovenNote(row)
  const action = !canWrite || !line ? null : line.live ? 'revert' : line.held ? 'release' : null
  const changed = () => { load(); onChanged() }
  if (!data) {
    return (
      <div className="de-x">
        {error ? <span className="de-x-err" role="alert">{error}</span> : <span className="x1-muted">Reading its rollout…</span>}
      </div>
    )
  }
  const rows = data.stages
  return (
    <div className="de-x">
      <div className="de-x-head">
        <b>Rollout <span>· {plural(rows.length, 'move', 'moves')}</span></b>
        {waiting?.again && canWrite && <DeployAgain ruleId={ruleId} className="de-x-add" onDone={changed} />}
        {action && !asking && (
          <button type="button" className={`x1-link ${waiting?.again && canWrite ? 'de-x-also' : 'de-x-add'}`} onClick={() => setAsking(true)}>{action === 'revert' ? 'Revert to shadow' : 'Release'}</button>
        )}
      </div>
      {action && asking && <Ask ruleId={ruleId} action={action} onDone={(done) => { setAsking(false); if (done) changed() }} />}
      {rows.length === 0 ? (
        <p className="de-x-none">No moves yet.</p>
      ) : (
        <ul className="de-x-list de-st-list">
          {rows.map((r) => (
            <li key={r.stage_id}>
              <span className="de-st-when" title={r.at}>{utcDayClock(r.at)}</span>
              <span className="de-st-move">{r.from_stage ? `${STAGE_NAME[r.from_stage]} → ${STAGE_NAME[r.to_stage]}` : STAGE_NAME[r.to_stage]}</span>
              <span className="de-st-why" title={r.reason}><b className={r.actor === 'system' ? 'vigil' : ''}>{who(r.actor)}</b> · {r.reason || '—'}</span>
            </li>
          ))}
        </ul>
      )}
      {row.sampled && <p className="de-x-none" data-testid="rule-sampled">{sampledText(row.sampled)}</p>}
      <p className="de-x-none">
        {waiting
          ? waiting.panel
          : line?.live
          ? canWrite
            ? 'Revert takes it out of production: analysts stop getting its alerts, and Vigil keeps running it in shadow until a person releases it.'
            : 'Live: analysts get its alerts. A person who can edit detections can move it back to shadow.'
          : line?.held
            ? 'Held in shadow: it runs but alerts nobody, and stays there until a person releases it.'
            : unproven
              ? `In shadow it runs but alerts nobody. ${unproven}`
              : 'In shadow it runs but alerts nobody. Vigil moves it live after 7 quiet days with its data arriving, once it is shown to fire.'}
      </p>
    </div>
  )
}

const plural = (v: number, one: string, many: string) => `${v.toLocaleString()} ${v === 1 ? one : many}`

function Ask({ ruleId, action, onDone }: { ruleId: number; action: 'revert' | 'release'; onDone: (changed: boolean) => void }) {
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (!reason.trim()) { setError('Say why.'); return }
    setBusy(true)
    const call = action === 'revert' ? detectionRulesApi.revertRule : detectionRulesApi.releaseRule
    call(ruleId, reason.trim())
      .then(() => onDone(true))
      .catch((err) => { setError(errorText(err, action === 'revert' ? 'Could not revert it.' : 'Could not release it.')); setBusy(false) })
  }
  return (
    <form className="de-x-form de-x-remove" onSubmit={submit}>
      <input className="de-x-in" aria-label="Reason" placeholder={action === 'revert' ? 'Why move it back to shadow (required)' : 'Why release it (required)'} value={reason} onChange={(e) => setReason(e.target.value)} autoFocus />
      {error && <span className="de-x-err" role="alert">{error}</span>}
      <div className="de-x-actions">
        <button type="submit" className="btn primary de-x-btn" disabled={busy}>{busy ? 'Saving…' : action === 'revert' ? 'Revert to shadow' : 'Release'}</button>
        <button type="button" className="btn ghost de-x-btn" onClick={() => onDone(false)}>Cancel</button>
      </div>
    </form>
  )
}
