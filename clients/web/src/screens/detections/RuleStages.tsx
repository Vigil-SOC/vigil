import { useState, type FormEvent } from 'react'
import { useAuth } from '../../contexts/AuthContext'
import { errorText } from '../../shared/errorText'
import { utcDayClock } from '../../shared/utc'
import { relativeTime } from '../settings/integrationHealth'
import { detectionRulesApi, type RuleStageRow, type RuleStages as Stages } from '../../services/api'

/** How long Vigil's own move back to shadow stays amber on the rule's line. */
const RECENT_MS = 7 * 24 * 60 * 60 * 1000
const STAGE_NAME: Record<RuleStageRow['to_stage'], string> = { shadow: 'Shadow', live: 'Live', off: 'Off' }

const who = (actor: string) => (actor === 'system' ? 'Vigil' : actor)
const dayMonth = (iso: string) => new Date(iso).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })

/** The person's Revert that holds the rule: their newest live -> shadow move. */
const revertOf = (rows: RuleStageRow[]) => rows.find((r) => r.actor !== 'system' && r.from_stage === 'live' && r.to_stage === 'shadow') ?? null

export interface StageLine {
  text: string
  /** The full reason, for the hover. */
  title: string
  /** Vigil moved it back to shadow in the last 7 days. */
  amber: boolean
  live: boolean
  held: boolean
}

/** A Vigil rule's stage, as the grey line under its name says it. Null before its first stage. */
export function stageLine({ stages: rows, held: isHeld }: Stages): StageLine | null {
  const now = rows[0]
  if (!now) return null
  const held = isHeld ? revertOf(rows) : null
  const backByVigil = now.to_stage === 'shadow' && now.from_stage === 'live' && now.actor === 'system'
  const base = { title: `${who(now.actor)}: ${now.reason}`, amber: false, live: now.to_stage === 'live', held: isHeld }
  if (backByVigil && Date.now() - new Date(now.at).getTime() < RECENT_MS) {
    return { ...base, amber: true, text: `Vigil moved it back to shadow ${relativeTime(now.at)}` }
  }
  if (isHeld && now.to_stage === 'shadow') return { ...base, ...(held && { title: `${held.actor}: ${held.reason}` }), text: held ? `In shadow, held by ${held.actor} since ${dayMonth(held.at)}` : 'In shadow, held by a person' }
  if (now.to_stage === 'live') return { ...base, text: `Live since ${dayMonth(now.at)}` }
  if (now.to_stage === 'off') return { ...base, text: `Off since ${dayMonth(now.at)}` }
  return { ...base, text: `In shadow since ${dayMonth(now.at)}` }
}

/** An overlay rule's rollout moves, newest first, with Revert to shadow (live) or
 * Release (held by a person) where Add exception sits on the Exceptions panel. */
export default function RuleStages({ ruleId, data, error, asking: askFirst, onChanged }: {
  ruleId: number
  data: Stages | null
  error: string | null
  /** Opened from the line's Revert link: ask why straight away. */
  asking: boolean
  onChanged: () => void
}) {
  const canWrite = useAuth().hasPermission('detections.write')
  const [asking, setAsking] = useState(askFirst)
  if (!data) return <div className="de-x">{error ? <span className="de-x-err" role="alert">{error}</span> : <span className="x1-muted">Reading its rollout…</span>}</div>

  const rows = data.stages
  const line = stageLine(data)
  const action = !canWrite || !line ? null : line.live ? 'revert' : line.held ? 'release' : null
  return (
    <div className="de-x">
      <div className="de-x-head">
        <b>Rollout <span>· {plural(rows.length, 'move', 'moves')}</span></b>
        {action && !asking && (
          <button type="button" className="x1-link de-x-add" onClick={() => setAsking(true)}>{action === 'revert' ? 'Revert to shadow' : 'Release'}</button>
        )}
      </div>
      {action && asking && <Ask ruleId={ruleId} action={action} onDone={(changed) => { setAsking(false); if (changed) onChanged() }} />}
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
      <p className="de-x-none">
        {line?.live
          ? 'Revert takes it out of production: analysts stop getting its alerts, and Vigil keeps running it in shadow until a person releases it.'
          : line?.held
            ? 'Held in shadow: it runs but alerts nobody, and stays there until a person releases it.'
            : 'In shadow it runs but alerts nobody. Vigil moves it live after 7 quiet days with its data arriving.'}
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
