import { useMemo } from 'react'
import { InfoTip } from '../../shared/InfoTip'
import { EmptyState } from '../../shared/ui'
import { Icon } from '../../shared/icons'
import type { DecisionRecord, IncidentOpened, StatusRead } from './medicTypes'
import { safeText, safeTitle } from './safeText'
import { useIncidentDetail } from './useMedic'
import { durationWord, evidenceOf, ratingWord, stamp, stateTone, stateWord, timeline, wouldHave } from './words'
import './medic.css'

/** Incident detail (UX-15…17): the "would have" panel, the timeline and the evidence, all text. */
export function IncidentPane({ id, status, onBack, onOpen }: {
  id: string
  status: StatusRead
  onBack: () => void
  onOpen: (id: string) => void
}) {
  const { detail, records, phase, code, reload } = useIncidentDetail(id, status.state === 'running')

  const view = useMemo(() => {
    if (!detail) return null
    const opened = detail.opened.body as IncidentOpened
    // the opened record also comes back in the decision list; keep one of each seq
    const bySeq = new Map<number, DecisionRecord>([[detail.opened.seq, detail.opened]])
    records.forEach((r) => bySeq.set(r.seq, r))
    const all = [...bySeq.values()]
    return {
      opened,
      would: wouldHave(opened, detail.summary.lane.value, all),
      rows: timeline(detail.opened, all),
      evidence: evidenceOf(opened, all.filter((r) => r.seq !== detail.opened.seq)),
    }
  }, [detail, records])

  const back = (
    <button type="button" className="mdc-back" onClick={onBack}><Icon name="chevL" size={14} /> Incidents</button>
  )

  if (!detail || !view) {
    return (
      <div className="mdc-body">
        <div className="mdc-crumb">{back}</div>
        {status.state !== 'running' ? (
          <EmptyState error icon="alert" title={status.state === 'down' ? 'Medic is down' : "Vigil can't tell if Medic is running"} body="This incident can be read again once Medic answers." />
        ) : phase === 'error' ? (
          <EmptyState error icon="alert" title={code === 'not_found' ? 'This incident is gone' : "Couldn't load this incident"}
            body={code === 'not_found' ? 'Medic no longer holds it, or the link is wrong.' : undefined}
            primary={code === 'not_found' ? undefined : { label: 'Retry', onClick: reload, icon: 'refresh' }}
            secondary={{ label: 'Back to incidents', onClick: onBack }} />
        ) : (
          <EmptyState loading icon="clock" title="Loading incident…" />
        )}
      </div>
    )
  }

  const { summary: s } = detail
  const advice = view.opened.advice
  const rating = ratingWord(s)

  return (
    <div className="mdc-body">
      <div className="mdc-crumb">{back}</div>
      <header className="mdc-head">
        <h2 title={safeTitle(s.title)}>{safeText(s.title)}</h2>
        <p className="mdc-meta">
          <span className={`mdc-state ${stateTone(s)}`}>{stateWord(s)}</span>
          <span>lane {s.lane.value}</span>
          <span>{durationWord(s, Date.now())}</span>
          <span>opened {stamp(s.opened_at)}</span>
          {s.rule.id && <span className="mdc-mono">rule {safeText(s.rule.id)} r{s.rule.revision}</span>}
          <span className="mdc-mono">pack {safeText(s.rule.pack_id)} {safeText(s.rule.pack_version)}</span>
        </p>
        {s.group.length > 0 && (
          <p className="mdc-meta">
            {s.group.map((g) => <span key={g.name} className="mdc-mono">{safeText(g.name)}: {safeText(g.value)}</span>)}
          </p>
        )}
        <p className="mdc-meta">
          <span>{rating ? `Rating: ${safeText(rating)}` : 'Unrated'}</span>
          {s.ratings.map((r) => <span key={`${r.admin}${r.seq}`} className="mdc-mono">{safeText(r.admin)} · {stamp(r.at, s.opened_at)}</span>)}
        </p>
      </header>

      {s.suppressed_by && (
        <p className="mdc-note">
          Suppressed by <button type="button" className="mdc-link mdc-mono" onClick={() => onOpen(s.suppressed_by!)}>{safeText(s.suppressed_by)}</button>; its cause is rated for both.
        </p>
      )}
      {s.suppressed_children.count > 0 && (
        <p className="mdc-note">
          {s.suppressed_children.count} symptom{s.suppressed_children.count === 1 ? '' : 's'} suppressed by this:{' '}
          {s.suppressed_children.incident_ids.map((c) => (
            <button key={c} type="button" className="mdc-link mdc-mono" onClick={() => onOpen(c)}>{safeText(c)}</button>
          ))}
        </p>
      )}

      <section className="mdc-card mdc-would" aria-label="What Medic would have done">
        <h3>What Medic would have done</h3>
        {view.would.map((l, n) => <p key={n} className={n === view.would.length - 1 ? 'mdc-nothing' : undefined}>{safeText(l)}</p>)}
      </section>

      {advice && (
        <section className="mdc-card" aria-label="Pack advice">
          <h3>Advice from the pack</h3>
          <pre className="mdc-pre">{safeText(advice.summary)}</pre>
          <pre className="mdc-pre">{safeText(advice.fix)}</pre>
        </section>
      )}

      <div className="mdc-cols">
        <section className="mdc-card" aria-label="Timeline">
          <h3>Timeline</h3>
          <ol className="mdc-timeline">
            {view.rows.map((r) => (
              <li key={r.key}>
                <span className="mdc-mono mdc-at" title={safeTitle(r.at)}>{stamp(r.at, s.opened_at)}</span>
                <span className="mdc-what-line">
                  {safeText(r.text)}
                  {r.link && <> <button type="button" className="mdc-link mdc-mono" onClick={() => onOpen(r.link!)}>{safeText(r.link)}</button></>}
                  {r.note && <pre className="mdc-pre">{safeText(r.note)}</pre>}
                </span>
              </li>
            ))}
          </ol>
        </section>
        <section className="mdc-card" aria-label="Evidence">
          <h3>Evidence ({view.evidence.length})
            <InfoTip label="About evidence" text="What Medic saw when it opened the incident, redacted before it was stored. Shown as plain text." align="start" />
          </h3>
          {view.evidence.length === 0 && <p className="mdc-quiet">This incident carries no evidence.</p>}
          {view.evidence.map((e, n) => (
            <div key={`${e.observation_id}${n}`} className="mdc-evidence">
              <p className="mdc-meta">
                <span className="mdc-mono">{safeText(e.signal)}</span>
                <span className="mdc-mono">{stamp(e.t, s.opened_at)}</span>
                <span className="mdc-mono">obs {safeText(e.observation_id)}</span>
                {e.excerpt && <span className="mdc-mono">redaction {safeText(e.excerpt.redaction_version)}</span>}
                {e.key !== undefined && <span className="mdc-mono">{safeText(e.key)}{e.value !== undefined ? ` = ${safeText(e.value)}` : ''}</span>}
              </p>
              {e.excerpt && <pre className="mdc-pre">{safeText(e.excerpt.text)}</pre>}
            </div>
          ))}
        </section>
      </div>
    </div>
  )
}
