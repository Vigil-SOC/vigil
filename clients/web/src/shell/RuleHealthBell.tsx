import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { useAuth } from '../contexts/AuthContext'
import { createPortal } from 'react-dom'
import { detectionRulesApi, type HealthEntry, type HealthInbox } from '../services/api'
import { Icon } from '../shared/icons'
import { errorText } from '../shared/errorText'
import { relativeTime } from '../screens/settings/integrationHealth'
import type { ConsoleScreenGoOptions } from '../shared/types'

/** Each check's flag as a person reads it, and its tone on the Rules page. */
/** Each check's flag as a person reads it, a one-line why, and its tone on the Rules page. */
const KIND: Record<string, { label: string; plural: string; why: string; tone: string }> = {
  runs: { label: 'Stopped running', plural: 'stopped running', why: 'Its query errors in the SIEM.', tone: 'poor' },
  sees: { label: 'Lost its data', plural: 'lost their data', why: 'The data it searches stopped arriving.', tone: 'fair' },
  quiet: { label: 'Too loud', plural: 'too loud', why: 'Over its alerts-per-day limit.', tone: 'high' },
  accurate: { label: 'Mostly false positives', plural: 'mostly false positives', why: 'Most of its judged alerts were not real.', tone: 'fair' },
  noisy: { label: 'Went noisy', plural: 'went noisy', why: 'Far more alerts today than its usual.', tone: 'high' },
  silent: { label: 'Went silent', plural: 'went silent', why: 'It fired regularly, then stopped.', tone: 'ac' },
  drift: { label: 'Changed outside the repo', plural: 'changed outside the repo', why: 'Edited, disabled or removed in the SIEM, not in its repo.', tone: 'ac' },
  revert: { label: 'Moved back to shadow', plural: 'moved back to shadow', why: 'Vigil moved it from live back to shadow. Analysts no longer get its alerts.', tone: 'fair' },
}
const kindOf = (check: string) => KIND[check] ?? { label: `${check} failing`, plural: `${check} failing`, why: 'A check that passed now fails.', tone: 'poor' }
const SIEM_NAME: Record<string, string> = { elastic: 'Elastic', splunk: 'Splunk', wazuh: 'Wazuh', overlay: 'Vigil', repo: 'Repo' }
const POLL_MS = 60_000
/** The reason sync writes when a live rule's new version goes back to shadow. */
const NEW_VERSION = 'new version'

