import { useEffect, useState } from 'react'
import { InfoTip } from '../../shared/InfoTip'
import { LevelBadge } from '../../shared/LevelBadge'
import { EmptyState } from '../../shared/ui'
import { Icon } from '../../shared/icons'
import type { ConsoleScreenProps } from '../../shared/types'
import { monitorApi, type FleetWallPayload, type WallCell } from '../../services/api'
import { MetricTip } from './parts'
import { count, hhmm, minutes, pct, usePoll } from './format'
import './monitor.css'

const FACET_KEY = 'vigil.fleetWall.facet'
const STATE_WORD: Record<WallCell['state'], string> = {
  needs_you: 'Needs you',
  running: 'Running',
  paused: 'Paused',
  waiting: 'Waiting for a run',
  stuck: 'Stuck',
  on_hold: 'On hold',
}

function readFacet(): string {
  try {
    return sessionStorage.getItem(FACET_KEY) ?? 'all'
  } catch {
    return 'all'
  }
}

function matches(cell: WallCell, facet: string): boolean {
  switch (facet) {
    case 'all':
      return true
    case 'near_limit':
      return cell.near_limit
    case 'combined':
      return cell.combined
    case 'single':
      return !cell.combined
    default:
      return cell.state === facet
  }
}

function slaText(seconds: number | null): string {
  if (seconds == null) return 'No SLA'
  if (seconds < 0) return `Breached ${minutes(-seconds / 60)} ago`
  return `${minutes(seconds / 60)} left`
}

function hoverText(cell: WallCell): string {
  const used = cell.limit_used == null ? 'not priced' : `${pct(cell.limit_used)} of its limit used`
  return `${cell.title} · ${STATE_WORD[cell.state]} · ${used}`
}

