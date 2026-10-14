import { useEffect } from 'react'
import { useSearchParams } from 'react-router-dom'
import { InfoTip } from '../../shared/InfoTip'
import { LevelBadge } from '../../shared/LevelBadge'
import { NotMeasured } from '../../shared/NotMeasured'
import { TabStrip } from '../../shared/TabStrip'
import { EmptyState } from '../../shared/ui'
import type { ConsoleScreenProps } from '../../shared/types'
import { monitorApi, type PulseGauge, type DecidedDay, type PulseDecisions, type PulseSummary, type PulseThroughput, type PulseWork } from '../../services/api'
import { DayChart, KindsTable, Measured, MetricTip, QueueChart } from './parts'
import { count, hhmm, minutes, money, pct, usePoll } from './format'
import './monitor.css'

type Tab = 'summary' | 'throughput' | 'decisions' | 'work' | 'sources' | 'system'

const TABS: { id: Tab; label: string }[] = [
  { id: 'summary', label: 'Summary' },
  { id: 'throughput', label: 'Throughput' },
  { id: 'decisions', label: 'Decisions' },
  { id: 'work', label: 'Paused and stuck' },
  { id: 'sources', label: 'Sources and memory' },
  { id: 'system', label: 'System' },
]

// The tabs this release does not build yet: their frame, and what they will show.
const LATER: Partial<Record<Tab, string>> = {
  sources: 'Intake by source with each source’s share, trust against its floor and lag; where memory pays off.',
  system: 'Provider latency and errors, tool availability, memory reads that could not be served, on-demand spend and reverts.',
}

export default function PulseScreen({ go, goSettings, openCase, setViewFull }: ConsoleScreenProps) {
  const [params, setParams] = useSearchParams()
  const asked = params.get('tab') as Tab | null
  const tab: Tab = TABS.some((t) => t.id === asked) ? (asked as Tab) : 'summary'
  const show = (next: Tab) => setParams(next === 'summary' ? {} : { tab: next }, { replace: true })

  // View-full: on Summary the main column and Adjustments each scroll on their own.
  useEffect(() => {
    setViewFull(true)
  }, [setViewFull])

  return (
    <div className={`mn-screen mn-pulse${tab === 'summary' ? ' mn-pulse-summary' : ''}`}>
      <header className="mn-head">
        <h1>Pulse</h1>
        <p>The fleet summarized, every number against its limit. Start with Summary; each tab goes one level deeper.</p>
      </header>
      <TabStrip tabs={TABS} active={tab} onChange={show} label="Pulse" className="mn-tabs" />
      {tab === 'summary' && <Summary show={show} go={go} />}
      {tab === 'throughput' && <Throughput />}
      {tab === 'decisions' && <Decisions goSettings={goSettings} />}
      {tab === 'work' && <Work openCase={openCase} />}
      {LATER[tab] && (
        <section className="mn-card mn-later">
          <h2>{TABS.find((t) => t.id === tab)?.label}</h2>
          <p>{LATER[tab]}</p>
          <NotMeasured tip="This tab is designed and not built yet; its numbers are not folded from the record yet." />
        </section>
      )}
    </div>
  )
}

function Loading({ phase, what, reload }: { phase: string; what: string; reload: () => void }) {
  if (phase === 'error') {
    return <EmptyState error icon="alert" title={`Couldn’t load ${what}`} body="The read failed; it is tried again every 15 s." primary={{ label: 'Retry', onClick: reload, icon: 'refresh' }} />
  }
  return <EmptyState loading icon="pulse" title={`Loading ${what}…`} />
}