/** The top bar's bell: notifications for the signed-in person, read or unread; only rule health for now. In-app only. */
export default function RuleHealthBell({ go }: { go: (screen: string, options?: ConsoleScreenGoOptions) => void }) {
  const [box, setBox] = useState<HealthInbox | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [open, setOpen] = useState(false)
  // Read entries sit under a closed "Earlier" row; it closes again with the panel.
  const [earlierOpen, setEarlierOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const canWrite = useAuth().hasPermission('detections.write')
  // A Vigil rule's Revert to shadow, asked in place: the entry asking why, and the ones reverted from here.
  const [asking, setAsking] = useState<number | null>(null)
  const [reverted, setReverted] = useState<Set<number>>(new Set())
  const [pos, setPos] = useState<{ top: number; right: number } | null>(null)
  const ref = useRef<HTMLDivElement>(null)
  const btnRef = useRef<HTMLButtonElement>(null)
  const popRef = useRef<HTMLDivElement>(null)

  const load = useCallback(() => {
    detectionRulesApi.healthInbox()
      .then((res) => { setBox(res.data); setError(null) })
      .catch((e) => setError(errorText(e, 'Rule health did not load.')))
  }, [])

  useEffect(() => {
    load()
    const t = window.setInterval(load, POLL_MS)
    return () => window.clearInterval(t)
  }, [load])

  const place = useCallback(() => {
    const r = ref.current?.getBoundingClientRect()
    if (r) setPos({ top: r.bottom + 6, right: Math.max(8, window.innerWidth - r.right) })
  }, [])

  useEffect(() => {
    if (!open) { setEarlierOpen(false); return }
    place()
    load()
    const onDoc = (e: MouseEvent) => {
      const t = e.target as Node
      if (ref.current?.contains(t) || popRef.current?.contains(t)) return
      setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      setOpen(false)
      btnRef.current?.focus()
    }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    window.addEventListener('resize', place)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
      window.removeEventListener('resize', place)
    }
  }, [open, place, load])

  const markRead = (ids: number[] | 'all') => {
    setBusy(true)
    detectionRulesApi.markHealthRead(ids)
      .then((res) => { setBox(res.data); setError(null) })
      .catch((e) => setError(errorText(e, 'Could not mark it read.')))
      .finally(() => setBusy(false))
  }

  const openRules = (ruleIds: number[]) => {
    setOpen(false)
    go('detections', { search: `?rules=${ruleIds.join(',')}` })
  }

  const openEntry = (entry: HealthEntry) => {
    if (!entry.read) markRead([entry.change_id])
    openRules([entry.rule_id])
  }

  const unread = box?.unread ?? 0
  const summary = box?.summary ?? null
  // With a summary, the unread entries fold into it; read ones still list.
  // The server lists unread entries only (none when the summary stands for them).
  const listed = box?.entries ?? []
  const earlier = box?.earlier ?? []
  const earlierTotal = box?.earlier_total ?? 0
  const breakdown = summary
    ? Object.entries(summary.by_check)
      .sort((a, b) => b[1] - a[1])
      .map(([check, count]) => `${count.toLocaleString()} ${kindOf(check).plural}`)
      .join(' · ')
    : ''
  const label = unread ? `Notifications, ${unread} unread` : 'Notifications'

  const entryRow = (e: HealthEntry) => {
    const k = kindOf(e.check)
    const done = reverted.has(e.change_id)
    // Vigil's own rules only; the server refuses one that isn't live, and says so.
    const canRevert = canWrite && e.tool === 'overlay' && e.check !== 'revert' && !done
    const why = done
      ? 'You moved it back to shadow just now. Analysts no longer get its alerts.'
      : e.check === 'revert' && e.reason === NEW_VERSION
        // A new version starts its own week in shadow: not a Revert of the rule.
        ? 'New version · back in shadow for its week. Analysts get its alerts again once it goes live.'
        : e.check === 'revert' && e.reason
          ? `Vigil moved it back to shadow: ${e.reason}. Analysts no longer get its alerts.`
          : k.why
    return (
      <li key={e.change_id} className={`rh-item${e.read ? '' : ' unread'}`}>
        <span className={`rh-dot ${k.tone}`} aria-hidden="true" />
        <div className="rh-body">
          <div className={`rh-kind ${k.tone}`}>{k.label}{e.read ? '' : <span className="rh-sr"> (unread)</span>}</div>
          <button type="button" className="rh-rule" title={e.name} onClick={() => openEntry(e)}>{e.name}</button>
          <div className="rh-why">{why}</div>
          <div className="rh-meta">
            <span className="rh-siem">{SIEM_NAME[e.tool] ?? e.tool}</span>
            <span>{relativeTime(e.since)}</span>
            {canRevert && asking !== e.change_id && (
              <button type="button" className="rh-link" onClick={() => setAsking(e.change_id)}>Revert to shadow</button>
            )}
            {!e.read && (
              <button type="button" className="rh-link" disabled={busy} onClick={() => markRead([e.change_id])}>Mark read</button>
            )}
          </div>
          {canRevert && asking === e.change_id && (
            <RevertAsk ruleId={e.rule_id} onDone={(ok) => { setAsking(null); if (ok) setReverted((r) => new Set(r).add(e.change_id)) }} />
          )}
        </div>
      </li>
    )
  }

  const root = ref.current?.closest('.soc-console') as HTMLElement | null
  const pop = open && pos ? (
    <div
      ref={popRef}
      className="rh-pop"
      role="dialog"
      aria-label="Notifications"
      style={{ position: 'fixed', top: pos.top, right: pos.right, zIndex: 80 }}
    >
      <div className="rh-head">
        <span className="rh-titles">
          <span className="rh-title">Notifications{unread ? <span className="rh-count"> · {unread.toLocaleString()} unread</span> : null}</span>
          <span className="rh-sub">Only rule health for now</span>
        </span>
        <button type="button" className="rh-link" disabled={!unread || busy} onClick={() => markRead('all')}>Mark all read</button>
      </div>
      {error && <p className="rh-error" role="alert">{error}</p>}
      {!box && !error && <p className="rh-empty">Reading rule health…</p>}
      {box && box.total === 0 && (
        <p className="rh-empty">No rule needs attention. A live rule that stops running, loses its data, goes noisy or silent, or changes outside its repo shows here.</p>
      )}
      <ul className="rh-list">
        {summary && (
          <li className="rh-item unread rh-summary">
            <span className="rh-dot poor" aria-hidden="true" />
            <div className="rh-body">
              <div className="rh-kind">{summary.rules.toLocaleString()} rules need attention</div>
              <div className="rh-break">{breakdown}</div>
              <div className="rh-meta">Newest {relativeTime(summary.since)}</div>
              <div className="rh-acts">
                <button type="button" className="rh-link strong" onClick={() => openRules(summary.rule_ids)}>
                  {summary.rule_ids.length < summary.rules ? `Open the newest ${summary.rule_ids.length} on Rules` : 'Open them on Rules'}
                </button>
                <button type="button" className="rh-link" disabled={busy} onClick={() => markRead('all')}>Mark all read</button>
              </div>
            </div>
          </li>
        )}
        {listed.map((e) => entryRow(e))}
      </ul>
      {box && box.total > 0 && !unread && <p className="rh-empty">Nothing unread.</p>}
      {earlierTotal > 0 && (
        <div className="rh-earlier">
          <button type="button" className="rh-earlier-toggle" aria-expanded={earlierOpen} onClick={() => setEarlierOpen((o) => !o)}>
            <svg viewBox="0 0 24 24" aria-hidden="true" className={earlierOpen ? 'open' : ''}><path d="m9 6 6 6-6 6" /></svg>
            Earlier ({earlierTotal.toLocaleString()})
          </button>
          {earlierOpen && (
            <ul className="rh-list">
              {earlier.map((e) => entryRow(e))}
              {earlierTotal > earlier.length && <li className="rh-more">and {(earlierTotal - earlier.length).toLocaleString()} more, on Rules</li>}
            </ul>
          )}
        </div>
      )}
      <div className="rh-foot">
        <button type="button" className="rh-link strong" onClick={() => { setOpen(false); go('detections') }}>All rules needing attention</button>
      </div>
    </div>
  ) : null

  return (
    <div className="rh-bell" ref={ref}>
      <button
        ref={btnRef}
        type="button"
        className={`vg-bell${open ? ' active' : ''}`}
        aria-label={label}
        title={label}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <Icon name="bell" size={18} />
        {unread > 0 && <span className="vg-bell-badge" aria-hidden="true">{unread > 99 ? '99+' : unread}</span>}
      </button>
      {pop && (root ? createPortal(pop, root) : pop)}
    </div>
  )
}

/** Why a person moves a live Vigil rule back to shadow, asked in the notification itself. */
function RevertAsk({ ruleId, onDone }: { ruleId: number; onDone: (ok: boolean) => void }) {
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (!reason.trim()) { setError('Say why.'); return }
    setBusy(true)
    detectionRulesApi.revertRule(ruleId, reason.trim())
      .then(() => onDone(true))
      .catch((err) => { setError(errorText(err, 'Could not revert it.')); setBusy(false) })
  }
  return (
    <form className="rh-ask" onSubmit={submit}>
      <input aria-label="Why move it back to shadow" placeholder="Why (required)" value={reason} onChange={(e) => setReason(e.target.value)} autoFocus />
      <button type="submit" className="rh-ask-go" disabled={busy}>{busy ? 'Reverting…' : 'Revert'}</button>
      <button type="button" className="rh-link" onClick={() => onDone(false)}>Cancel</button>
      {error && <p className="rh-error" role="alert">{error}</p>}
    </form>
  )
}