export default function FleetWallScreen({ go, openCase, setViewFull }: ConsoleScreenProps) {
  const { data, phase, reload } = usePoll<FleetWallPayload>(monitorApi.wall, 10_000)
  const [facet, setFacet] = useState<string>(readFacet)
  const [selected, setSelected] = useState<string | null>(null)
  const [hovered, setHovered] = useState<string | null>(null)

  // View-full: the head, strip and filters stay on top and the bands scroll in the rest.
  useEffect(() => {
    setViewFull(true)
  }, [setViewFull])

  useEffect(() => {
    try {
      sessionStorage.setItem(FACET_KEY, facet)
    } catch {
      // A browser that refuses storage keeps the facet for this view only.
    }
  }, [facet])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setSelected(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  if (!data) {
    return (
      <div className="mn-screen">
        {phase === 'error' ? (
          <EmptyState error icon="alert" title="Couldn’t load the Fleet Wall" body="The read failed; it is tried again every 10 s." primary={{ label: 'Retry', onClick: reload, icon: 'refresh' }} />
        ) : (
          <EmptyState loading icon="wall" title="Loading the Fleet Wall…" />
        )}
      </div>
    )
  }

  const cells = data.bands.flatMap((band) => (band.cells ?? []).map((cell) => ({ cell, band: band.name })))
  const chosen = cells.find((entry) => entry.cell.case_id === selected) ?? null
  const pointed = cells.find((entry) => entry.cell.case_id === hovered) ?? null
  const slots = data.slots
  const closed = data.closed_today
  const pace = closed.value != null ? closed.value - closed.yesterday : null

  return (
    <div className="mn-screen mn-wall">
      <header className="mn-head">
        <h1>Fleet Wall</h1>
        <p>Every open case, live, grouped by kind of work. The cases that need a person come first.</p>
      </header>

      {data.next_action && (
        <div className="mn-banner" role="status">
          <Icon name="bell" size={15} className="mn-banner-icon" />
          <span>
            <b>{data.next_action.count} case{data.next_action.count === 1 ? ' is' : 's are'} waiting on you</b> (red squares). Each one says why it stopped and what happens if you do nothing. Everything else is running inside its limits.
          </span>
          <span className="mn-banner-actions">
            {data.next_action.case_id && (
              <button type="button" className="mn-primary" onClick={() => openCase(data.next_action!.case_id!)}>Review the first one</button>
            )}
            <button type="button" className="mn-link" onClick={() => go('home')}>Open Needs you</button>
          </span>
        </div>
      )}

      <section className="mn-card mn-strip">
        <div className="mn-strip-slots">
          <div className="mn-gauge-head">
            <span>Slots in use</span>
            <MetricTip label="Slots in use" metric={slots} limit={slots.limit ? `${slots.limit} cases running at once` : 'No concurrency cap is set'} />
          </div>
          <div className="mn-big">
            {count(slots.value)}
            <small>{slots.limit ? ` of ${count(slots.limit)}` : ' · no cap set'}</small>
            <LevelBadge level={slots.level} variant="pill" />
          </div>
        </div>
        <div className="mn-strip-bar">
          {slots.value != null && slots.limit ? (
            <div className="mn-slots-bar" role="meter" aria-label="Slots in use against the cap" aria-valuemin={0} aria-valuemax={slots.limit} aria-valuenow={slots.value}>
              <span className="run" style={{ width: `${(Math.max(0, slots.value - slots.near_limit) / slots.limit) * 100}%` }} />
              <span className="near" style={{ width: `${(Math.min(slots.near_limit, slots.value) / slots.limit) * 100}%` }} />
            </div>
          ) : (
            <div className="mn-slots-bar" />
          )}
          <div className="mn-slots-text">
            <span>{count(slots.value)} running · {count(slots.near_limit)} near their limit</span>
            <span>
              {slots.limit && slots.value != null ? `${count(Math.max(0, slots.limit - slots.value))} free · paused and waiting cases give their slot back` : 'No concurrency cap is set'}
            </span>
          </div>
        </div>
        <div className="mn-strip-closed">
          <div className="mn-gauge-head">
            <span>Closed today</span>
            <MetricTip label="Closed today" metric={closed} limit="No target: pace against yesterday, without a goal" />
          </div>
          <div className="mn-big">
            {count(closed.value)}
            {pace != null && <small className={pace >= 0 ? 'good' : 'fair'}>{` ${pace >= 0 ? '+' : ''}${pace} vs yesterday`}</small>}
          </div>
          <div className="mn-sub mn-one-line">
            {closed.without_person != null ? `${pct(closed.without_person)} without a person` : 'None closed yet today'}
            {' · '}
            {data.incidents_today.value != null ? `${count(data.incidents_today.value)} incidents declared` : 'incidents not measured yet'}
            {data.incidents_today.value == null && <InfoTip label="Why incidents are not measured" text={data.incidents_today.unmeasured_reason ?? ''} align="end" />}
          </div>
        </div>
      </section>

      <div className="mn-facets" role="group" aria-label="Filter by state">
        <span className="mn-muted">Filter by state</span>
        {data.facets.map((f) => (
          <button
            key={f.key}
            data-facet={f.key}
            type="button"
            className={`mn-facet${facet === f.key ? ' active' : ''}`}
            disabled={f.count == null}
            title={f.unmeasured_reason ?? undefined}
            aria-pressed={facet === f.key}
            onClick={() => setFacet(f.key)}
          >
            <span className={`mn-swatch sw-${f.key}`} aria-hidden="true" />
            {f.label}
            <span className="num">{f.count == null ? '—' : count(f.count)}</span>
          </button>
        ))}
      </div>

      {pointed ? (
        <div className="mn-legend mn-pointed" aria-live="polite">
          <span className="mn-pointed-case">
            <span className={`mn-swatch sw-${pointed.cell.near_limit && pointed.cell.state === 'running' ? 'near_limit' : pointed.cell.state}`} aria-hidden="true" />
            <b>Case {pointed.cell.case_id}</b>
            <span className="mn-pointed-text">{hoverText(pointed.cell)}</span>
          </span>
          <span className="mn-muted">Click to see this case and what you can do</span>
        </div>
      ) : (
        <div className="mn-legend">
          <span>
            <b>Each square is one open case.</b> Point at one to read it; click to see the case and what you can do with it.
          </span>
          <span className="mn-muted">
            Thick outline: 5 or more alerts <InfoTip label="How the squares are drawn" calculation={data.info.cell} align="end" />
          </span>
          <span className="mn-muted">
            Percentages: closed without a person today <InfoTip label="How the bands are calculated" calculation={data.info.kind} align="end" />
          </span>
        </div>
      )}

      {chosen && (
        <section className={`mn-card mn-select ${chosen.cell.state}`} aria-label={`Selected case ${chosen.cell.case_id}`}>
          <div className="mn-select-top">
            <span className={`mn-swatch sw-${chosen.cell.state}`} aria-hidden="true" />
            <span>
              Selected square: case <span className="mono">{chosen.cell.case_id}</span>
            </span>
            <span className="mn-muted">Click another square to switch, or press Esc to clear</span>
            <button type="button" className="mn-close" aria-label="Clear the selection" onClick={() => setSelected(null)}>×</button>
          </div>
          <div className="mn-select-main">
            <div>
              <b className="mn-select-title">{chosen.cell.title}</b>
              <div className={`mn-select-why ${chosen.cell.state}`}>
                {chosen.band} · {STATE_WORD[chosen.cell.state]}: {chosen.cell.reason}
              </div>
              {chosen.cell.description && <p className="mn-select-desc">{chosen.cell.description}</p>}
            </div>
            <div className="mn-select-actions">
              <button type="button" className="mn-primary" onClick={() => openCase(chosen.cell.case_id)}>
                {chosen.cell.state === 'needs_you' ? 'Decide in the case' : 'Open the case'}
              </button>
            </div>
          </div>
          <dl className="mn-select-grid">
            <div><dt>Limit used</dt><dd className={chosen.cell.limit_level ?? ''}>{chosen.cell.limit_used == null ? 'Not priced' : pct(chosen.cell.limit_used)}</dd></div>
            <div><dt>Steps taken</dt><dd>{chosen.cell.steps ?? 0}</dd></div>
            <div><dt>Alerts grouped</dt><dd>{chosen.cell.alerts}</dd></div>
            <div><dt>Severity</dt><dd className={`sev ${chosen.cell.severity ?? ''}`}>{chosen.cell.severity ?? 'Not set'}</dd></div>
            <div><dt>SLA</dt><dd>{slaText(chosen.cell.sla_seconds_left)}</dd></div>
            <div><dt>Owner</dt><dd>{chosen.cell.owner ?? 'Agents'}</dd></div>
          </dl>
          <p className="mn-muted">Hold and close from the wall are not built yet; both are on the case page.</p>
        </section>
      )}

      {data.bands.length === 0 ? (
        <EmptyState icon="wall" title="No open cases" body="Every case is closed. New cases appear here as triage forms them." />
      ) : (
        <div className="mn-bands">
          {data.bands.map((band) => (
            <section key={band.workflow_id} className="mn-card mn-band">
              <div className="mn-band-head">
                <div className="mn-band-title">
                  <b>{band.name}</b>
                  {band.without_person.value != null && (
                    <span className={`mn-rate-word ${band.without_person.level ?? ''}`}>{pct(band.without_person.value)} without a person</span>
                  )}
                </div>
                <div className="mn-band-line">
                  <b>{count(band.open)}</b> open case{band.open === 1 ? '' : 's'}<span className="mn-dash"> · </span><b>{count(band.running.value)}</b>
                  {band.running.limit ? <> of {count(band.running.limit)} slots in use</> : ' running'}
                </div>
                {band.running.value != null && band.running.limit ? (
                  <div className="mn-band-slots">
                    <div className="mn-slots-bar" role="meter" aria-label={`${band.name} slots in use`} aria-valuemin={0} aria-valuemax={band.running.limit} aria-valuenow={band.running.value}>
                      <span className="run" style={{ width: `${Math.min(1, band.running.value / band.running.limit) * 100}%` }} />
                    </div>
                    <LevelBadge level={band.running.level} variant="pill" />
                  </div>
                ) : null}
                <div className="mn-sub" title={String(band.running.cap_text ?? '')}>
                  {count(band.today)} runs today · {band.waiting ? `${count(band.waiting)} waiting for a run` : 'none waiting'}
                </div>
              </div>
              <div className="mn-cells" role="list">
                {(band.cells ?? []).map((cell) => {
                  const shown = matches(cell, facet)
                  return (
                    <button
                      key={cell.case_id}
                      type="button"
                      role="listitem"
                      className={[
                        'mn-cell',
                        cell.state,
                        cell.heavy ? 'heavy' : '',
                        cell.near_limit && cell.state === 'running' ? 'near' : '',
                        selected === cell.case_id ? 'selected' : '',
                        shown ? '' : 'dim',
                      ].filter(Boolean).join(' ')}
                      onMouseEnter={() => setHovered(cell.case_id)}
                      onMouseLeave={() => setHovered((h) => (h === cell.case_id ? null : h))}
                      onFocus={() => setHovered(cell.case_id)}
                      onBlur={() => setHovered((h) => (h === cell.case_id ? null : h))}
                      aria-label={`Case ${cell.case_id}, ${STATE_WORD[cell.state]}`}
                      onClick={() => setSelected(cell.case_id === selected ? null : cell.case_id)}
                    />
                  )
                })}
              </div>
            </section>
          ))}
          {data.omitted > 0 && <p className="mn-muted">{count(data.omitted)} more open cases are not drawn.</p>}
        </div>
      )}

      {data.just_closed.length > 0 && (
        <section className="mn-closed">
          <span className="mn-muted">Just closed · last {data.just_closed_minutes} min</span>
          {data.just_closed.map((row) => (
            <button key={row.case_id} type="button" className="mn-closed-chip" onClick={() => openCase(row.case_id)}>
              <span className="mono">{row.case_id}</span> {hhmm(row.closed_at)} by {row.by}
            </button>
          ))}
        </section>
      )}
    </div>
  )
}
