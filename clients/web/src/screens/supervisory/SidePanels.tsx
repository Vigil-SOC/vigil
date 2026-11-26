import { useState } from 'react'
import { Icon } from '../../shared/icons'
import { NotMeasured } from '../../shared/NotMeasured'
import type { CurrentSetting, FailureReason, HistoryEntry, LearnsFrom } from './supervisoryTypes'
import { ago } from './words'

const HISTORY_SHOWN = 3
const SHORT_DAY = new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', month: 'short', day: 'numeric' })

/** `failures` null: not measured yet. */
export function FailuresPanel({ failures }: { failures: { days: number; items: FailureReason[] } | null }) {
  const items = failures?.items ?? []
  const top = Math.max(1, ...items.map((i) => i.count))
  return (
    <section className="spv-card spv-side" aria-label="Why runs failed">
      <h3>Why runs failed {failures && <span className="spv-quiet">· {failures.days} days</span>}</h3>
      {!failures && <NotMeasured align="end" tip="Why runs ended without an answer isn't counted on this install yet." />}
      {failures && items.length === 0 && <p className="spv-quiet">No failed runs.</p>}
      {items.map((f, i) => (
        <div key={f.reason} className="spv-fail">
          <div className="spv-row"><span>{f.reason}</span><span className="spv-mono spv-strong">{f.count}</span></div>
          <div className="spv-track"><div className={i === 0 ? 'spv-fill top' : 'spv-fill'} style={{ width: `${(f.count / top) * 100}%` }} /></div>
          {f.note && <span className="spv-note">{f.note}</span>}
        </div>
      ))}
    </section>
  )
}

function outcomeLine(h: HistoryEntry): { text: string; tone: string } {
  if (h.outcome === 'dismissed') return { text: `Dismissed · "${h.note}"`, tone: 'muted' }
  if (h.outcome === 'undone') return { text: `Undone · ${ago(h.at)}`, tone: 'muted' }
  if (!h.note) return { text: `Applied · ${ago(h.at)}`, tone: 'good' }
  return { text: `Applied · ${h.note}`, tone: h.improved ? 'good' : h.improved === false ? 'poor' : 'muted' }
}

function UndoButton({ entry, onUndo }: { entry: HistoryEntry; onUndo: (h: HistoryEntry) => Promise<void> }) {
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState(false)
  const run = () => {
    setBusy(true)
    setFailed(false)
    onUndo(entry).catch(() => { setBusy(false); setFailed(true) })
  }
  return (
    <>
      {' · '}
      <button type="button" className="spv-link" disabled={busy} onClick={run}>{busy ? 'Undoing…' : 'Undo'}</button>
      {failed && <span className="spv-outcome poor" role="alert"> · couldn't undo</span>}
    </>
  )
}

export function HistoryPanel({ items, canUndo, onUndo }: { items: HistoryEntry[]; canUndo: boolean; onUndo: (h: HistoryEntry) => Promise<void> }) {
  const [all, setAll] = useState(false)
  const shown = all ? items : items.slice(0, HISTORY_SHOWN)
  return (
    <section className="spv-card spv-side" aria-label="History">
      <h3>History</h3>
      {items.length === 0 && <p className="spv-quiet">Nothing applied or dismissed yet.</p>}
      {shown.map((h) => {
        const o = outcomeLine(h)
        return (
          <div key={h.id} className="spv-hist">
            <span><span className="spv-quiet" title={h.at}>{SHORT_DAY.format(new Date(h.at))}</span> · <code>{h.key} {h.from} → {h.to}</code></span>
            <span className={`spv-outcome ${o.tone}`}>
              {o.text}
              {canUndo && h.undoable && <UndoButton entry={h} onUndo={onUndo} />}
            </span>
          </div>
        )
      })}
      {items.length > HISTORY_SHOWN && (
        <button type="button" className="spv-link spv-link-strong" aria-expanded={all} onClick={() => setAll(!all)}>
          {all ? 'Show less' : 'All history'}<Icon name={all ? 'chevD' : 'chevR'} size={12} />
        </button>
      )}
    </section>
  )
}

/** `items` null: not measured yet. */
export function SettingsPanel({ items }: { items: CurrentSetting[] | null }) {
  return (
    <section className="spv-card spv-side" aria-label="Current settings">
      <h3>Current settings</h3>
      {!items && <NotMeasured align="end" tip="The settings it watches, and where each value comes from, aren't reported here yet." />}
      {items?.length === 0 && <p className="spv-quiet">No settings it watches are set on this install.</p>}
      {items?.map((s) => (
        <div key={s.key} className="spv-row spv-setting">
          <code title={s.key}>{s.key}</code>
          <span>{s.value} <span className="spv-quiet">{s.source}</span></span>
        </div>
      ))}
    </section>
  )
}

export function LearnsPanel({ days, items }: { days: number; items: LearnsFrom[] }) {
  return (
    <section className="spv-card spv-side" aria-label="What it learns from">
      <h3>What it learns from <span className="spv-quiet">· {days} days</span></h3>
      {items.map((l) => (
        <div key={l.label} className="spv-row"><span>{l.label}</span><span className="spv-mono spv-strong">{l.count}</span></div>
      ))}
    </section>
  )
}
