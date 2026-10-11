import { useState, type KeyboardEvent } from 'react'
import { Link } from 'react-router-dom'
import { Icon, type IconName } from '../../shared/icons'
import { InfoTip } from '../../shared/InfoTip'
import { clock } from './clock'
import { useSourceBadge } from '../../shared/useSourceBadge'
import type { OverviewFeedItem, OverviewPayload } from '../../services/api'

const CONNECT_DATA = '/settings?section=data'

const TABS = [
  { key: 'all', label: 'All' },
  { key: 'crit', label: 'Crit' },
  { key: 'high', label: 'High' },
  { key: 'med', label: 'Med' },
  { key: 'low', label: 'Low' },
] as const

const SEVERITY = { crit: 'Critical', high: 'High', med: 'Medium', low: 'Low', none: '' } as const
type Sev = keyof typeof SEVERITY

// State pill colour by terminal state; anything else is neutral.
const PILL: Record<string, string> = {
  resolved_auto: 'good',
  resolved_person: 'good',
  needs_you: 'poor',
  working: 'ac',
}

/** The severity key a row filters and paints by; anything unrecognised is "none". */
function sevKey(severity: string | null): Sev {
  const s = (severity ?? '').toLowerCase()
  if (s.startsWith('crit')) return 'crit'
  if (s.startsWith('high')) return 'high'
  if (s.startsWith('med')) return 'med'
  if (s.startsWith('low')) return 'low'
  return 'none'
}

/** The severity square and word, in the rail and the popup header. */
export function SevMark({ severity }: { severity: string | null }) {
  const key = sevKey(severity)
  return (
    <span className="ov-sev">
      <i style={{ background: `var(--sev-${key})` }} />
      {SEVERITY[key] || severity || 'Unknown'}
    </span>
  )
}

function AgentMark({ item }: { item: OverviewFeedItem }) {
  if (item.terminal_state === 'working') {
    const title = item.case_id ? `An agent is already working on it in case ${item.case_id}` : 'An agent is already working on it'
    return <span className="ov-mark-a" title={title}><Icon name="bot" size={12} /></span>
  }
  if (item.terminal_state === 'resolved_auto') {
    return <span className="ov-mark-a good" title="Resolved automatically"><Icon name="check" size={12} /></span>
  }
  return null
}

function Row({ item, onOpen, openCase }: { item: OverviewFeedItem; onOpen: (id: string) => void; openCase: (id: string) => void }) {
  const badge = useSourceBadge()(item.data_source)
  const onKey = (e: KeyboardEvent) => {
    if (e.target !== e.currentTarget || (e.key !== 'Enter' && e.key !== ' ')) return
    e.preventDefault()
    onOpen(item.finding_id)
  }
  const text = item.title || item.description || item.finding_id
  const full = item.title && item.description ? item.description : text
  return (
    <div role="button" tabIndex={0} className="ov-row" onClick={() => onOpen(item.finding_id)} onKeyDown={onKey}>
      <div className="ov-row-1">
        <span className="ov-st" title={badge.label}><Icon name={badge.icon as IconName} size={11} /></span>
        <SevMark severity={item.severity} />
        <span className="ov-row-r">
          <AgentMark item={item} />
          <time className="ov-time" title={item.created_at ?? undefined}>{clock(item.created_at)}</time>
        </span>
      </div>
      <div className="ov-row-t" title={full}>{text}</div>
      <div className="ov-row-3">
        <b>{badge.label}</b>
        <span>·</span>
        <span className="ov-fid" title={item.finding_id}>{item.finding_id}</span>
        {item.case_id ? (
          <button
            type="button"
            className="ov-pill ac"
            onClick={(e) => {
              e.stopPropagation() // the row opens the alert
              openCase(item.case_id!)
            }}
          >
            Case {item.case_id}
          </button>
        ) : (
          <span className={`ov-pill ${PILL[item.terminal_state] ?? ''}`}>{item.terminal_label}</span>
        )}
      </div>
    </div>
  )
}

interface Props {
  data: OverviewPayload
  paused: boolean
  onTogglePause: () => void
  onOpen: (id: string) => void
  openCase: (id: string) => void
}

/** "Incoming alerts": the live feed, newest first, as a right rail. */
export default function AlertRail({ data, paused, onTogglePause, onOpen, openCase }: Props) {
  const [tab, setTab] = useState<(typeof TABS)[number]['key']>('all')
  const rows = tab === 'all' ? data.feed : data.feed.filter((r) => sevKey(r.severity) === tab)
  const today = data.arrivals.reduce((n, a) => n + a.count, 0)
  const empty = data.empty && data.feed.length === 0 // rows win over the empty panel
  const tabLabel = TABS.find((t) => t.key === tab)!.label
  return (
    <aside className="ov-rail" aria-label="Incoming alerts">
      <div className="ov-rail-h">
        <div className="ov-rail-t">
          <h2>Incoming alerts</h2>
          {!empty && (
            <span className={`ov-live${paused ? ' paused' : ''}`}>
              <i />
              {paused ? 'Paused' : 'Live'}
            </span>
          )}
          <span className="ov-rail-ctl">
            <InfoTip
              label="About incoming alerts"
              source="Findings as they arrive from pollers, webhooks and uploads"
              calculation="Newest first. Status is what triage did with the alert"
              limit={`Shows the latest ${data.feed_limit}`}
            />
            <button
              type="button"
              className="ov-pause"
              aria-label={paused ? 'Resume the live feed' : 'Pause the live feed'}
              title={paused ? 'Resume the live feed' : 'Pause the live feed'}
              onClick={onTogglePause}
            >
              <Icon name={paused ? 'play' : 'pause'} size={13} />
            </button>
          </span>
        </div>
        <p>{empty ? 'Alerts show here as they arrive, newest first' : `${today} today · newest first · status is what triage did`}</p>
        {!empty && (
          <div className="ov-tabs" role="tablist" aria-label="Severity">
            {TABS.map((t) => (
              <button key={t.key} type="button" role="tab" aria-selected={tab === t.key} onClick={() => setTab(t.key)}>
                {t.label}
              </button>
            ))}
          </div>
        )}
      </div>
      <div className="ov-rail-b">
        {empty ? (
          <div className="ov-rail-empty">
            <div className="ov-empty-panel">
              <span className="ov-tile accent"><Icon name="alert" size={18} /></span>
              <b>No alerts yet</b>
              <span>Connect a SIEM, an EDR or the LogLM pipeline and alerts start arriving within minutes.</span>
              <Link className="btn primary no-underline" to={CONNECT_DATA}>Connect data</Link>
            </div>
          </div>
        ) : rows.length === 0 ? (
          <p className="ov-rail-none">{tab === 'all' ? 'No alerts.' : `No ${tabLabel} alerts.`}</p>
        ) : (
          rows.map((item) => <Row key={item.finding_id} item={item} onOpen={onOpen} openCase={openCase} />)
        )}
      </div>
      {!empty && (
        <div className="ov-rail-f">
          <span>Click an alert to send it to triage or open its case</span>
          <Link to="/triage">Triage queue →</Link>
        </div>
      )}
    </aside>
  )
}
