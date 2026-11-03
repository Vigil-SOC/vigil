import { useEffect, useMemo, useState } from 'react'
import { DataTable, type ColumnDef } from '../../shared/DataTable'
import { InfoTip } from '../../shared/InfoTip'
import { NotMeasured } from '../../shared/NotMeasured'
import { EmptyState } from '../../shared/ui'
import type { Sensor, StatusRead } from './medicTypes'
import { safeText, safeTitle } from './safeText'
import { useSensors } from './useMedic'
import { spanWord, stamp } from './words'
import './medic.css'

// UX-24: the state and reason enums; anything else (and `unknown`) reads "Unknown", never a value that could pass for healthy
const STATE: Record<string, [string, 'idle' | 'poor' | 'plain']> = {
  reporting: ['Reporting', 'plain'],
  cant_see: ["Medic can't see", 'poor'],
  off: ['Off', 'idle'],
}
const REASON: Record<string, string> = {
  auth_401: 'Sign-in refused (401)',
  auth_403: 'Access denied (403)',
  unreachable: 'Unreachable',
  timeout: 'Timed out',
  http_5xx: 'Server error (5xx)',
  parse_error: "Couldn't read the reply",
  not_found: 'Not found',
  disabled_by_config: 'Switched off in config',
  redis_fault: 'Redis fault',
}

const Unknown = () => <span className="not-measured">Unknown</span>
const own = (o: Record<string, unknown>, k: unknown) => (typeof k === 'string' && Object.prototype.hasOwnProperty.call(o, k) ? k : null)
const time = (v: unknown, ref: string) => (typeof v === 'string' ? stamp(v, ref) : '—')
/** Unreadable sensors first, then Off, then Reporting; the order read within each. */
const rank = (s: Sensor) => (s.state === 'reporting' ? 2 : s.state === 'off' ? 1 : 0)

export function SensorsTab({ status, open }: { status: StatusRead; open: (id: string) => void }) {
  const running = status.state === 'running'
  const { list, phase, failedSince, lastGood, reload } = useSensors(running)
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 15_000)
    return () => window.clearInterval(t)
  }, [])

  const items = useMemo(() => (list ? [...list.items].sort((a, b) => rank(a) - rank(b)) : []), [list])
  const asOf = typeof list?.as_of === 'string' ? list.as_of : ''

  // Medic is not answering and no list was ever read: nothing to keep
  if (!running && !list) {
    return (
      <div className="mdc-body">
        <EmptyState
          error icon="alert"
          title={status.state === 'down' ? 'Medic is down' : "Vigil can't tell if Medic is running"}
          body="There is no earlier list of sensors to show. This page reads it again once Medic answers."
        />
      </div>
    )
  }

  const cols: ColumnDef<Sensor>[] = [
    { key: 'id', label: 'Sensor', render: (s) => <span className="mdc-mono mdc-wrap" title={safeTitle(s.id)}>{safeText(s.id)}</span> },
    {
      key: 'state', label: 'State',
      render: (s) => {
        const k = own(STATE, s.state)
        if (!k) return <Unknown />
        const [word, tone] = STATE[k]
        return <span className={`mdc-state ${tone}`}>{word}</span>
      },
    },
    { key: 'since', label: 'Since', render: (s) => <span className="mdc-mono" title={safeTitle(s.since)}>{time(s.since, asOf)}</span> },
    { key: 'good', label: 'Last good reading', render: (s) => <span className="mdc-mono" title={safeTitle(s.last_good_at)}>{time(s.last_good_at, asOf)}</span> },
    {
      key: 'reason', label: 'Reason',
      render: (s) => {
        const k = own(REASON, s.reason)
        if (k) return <span className="mdc-wrap">{REASON[k]}</span>
        // no reason is normal while reporting; for any other state it is not known
        return s.state === 'reporting' && s.reason == null ? <span className="mdc-quiet">—</span> : <Unknown />
      },
    },
    {
      key: 'dropped', label: 'Dropped lines, last hour',
      render: (s) => (own(STATE, s.state) && Number.isFinite(s.dropped_last_hour) && (s.dropped_last_hour as number) >= 0
        ? <span className="mdc-mono">{s.dropped_last_hour}</span>
        : <NotMeasured />),
    },
    {
      key: 'incident', label: 'Watcher incident',
      render: (s) => (typeof s.watcher_incident_id === 'string' && s.watcher_incident_id
        ? <button type="button" className="mdc-link mdc-wrap" title={safeTitle(s.watcher_incident_id)} onClick={(e) => { e.stopPropagation(); open(s.watcher_incident_id!) }}>{safeText(s.watcher_incident_id)}</button>
        : <span className="mdc-quiet">—</span>),
    },
  ]

  const cantSee = items.filter((s) => s.state === 'cant_see').length
  const unknown = items.filter((s) => !own(STATE, s.state)).length
  const watching = status.medic?.sensors
  const emptyBody = watching && watching.cant_see > 0 ? `Medic says it can't see ${watching.cant_see} sensor${watching.cant_see === 1 ? '' : 's'}, but none were listed.` : undefined
  const staleMin = failedSince && lastGood ? spanWord(now - lastGood) : null

  return (
    <div className="mdc-body">
      {!running && (
        <div className="mdc-strip poor" role="status">
          {status.state === 'down' ? 'Medic is down' : "Vigil can't tell if Medic is running"}: showing data up to {stamp(asOf)}
        </div>
      )}
      <div className="mdc-bar">
        <span className="mdc-rate">
          {asOf && <>As of {stamp(asOf)} · </>}{items.length} sensor{items.length === 1 ? '' : 's'}
          {cantSee > 0 && <> · <span className="mdc-cant">{cantSee} Medic can't see</span></>}
          {unknown > 0 && <> · {unknown} unknown</>}
          <InfoTip
            label="About the sensors list"
            source="Medic's own list of the places it reads from, as of the time shown."
            calculation="One row per sensor. Medic can't see a sensor when it cannot read it, for the reason shown."
            limit="Dropped lines are not recorded yet, so they show as not measured. Not measured is never zero."
          />
        </span>
      </div>
      {staleMin && running && <p className="mdc-stale" role="status">Couldn't refresh. Last updated {staleMin} ago.</p>}
      <div className="mdc-card">
        <DataTable
          className="tbl mdc-sens"
          columns={cols}
          rows={items}
          rowKey={(s) => safeText(s.id)}
          phase={phase}
          error="Couldn't load sensors."
          onRetry={reload}
          loadingMessage="Loading sensors…"
          emptyMessage={<EmptyState table compact icon="info" title="Medic lists no sensors." body={emptyBody} />}
        />
      </div>
    </div>
  )
}