function Gauge({ gauge }: { gauge: PulseGauge }) {
  const value = (() => {
    switch (gauge.key) {
      case 'without_person':
        return <Measured metric={gauge} render={pct} />
      case 'time_to_decide':
        return <Measured metric={gauge} render={minutes} />
      case 'spend_today':
        return <Measured metric={gauge} render={money} />
      default:
        return <Measured metric={gauge} render={count} />
    }
  })()
  const sub = (() => {
    if (gauge.key === 'closed_today') return `${count(gauge.yesterday as number)} yesterday by this time`
    if (gauge.key === 'without_person') return gauge.sample ? `Of ${count(gauge.sample as number)} closed today` : 'No case closed today yet'
    if (gauge.key === 'time_to_decide') return gauge.sample ? `Over ${count(gauge.sample as number)} decisions answered today` : 'No decision answered today yet'
    if (gauge.key === 'spend_today') return gauge.limit ? `of ${money(gauge.limit)} today` : 'No daily cap is set'
    return ''
  })()
  return (
    <div className="mn-card mn-gauge">
      <div className="mn-gauge-head">
        <span>{gauge.label}</span>
        <MetricTip label={gauge.label} metric={gauge} limit={gauge.limit ? `${money(gauge.limit)} a day` : 'None configured, so no level'} />
        <LevelBadge level={gauge.level} variant="pill" />
      </div>
      <div className="mn-big">{value}</div>
      <div className="mn-sub">{sub}</div>
    </div>
  )
}

