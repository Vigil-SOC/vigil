import { useSearchParams } from 'react-router-dom'
import { useAuth } from '../../contexts/AuthContext'
import { InfoTip } from '../../shared/InfoTip'
import { LevelBadge, type Level } from '../../shared/LevelBadge'
import { NotMeasured } from '../../shared/NotMeasured'
import { TabStrip } from '../../shared/TabStrip'
import { EmptyState } from '../../shared/ui'
import { IncidentsTab } from './IncidentsTab'
import { PackTab } from './PackTab'
import { SensorsTab } from './SensorsTab'
import type { StatusRead } from './medicTypes'
import { safeText } from './safeText'
import { useStatus } from './useMedic'
import { spanWord, stamp, uptimeWord } from './words'
import './medic.css'

// UX-6: the word and level of each state, worst first; Off has no level
const WORDS: Record<string, [string, Level]> = {
  running: ['Running', 'good'],
  degraded: ['Degraded', 'fair'],
  starting: ['Starting', 'fair'],
  unknown: ['Unknown', 'fair'],
  down: ['Down', 'poor'],
  blind: ['Blind', 'poor'],
  not_recording: ['Not recording', 'poor'],
  crash_looping: ['Crash-looping', 'poor'],
}

const TABS = [{ id: 'incidents', label: 'Incidents' }, { id: 'cant-see', label: "Can't see" }, { id: 'pack', label: 'Content pack' }] as const

const mb = (b: number) => (b >= 2 ** 30 ? `${(b / 2 ** 30).toFixed(1)} GiB` : `${Math.round(b / 2 ** 20)} MB`)

/** Medic's single worst-wins status: the backend's off, down and unknown win; otherwise Medic's own state. */
function statusWord(s: StatusRead): string {
  return s.state === 'running' ? s.medic?.state ?? 'unknown' : s.state
}

export default function MedicScreen() {
  const { hasPermission } = useAuth()
  const { status, error, reload } = useStatus()
  const [params, setParams] = useSearchParams()
  const incidentId = params.get('incident')
  // the tab is in the URL; any other ?tab= reads as Incidents
  const tab = TABS.find((t) => t.id === params.get('tab'))?.id ?? 'incidents'
  // an incident always opens in the Incidents tab, whichever tab linked to it
  const open = (id: string | null) => setParams(id ? { tab: 'incidents', incident: id } : { tab })

  if (!status) {
    return (
      <div className="mdc-page">
        {error
          ? <EmptyState error icon="alert" title="Couldn't load Medic's status" body={error === 'medic_off' ? undefined : 'Vigil could not read Medic’s status. Try again in a moment.'} primary={{ label: 'Retry', onClick: reload, icon: 'refresh' }} />
          : <EmptyState loading icon="clock" title="Loading Medic…" />}
      </div>
    )
  }

  const word = statusWord(status)
  const [label, level] = WORDS[word] ?? [safeText(word), 'fair' as Level]
  const m = status.medic
  const seen = status.last_seen_at ? Date.parse(status.last_seen_at) : null

  return (
    <div className="mdc-page">
      {m?.dev_mode && <div className="mdc-strip fair" role="status">Dev mode: unsigned content allowed.</div>}

      <section className="mdc-card mdc-status" aria-label="Medic status">
        <div className="mdc-status-head">
          {status.state === 'off' ? <span className="mdc-off">Off</span> : <LevelBadge level={level} variant="pill" label={label} />}
          {status.state === 'unknown' && <span className="mdc-quiet">Vigil can't tell (database unavailable)</span>}
          <InfoTip
            label="How Medic's status is derived"
            source="Vigil asks Medic for its status once a minute and keeps the answer."
            calculation="Running while Medic answers; Down after about 4½ minutes without an answer; Off when Medic is switched off; Unknown when Vigil can't read its own record. Otherwise Medic's own state, worst first."
            limit="Medic's own lines (uptime, store, sensors, pack) show only while it is Running."
            align="start"
          />
        </div>
        {seen !== null && (
          <p>Last seen {stamp(status.last_seen_at)} ({spanWord(Date.now() - seen)} ago){m && <> · heartbeat {stamp(m.heartbeat_at)}</>}</p>
        )}
        {m && (
          <>
            <p>
              Up {uptimeWord(m.uptime_s)} · {m.restarts_24h} restart{m.restarts_24h === 1 ? '' : 's'} in 24 h
              {m.last_exit && ` (exit: ${safeText(m.last_exit.reason)})`}
            </p>
            <p>Store: {m.store.states.map(safeText).join(', ')} · {mb(m.store.bytes_used)} of {mb(m.store.cap_bytes)}</p>
            <p>
              Sensors: {m.sensors.reporting} reporting
              {m.sensors.cant_see > 0 && <> · <button type="button" className="mdc-link mdc-cant" onClick={() => setParams({ tab: 'cant-see' })}>{m.sensors.cant_see} Medic can't see ›</button></>}
            </p>
            <p>
              {m.pack
                ? <button type="button" className="mdc-link" onClick={() => setParams({ tab: 'pack' })}>{`Pack ${safeText(m.pack.id)} ${safeText(m.pack.version)} ${safeText(m.pack.channel)} · ${m.pack.stale ? 'expired, still running' : `expires ${Math.max(0, Math.ceil((Date.parse(m.pack.expires_at) - Date.now()) / 864e5))} d`} ›`}</button>
                : <>Pack: <NotMeasured /></>}
            </p>
          </>
        )}
      </section>

      {status.state === 'off' ? (
        <EmptyState
          icon="info"
          title="Medic is off on this install."
          body={hasPermission('medic.admin') ? <>Set <code>VIGIL_MEDIC_ENABLED=true</code> to turn it on (see docs/medic/enable.md).</> : undefined}
        />
      ) : (
        <>
          <div className="mdc-tabs">
            <TabStrip label="Medic sections" tabs={TABS} active={tab} onChange={(id) => setParams({ tab: id })} />
          </div>
          {tab === 'cant-see' ? <SensorsTab status={status} open={open} />
            : tab === 'pack' ? <PackTab running={status.state === 'running'} reloadStatus={reload} />
            : <IncidentsTab status={status} incidentId={incidentId} open={open} />}
        </>
      )}
    </div>
  )
}
