import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { detectionRulesApi, type HealthEntry, type HealthInbox } from '../services/api'
import { Icon } from '../shared/icons'
import { errorText } from '../shared/errorText'
import { relativeTime } from '../screens/settings/integrationHealth'
import type { ConsoleScreenGoOptions } from '../shared/types'

/** Each check's flag as a person reads it, and its tone on the Rules page. */
const KIND: Record<string, { label: string; plural: string; tone: string }> = {
  runs: { label: 'Stopped running', plural: 'stopped running', tone: 'poor' },
  sees: { label: 'Lost its data', plural: 'lost their data', tone: 'fair' },
  quiet: { label: 'Too loud', plural: 'too loud', tone: 'high' },
  accurate: { label: 'Mostly false positives', plural: 'mostly false positives', tone: 'fair' },
  noisy: { label: 'Went noisy', plural: 'went noisy', tone: 'high' },
  silent: { label: 'Went silent', plural: 'went silent', tone: 'ac' },
  drift: { label: 'Changed outside the repo', plural: 'changed outside the repo', tone: 'ac' },
}
const kindOf = (check: string) => KIND[check] ?? { label: `${check} failing`, plural: `${check} failing`, tone: 'poor' }
const SIEM_NAME: Record<string, string> = { elastic: 'Elastic', splunk: 'Splunk', wazuh: 'Wazuh', overlay: 'Vigil', repo: 'Repo' }
const POLL_MS = 60_000

/** The top bar's bell: rule-health flags for the signed-in person, read or unread. In-app only. */
export default function RuleHealthBell({ go }: { go: (screen: string, options?: ConsoleScreenGoOptions) => void }) {
  const [box, setBox] = useState<HealthInbox | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
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
    if (!open) return
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
  const listed = (box?.entries ?? []).filter((e) => !summary || e.read)
  const breakdown = summary
    ? Object.entries(summary.by_check)
      .sort((a, b) => b[1] - a[1])
      .map(([check, count]) => `${count.toLocaleString()} ${kindOf(check).plural}`)
      .join(' · ')
    : ''
  const label = unread ? `Rule health, ${unread} unread` : 'Rule health'

  const root = ref.current?.closest('.soc-console') as HTMLElement | null
  const pop = open && pos ? (
    <div
      ref={popRef}
      className="rh-pop"
      role="dialog"
      aria-label="Rule health"
      style={{ position: 'fixed', top: pos.top, right: pos.right, zIndex: 80 }}
    >
      <div className="rh-head">
        <span className="rh-title">Rule health{unread ? <span className="rh-count"> · {unread.toLocaleString()} unread</span> : null}</span>
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
        {listed.map((e) => {
          const k = kindOf(e.check)
          return (
            <li key={e.change_id} className={`rh-item${e.read ? '' : ' unread'}`}>
              <span className={`rh-dot ${k.tone}`} aria-hidden="true" />
              <div className="rh-body">
                <div className={`rh-kind ${k.tone}`}>{k.label}{e.read ? '' : <span className="rh-sr"> (unread)</span>}</div>
                <button type="button" className="rh-rule" title={e.name} onClick={() => openEntry(e)}>{e.name}</button>
                <div className="rh-meta">
                  <span className="rh-siem">{SIEM_NAME[e.tool] ?? e.tool}</span>
                  <span>{relativeTime(e.since)}</span>
                  {!e.read && (
                    <button type="button" className="rh-link" disabled={busy} onClick={() => markRead([e.change_id])}>Mark read</button>
                  )}
                </div>
              </div>
            </li>
          )
        })}
      </ul>
      {box && box.total > box.entries.length && (
        <p className="rh-foot">Showing the newest {box.entries.length} of {box.total.toLocaleString()}.</p>
      )}
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