function Summary({ show, go }: { show: (tab: Tab) => void; go: ConsoleScreenProps['go'] }) {
  const { data, phase, reload } = usePoll<PulseSummary>(monitorApi.summary)
  if (!data) return <Loading phase={phase} what="Pulse" reload={reload} />
  const day = data.day
  const formed = day.cases_formed.reduce((a, b) => a + b, 0)
  const attached = day.attached.reduce((a, b) => a + b, 0)
  const decisions = day.decisions.reduce((a, b) => a + b, 0)
  return (
    <div className="mn-split">
      <div className="mn-col">
        <div className="mn-gauges">{data.gauges.map((gauge) => <Gauge key={gauge.key} gauge={gauge} />)}</div>
        <section className="mn-card">
          <header className="mn-card-head">
            <h2>What needs attention</h2>
            <span className="mn-chip">{data.attention.length}</span>
          </header>
          {data.attention.length === 0 ? (
            <p className="mn-empty">Nothing needs attention. Every number is inside its limit.</p>
          ) : (
            <ul className="mn-attention">
              {data.attention.map((item) => (
                <li key={item.title}>
                  <span className={`mn-mark ${item.level}`} aria-hidden="true">!</span>
                  <div>
                    <b>{item.title}</b>
                    <p>{item.why}</p>
                  </div>
                  <button type="button" className="mn-primary" onClick={() => (item.door === 'needs_you' ? go('home') : show(item.door))}>
                    {item.action}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
        <section className="mn-card">
          <header className="mn-card-head">
            <h2>Today at a glance</h2>
            <InfoTip label="How today at a glance is calculated" calculation={day.info} limit={day.cap ? `${day.cap} cases running at once` : 'No concurrency cap is set'} />
            <button type="button" className="mn-link" onClick={() => show('throughput')}>Throughput details →</button>
          </header>
          <p className="mn-sub">Bars: alerts in (grey) and cases closed (green) each hour. Line: cases running{day.cap ? `, against the ${day.cap} cap` : ''}.</p>
          <DayChart day={day} />
          <div className="mn-chips">
            {day.peak.at && <span className="mn-chip-line fair">Peak {day.peak.running} running at {hhmm(day.peak.at)}{day.peak.free != null ? `, ${day.peak.free} slots free` : ''}</span>}
            <span className="mn-chip-line good">{count(formed)} cases formed · {count(attached)} alerts joined open cases · <NotMeasured label="dropped" tip={day.dropped.unmeasured_reason ?? ''} /></span>
            <span className="mn-chip-line accent">{count(decisions)} of your decisions in 24 h</span>
          </div>
        </section>
      </div>
      <aside className="mn-card mn-adjust">
        <h2>Adjustments</h2>
        <p className="mn-sub">Fleet-wide settings, read here. Turning them from Pulse, with a preview and an undo, is not built yet.</p>
        {data.adjustments.map((adj) => (
          <div key={adj.key} className="mn-adjust-row">
            <div className="mn-adjust-top">
              <span>{adj.label}</span>
              {adj.current ? <span className="num">{adj.current}</span> : <NotMeasured tip={adj.note} align="end" />}
            </div>
            <span className="mn-muted">{adj.note}</span>
          </div>
        ))}
      </aside>
    </div>
  )
}

function Throughput() {
  const { data, phase, reload } = usePoll<PulseThroughput>(monitorApi.throughput)
  if (!data) return <Loading phase={phase} what="throughput" reload={reload} />
  return (
    <div className="mn-col">
      <section className="mn-card">
        <header className="mn-card-head">
          <h2>Alerts in, cases closed and cases running · 24 hours by hour</h2>
          <InfoTip label="How the throughput chart is calculated" calculation={data.day.info} />
        </header>
        <DayChart day={data.day} tall />
      </section>
      <KindsTable kinds={data.kinds} info={data.info} />
    </div>
  )
}

function Work({ openCase }: { openCase: (id: string) => void }) {
  const { data, phase, reload } = usePoll<PulseWork>(monitorApi.work)
  if (!data) return <Loading phase={phase} what="paused and stuck" reload={reload} />
  return (
    <div className="mn-col">
      <section className="mn-card">
        <header className="mn-card-head">
          <h2>Paused, stuck and on hold</h2>
          <InfoTip label="How paused, stuck and on hold are calculated" calculation={data.info} limit="No limit is configured for any of them yet." />
        </header>
        <p className="mn-sub">How many, and how long they have waited. The oldest one opens its case.</p>
        {data.rows.map((row) => {
          const top = Math.max(1, ...(row.ages ?? []).map((a) => a.count))
          return (
            <div key={row.key} className={`mn-work-row ${row.key}`}>
              <div className="mn-work-label">
                <b>{row.label}</b>
                <span className={row.count.value == null ? '' : 'num'}><Measured metric={row.count} render={count} /></span>
                <span className="mn-muted">{row.what}</span>
              </div>
              <div className="mn-hist" aria-label={`${row.label} by age`}>
                {row.ages ? (
                  row.ages.map((bucket) => (
                    <div key={bucket.label} className="mn-hist-col" title={`${bucket.count} ${bucket.label}`}>
                      <div className="mn-hist-bar" style={{ height: `${(bucket.count / top) * 100}%` }} />
                    </div>
                  ))
                ) : null}
              </div>
              <div className="mn-work-door">
                {row.oldest ? (
                  <button type="button" className="mn-secondary" onClick={() => openCase(row.oldest!.case_id)}>Oldest: {minutes(row.oldest.minutes)} →</button>
                ) : (
                  <span className="mn-muted">None waiting</span>
                )}
              </div>
            </div>
          )
        })}
        <div className="mn-hist-axis">
          <span />
          <div className="mn-hist-labels">
            {(data.rows.find((r) => r.ages)?.ages ?? []).map((bucket) => <span key={bucket.label}>{bucket.label}</span>)}
          </div>
          <span />
        </div>
      </section>
      <section className="mn-card">
        <header className="mn-card-head">
          <h2>Waiting to be picked up · 24 hours</h2>
          <InfoTip label="How waiting to be picked up is calculated" calculation={data.queue.queue_info} limit={data.queue.high_water ? `Launching stops above ${data.queue.high_water} queued` : 'No high-water mark is set'} />
        </header>
        <p className="mn-sub">
          {data.queue.high_water ? `Above ${data.queue.high_water} queued alerts, the daemon stops launching new work until the queue drains.` : 'No high-water mark is set.'} No alert is dropped by score yet.
        </p>
        <QueueChart hours={data.queue.hours} queued={data.queue.queued} highWater={data.queue.high_water} />
      </section>
    </div>
  )
}

const DECIDER = [
  { key: 'agents_alone', label: 'Agents alone' },
  { key: 'approved_by_person', label: 'Agents, approved by you' },
  { key: 'by_person', label: 'You decided' },
] as const

function shares(day: DecidedDay) {
  const total = day.agents_alone + (day.approved_by_person ?? 0) + day.by_person
  return DECIDER.map(({ key }) => (total ? (day[key] ?? 0) / total : 0))
}

function Decisions({ goSettings }: { goSettings: ConsoleScreenProps['goSettings'] }) {
  const { data, phase, reload } = usePoll<PulseDecisions>(monitorApi.decisions)
  if (!data) return <Loading phase={phase} what="decisions" reload={reload} />
  const { who, stops, suggestion } = data
  const today = who.days[who.days.length - 1]
  const todayShares = today ? shares(today) : [0, 0, 0]
  const top = Math.max(1, ...stops.counts.flat())
  const asked = stops.counts.flat().reduce((a, b) => a + b, 0)
  return (
    <div className="mn-decisions">
      <section className="mn-card">
        <header className="mn-card-head">
          <h2>Who decided</h2>
          <InfoTip label="How who decided is calculated" calculation={who.info} source={who.approved_unmeasured_reason ?? undefined} />
        </header>
        <p className="mn-sub">Of the cases closed today, who made the call. The trend shows whether agents handle more alone as they learn from your decisions.</p>
        {today ? (
          <>
            <div className="mn-decided-bar" role="img" aria-label={DECIDER.map((d, i) => `${d.label} ${pct(todayShares[i])}`).join(', ')}>
              {DECIDER.map((d, i) => <span key={d.key} className={`seg ${d.key}`} style={{ width: `${todayShares[i] * 100}%` }} />)}
            </div>
            <div className="mn-decided-keys">
              {DECIDER.map((d, i) => (
                <span key={d.key}>
                  <i className={`key ${d.key}`} aria-hidden="true" />
                  {d.label}{' '}
                  {d.key === 'approved_by_person' && today.approved_by_person == null ? (
                    <NotMeasured tip={who.approved_unmeasured_reason ?? 'Not recorded apart yet.'} />
                  ) : (
                    <b className="num">{pct(todayShares[i])}</b>
                  )}
                </span>
              ))}
            </div>
            <div className="mn-decided-days" role="list" aria-label="Who decided, each of the last 7 days">
              {who.days.map((day) => {
                const parts = shares(day)
                const line = `${day.day}: ${DECIDER.map((d, i) => `${d.label.toLowerCase()} ${pct(parts[i])}`).join(', ')}`
                return (
                  <div key={day.day} className="mn-decided-day" role="listitem" aria-label={line} title={line}>
                    {DECIDER.map((d, i) => <span key={d.key} className={`seg ${d.key}`} style={{ height: `${parts[i] * 100}%` }} />)}
                  </div>
                )
              })}
            </div>
            <div className="mn-decided-axis"><span>7 days ago</span><span>Today</span></div>
          </>
        ) : (
          <p className="mn-empty">No case closed in the last 7 days.</p>
        )}
      </section>

      <section className="mn-card">
        <header className="mn-card-head">
          <h2>Why agents stopped to ask you</h2>
          <InfoTip label="How why agents stopped is calculated" calculation={stops.info} />
        </header>
        <p className="mn-sub">
          Each stop today, by reason and kind of work.
          {stops.median_minutes != null ? ` Median time to decide: ${minutes(stops.median_minutes)}.` : ' None answered yet today.'}
          {stops.expired ? ` ${count(stops.expired)} expired and followed their default.` : ''}
        </p>
        {asked === 0 ? (
          <p className="mn-empty">No agent stopped to ask today.</p>
        ) : (
          <table className="mn-stops">
            <thead>
              <tr>
                <th scope="col" aria-label="Reason" />
                {stops.kinds.map((k) => <th key={k.workflow_id} scope="col">{k.name}</th>)}
              </tr>
            </thead>
            <tbody>
              {stops.reasons.map((reason, r) => (
                <tr key={reason.key}>
                  <th scope="row">{reason.label}</th>
                  {stops.kinds.map((kind, k) => {
                    const n = stops.counts[r]?.[k] ?? 0
                    return (
                      <td key={kind.workflow_id} className={n ? 'on' : ''} style={n ? { ['--heat' as string]: `${Math.round(25 + (n / top) * 75)}%` } : undefined} title={`${reason.label} · ${kind.name}: ${n}`}>
                        {n ? <span className="num">{n}</span> : <span className="mn-dash" aria-label="none">·</span>}
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {suggestion && (
          <div className="mn-suggest">
            <span>{suggestion.text}</span>
            <button type="button" className="mn-primary" onClick={() => goSettings(suggestion.settings_section)}>{suggestion.action}</button>
          </div>
        )}
      </section>
    </div>
  )
}
