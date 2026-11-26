import { useState } from 'react'
import { Icon } from '../../shared/icons'
import { utcDayClock } from '../../shared/utc'
import { DISMISS_REASONS, type DismissReason, type HistoryEntry, type EvidenceCell, type EvidenceColumnKind, type HeldSuggestion, type Suggestion } from './supervisoryTypes'
import { FN_LABEL, ago, since } from './words'

/** "Apply 0.90 → 0.93", or "Apply · turn on" for a switch. */
function applyLabel(s: Suggestion): string {
  return /^turn /i.test(s.verb) ? `Apply · ${s.verb.toLowerCase()}` : `Apply ${s.from} → ${s.to}`
}

function Cell({ cell, kind, openCase }: { cell: EvidenceCell; kind: EvidenceColumnKind; openCase: (id: string) => void }) {
  const cls = `${kind === 'text' ? '' : 'spv-mono'}${cell.attack ? ' spv-attack' : ''}`
  if (kind === 'case') {
    return <td><button type="button" className="spv-link spv-mono" onClick={() => openCase(cell.text)}>{cell.text}</button></td>
  }
  return <td className={cls || undefined}>{cell.text}</td>
}

function Evidence({ s, openCase }: { s: Suggestion; openCase: (id: string) => void }) {
  return (
    <div className="spv-evidence" id={`spv-ev-${s.id}`}>
      <div className="spv-plain" role="note">
        <Icon name="info" size={18} />
        <div>
          <span className="spv-plain-label">In plain words</span>
          <p>{s.plain_words}</p>
        </div>
      </div>
      <div className="spv-ev-table">
        <span className="spv-sub">{s.table.title}</span>
        <div className="spv-tbl-wrap">
          <table className="spv-tbl">
            <thead>
              <tr>{s.table.columns.map((c) => <th key={c.label} scope="col">{c.label}</th>)}</tr>
            </thead>
            <tbody>
              {s.table.rows.map((row, i) => (
                <tr key={i}>
                  {row.map((cell, j) => <Cell key={j} cell={cell} kind={s.table.columns[j]?.kind ?? 'text'} openCase={openCase} />)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <span className="spv-foot">{s.table.foot}</span>
      </div>
      <div className="spv-ev-replay">
        <span className="spv-sub">{s.replay_title}</span>
        {s.replay_rows.map((r) => (
          <div key={r.label} className="spv-replay-row">
            <span>{r.label}</span>
            <span className="spv-mono">
              <span className="spv-now">{r.now}</span> → <span className={`spv-next ${r.effect}`}>{r.next}</span>
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

export function SuggestionCard({
  s,
  expanded,
  onToggle,
  canWrite,
  applied,
  onApply,
  onUndo,
  onDismiss,
  openCase,
}: {
  s: Suggestion
  expanded: boolean
  onToggle: () => void
  /** the viewer holds ai_autonomy.write, which Apply and Dismiss both need; without it the card only shows its evidence */
  canWrite: boolean
  /** set for a short window after an apply, while the card offers Undo */
  applied: HistoryEntry | null
  onApply: () => Promise<void>
  onUndo: () => Promise<void>
  onDismiss: (reason: DismissReason) => Promise<void>
  openCase: (id: string) => void
}) {
  const [dismissing, setDismissing] = useState(false)
  const [busy, setBusy] = useState<'apply' | 'undo' | 'dismiss' | null>(null)
  const [failed, setFailed] = useState<'apply' | 'undo' | 'dismiss' | null>(null)
  const run = (what: 'apply' | 'undo' | 'dismiss', call: () => Promise<void>) => {
    setBusy(what)
    setFailed(null)
    call().then(() => setBusy(null), () => { setBusy(null); setFailed(what) })
  }
  const applying = busy === 'apply' || busy === 'dismiss'
  const blocked = !s.apply.possible
  return (
    <article className={`spv-card spv-sugg${s.widens_autonomy ? ' widens' : ''}`} aria-label={`${s.verb} ${s.key} ${s.from} to ${s.to}`}>
      <div className="spv-sugg-body">
        <div className="spv-sugg-top">
          <span className="spv-tags">
            <span className="spv-fn">{FN_LABEL[s.fn]}</span>
            {s.widens_autonomy && (
              <span className="spv-tag fair"><Icon name="alert" size={11} />Widens autonomy</span>
            )}
          </span>
          <span className="spv-quiet" title={utcDayClock(s.suggested_at) ?? undefined}>Suggested {ago(s.suggested_at)}</span>
        </div>
        <h3 className="spv-change">
          {s.verb} <code>{s.key}</code> <span className="spv-mono spv-fromto">{s.from} → {s.to}</span>
        </h3>
        <p className="spv-why">{s.why}</p>
        <div className="spv-facts">
          <div className="spv-fact"><span>Evidence</span><b>{s.evidence}</b></div>
          <div className="spv-fact"><span>Replay on your history</span><b>{s.replay}</b></div>
          {s.check && (
            <div className="spv-fact">
              <span>30-case check</span>
              <b className={s.check.passed ? 'spv-pass' : 'spv-broke'}>
                <Icon name={s.check.passed ? 'check' : 'x2'} size={12} />{s.check.text}
              </b>
            </div>
          )}
        </div>
        {dismissing ? (
          <div className="spv-dismiss" role="group" aria-label="Dismiss reason">
            <span>Why dismiss it? It uses your answer to stop suggesting things like this.</span>
            <div className="spv-dismiss-row">
              {DISMISS_REASONS.map((r) => (
                <button key={r} type="button" className="filter-chip" onClick={() => { setDismissing(false); run('dismiss', () => onDismiss(r)) }}>{r}</button>
              ))}
              <button type="button" className="spv-cancel" onClick={() => setDismissing(false)}>Cancel</button>
            </div>
          </div>
        ) : applied ? (
          <div className="spv-actions">
            {applied.undoable !== false && (
              <button type="button" className="btn ghost" disabled={busy === 'undo'} onClick={() => run('undo', onUndo)}>
                {busy === 'undo' ? 'Undoing…' : 'Undo'}
              </button>
            )}
            <span className="spv-applied" role="status">
              {failed === 'undo'
                ? <span className="spv-failed" role="alert">Couldn't undo it; <code>{s.key}</code> is still {applied.to}</span>
                : <><Icon name="check" size={13} />Applied · <code>{s.key}</code> is now {applied.to}</>}
            </span>
          </div>
        ) : (
          <div className="spv-actions">
            {canWrite && (
              <button
                type="button"
                className="btn primary"
                disabled={applying || blocked}
                aria-describedby={blocked || failed === 'apply' ? `spv-why-not-${s.id}` : undefined}
                onClick={() => run('apply', onApply)}
              >
                {applying ? 'Applying…' : applyLabel(s)}
              </button>
            )}
            {canWrite && (
              <button type="button" className="btn ghost" disabled={applying} onClick={() => setDismissing(true)}>{busy === 'dismiss' ? 'Dismissing…' : 'Dismiss…'}</button>
            )}
            <button
              type="button"
              className={`spv-toggle${expanded ? ' open' : ''}`}
              aria-expanded={expanded}
              aria-controls={`spv-ev-${s.id}`}
              onClick={onToggle}
            >
              {expanded ? 'Hide evidence' : 'Show evidence'}<Icon name="chevD" size={13} />
            </button>
            {failed === 'dismiss' ? (
              <p className="spv-why-not failed" role="alert">Couldn't dismiss it; try again.</p>
            ) : canWrite && (blocked || failed === 'apply') && (
              <p className={`spv-why-not${failed === 'apply' ? ' failed' : ''}`} id={`spv-why-not-${s.id}`} role={failed === 'apply' ? 'alert' : undefined}>
                {failed === 'apply' ? "Couldn't apply it. The card shows the setting as it is now." : s.apply.reason}
              </p>
            )}
          </div>
        )}
      </div>
      {expanded && <Evidence s={s} openCase={openCase} />}
    </article>
  )
}

export function HeldCard({ h, onOpenIncident }: { h: HeldSuggestion; onOpenIncident: () => void }) {
  return (
    <article className="spv-card spv-held" aria-label={`Held: ${h.verb} ${h.key}`}>
      <div className="spv-held-text">
        <span className="spv-tags">
          <span className="spv-fn">{FN_LABEL[h.fn]}</span>
          <span className="spv-tag">Held</span>
        </span>
        <span>
          {h.verb} <code>{h.key}</code> {h.from} → {h.to} waits while this Medic incident is open:{' '}
          <span className="spv-strong">{h.incident.title}</span> · firing {since(h.incident.firing_since)}
        </span>
      </div>
      <button type="button" className="spv-link spv-link-strong" onClick={onOpenIncident}>View on Medic<Icon name="chevR" size={12} /></button>
    </article>
  )
}
