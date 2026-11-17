import { useState } from 'react'
import { InfoTip } from '../../shared/InfoTip'
import { utcDayClock } from '../../shared/utc'
import type { FixLevel, FixLevelName } from './medicTypes'
import { safeText } from './safeText'
import { problemCode, problemReason, setFixLevel } from './useMedic'

const MAX_REASON = 500
const LEVELS: FixLevelName[] = ['L0', 'L1', 'L2']
const LEVEL_WORD: Record<FixLevelName, string> = { L0: 'observe: a click is refused', L1: 'one click', L2: 'on its own' }
const RESET_WORD = { store_reset: 'Store reset', early_purge: 'Early purge' }

// Medic sends the reason of a refusal as a closed set; it sends no text of its own.
const REFUSED: Record<string, string> = {
  no_change: 'It is already at that level.',
  persistence_off: 'Medic is told persistence is off, so it cannot keep a level. Turn persistence on before raising to L2.',
  no_rule: 'The active pack has no rule for this fix type.',
  observe: 'Medic is in observe-only mode.',
}
const PROBLEM: Record<string, string> = {
  medic_off: 'Medic is off.',
  medic_unavailable: "Medic isn't answering. Try again in a moment.",
  busy: 'Medic was too busy to take it. Try again in a moment.',
  store_not_recording: "Medic isn't recording, so it changed nothing.",
}
const failure = (e: unknown) => {
  const code = problemCode(e)
  if (code === 'fix_refused') return `Refused: ${REFUSED[problemReason(e)] ?? 'Medic would not change the level.'} The level stays.`
  return `${PROBLEM[code] ?? "Couldn't reach Medic."} Check the level on this page before trying again.`
}

const when = (iso: string) => utcDayClock(iso) ?? safeText(iso)
const n = (v: number) => v.toLocaleString()

function Card({ f, admin, onChanged }: { f: FixLevel; admin: boolean; onChanged: (next: FixLevel) => void }) {
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<{ good: boolean; text: string } | null>(null)
  const at = LEVELS.indexOf(f.level)
  const up = LEVELS[at + 1]
  const lower = LEVELS.slice(0, at)
  const t = f.track_record

  const change = async (level: FixLevelName, why?: string) => {
    setBusy(true); setNote(null)
    try {
      const next = await setFixLevel(f.fix_type, level, why)
      setReason('')
      onChanged(next)
      setNote({ good: true, text: `Now ${next.level}.` })
    } catch (e) {
      setNote({ good: false, text: failure(e) })
    } finally { setBusy(false) }
  }

  return (
    <section className="mdc-card" aria-label={`Fix type ${safeText(f.fix_type)}`}>
      <h3><span className="mdc-mono">{safeText(f.fix_type)}</span><InfoTip label="About fix levels" source="Medic's decision log; Medic counts it, the console does not." calculation="L0 refuses a click, L1 is one click, L2 lets Medic run the fix itself. Medic lowers L2 to L1 on repeated failures, a changed rule or pack, or a store reset; it never raises." limit="Retention and the size cap thin the log, so the counts cover only the window shown." /></h3>
      <dl className="mdc-facts mdc-fix">
        <dt>Level</dt>
        <dd>
          <strong>{f.level}</strong> · {LEVEL_WORD[f.level]}
          {f.unreadable && <span className="mdc-expiry poor"> · Its settings can no longer be read, so it is L0 until an admin raises it</span>}
        </dd>
        {f.level === 'L2' && f.raised_on && (<><dt>Raised on</dt><dd>{when(f.raised_on.at)} · rule revision {f.raised_on.revision} · pack <span className="mdc-mono">{safeText(f.raised_on.pack_id)}</span></dd></>)}
      </dl>

      <h4 className="mdc-sub">Track record</h4>
      {!t ? <p className="mdc-quiet">Medic could not read its decision log, so it has no counts to show.</p> : (
        <dl className="mdc-facts mdc-fix">
          <dt>Window</dt><dd>{t.since ? `Counted from ${when(t.since)}, the oldest record Medic still holds` : 'The decision log has no records yet'}</dd>
          <dt>Proposed</dt><dd>{n(t.proposed)}</dd>
          <dt>Ran</dt><dd>{n(t.ran.person + t.ran.medic)} · {n(t.ran.person)} by a person, {n(t.ran.medic)} by Medic</dd>
          <dt>Cleared</dt><dd>{n(t.cleared)}</dd>
          <dt>Failed</dt><dd>{n(t.failed)}</dd>
          <dt>Did not come back</dt><dd>{n(t.not_back)}</dd>
          <dt>Not verified</dt><dd>{n(t.not_verified)}</dd>
          <dt>Escalations</dt><dd>{n(t.escalations)}</dd>
          <dt>Medic lowered it</dt><dd>{n(t.lowered_by_medic)}</dd>
          <dt>Reset or purge</dt>
          <dd>{t.resets.length === 0 ? 'None in this window' : (
            <ul className="mdc-list">{t.resets.map((r) => <li key={`${r.kind}${r.at}`}>{RESET_WORD[r.kind] ?? safeText(r.kind)} · {when(r.at)}</li>)}</ul>
          )}</dd>
        </dl>
      )}

      {admin && (
        <div className="mdc-level">
          {up && (
            <>
              <label className="mdc-quiet" htmlFor={`mdc-why-${f.fix_type}`}>Reason to raise to {up}</label>
              <textarea id={`mdc-why-${f.fix_type}`} className="field-input" rows={2} maxLength={MAX_REASON} value={reason} disabled={busy}
                onChange={(e) => setReason(e.target.value.slice(0, MAX_REASON))} />
              <p className="mdc-quiet">
                {up === 'L2' ? 'At L2 Medic restarts soc-daemon on its own when the incident routes. ' : 'At L1 a click on Run fix restarts soc-daemon. '}
                A restart stops ingestion, and findings accepted but not yet stored are dropped. Polled and webhook sources do not resend them. Kafka is re-read.
              </p>
            </>
          )}
          <div className="mdc-actions">
            {up && <button type="button" className="btn primary" disabled={busy || !reason.trim()} onClick={() => change(up, reason.trim())}>Raise to {up}</button>}
            {lower.map((l) => <button key={l} type="button" className="btn" disabled={busy} onClick={() => change(l)}>Lower to {l}</button>)}
          </div>
        </div>
      )}
      {note && <p className={note.good ? 'mdc-saved' : 'mdc-save-failed'} role={note.good ? 'status' : 'alert'}>{note.text}</p>}
    </section>
  )
}

/** Each fix type's level and track record, with the admin's raise (one step, with a reason) and lower (no reason). */
export function FixTypes({ types, admin, onChanged }: { types: FixLevel[] | undefined; admin: boolean; onChanged: (next: FixLevel) => void }) {
  if (!types || types.length === 0) {
    return <section className="mdc-card" aria-label="Fix types"><h3>Fix types</h3><p className="mdc-quiet">No fix types to show. Levels are only available when Vigil runs on Compose.</p></section>
  }
  return <>{types.map((f) => <Card key={f.fix_type} f={f} admin={admin} onChanged={onChanged} />)}</>
}
