import { useCallback, useEffect, useMemo, useRef, useState, type JSX, type ReactNode } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { DataTable, type ColumnDef } from '../../shared/DataTable'
import { Icon } from '../../shared/icons'
import { usePageSnapshot } from '../../shell/pageSnapshot'
import { InfoTip } from '../../shared/InfoTip'
import { NotMeasured } from '../../shared/NotMeasured'
import { useSourceBadge } from '../../shared/useSourceBadge'
import { EmptyState, Select } from '../../shared/ui'
import { StatCard, StatCards } from '../../shared/StatCard'
import { utcClock } from '../../shared/utc'
import type { ConsoleScreenProps } from '../../shared/types'
import { parseSourceEvidence } from '../../data/sourceEvidence'
import { SourceEvidenceSection } from '../dashboard/SourceEvidenceSection'
import { triageApi, type TriagePayload, type TriageQuery, type TriageRow, type TriageSource, type TriageVerdict } from '../../services/api'
import { SeverityMark } from '../../shared/SeverityMark'
import FindingDetails from '../../shared/FindingDetails'
import { IntakeBanners } from '../../shared/AutoOpsOffBanner'
import { useAutoOpsOff } from '../../shared/useAutoOpsOff'
import { RangeToggle } from '../../shared/RangeToggle'
import { useCountRange } from '../../shared/countRange'

const POLL_MS = 10_000
const ROW_CAP = 200 // the server stops at this many rows
const BLANK = '—'
const PAGE_DESC =
  'Every incoming alert and what triage did with it. Agents pick alerts up on their own; check that grouping looks right.'
const PAGE_DESC_OFF =
  'Every incoming alert and what triage did with it. Agents pick alerts up only while AutoOps is on; check that grouping looks right.'

type Phase = 'loading' | 'error' | 'ready'
type FilterKey = 'kind' | 'source' | 'state'

const DETECTION = 'detection'
const QUEUED = 'queued'
const KINDS = [
  { value: 'detection', label: 'Alert' },
  { value: 'schedule', label: 'Schedule' },
  { value: 'human_ask', label: 'Ask' },
]

// the table pill's words (_state_label in triage_read.py); dot colours are tokens, the pill tone lives in styles.css
const STATES = [
  { value: 'queued', label: 'Waiting for triage', dot: 'tx1' },
  { value: 'launched', label: 'Started a case', dot: 'good', title: 'Also holds runs picked up without a case, which the table shows as Picked up' },
  { value: 'merged', label: 'Added to a case', dot: 'ac' },
  { value: 'expired', label: 'Expired', dot: 'tx2' },
  // an alert's states below are Overview's groups, not intake words: not picked up is on no case and not waiting
  { value: 'not_picked_up', label: 'Not picked up', dot: 'tx3' },
  // an alert marked as noise: Overview's Dismissed as noise
  { value: 'dismissed', label: 'Dismissed as noise', dot: 'tx3' },
  // an alert naming an excluded IP: the daemon skips its triage on purpose
  { value: 'excluded', label: 'Excluded', dot: 'tx3' },
]
const QUEUED_OFF = 'AutoOps is off'

// DESIGN §2: "1.2 s", "42 s", "4 min 12 s", "7 h 5 min"
function fmtDuration(seconds: number): string {
  const total = Math.max(0, seconds)
  if (total < 10) return `${total.toFixed(1)} s`
  const whole = Math.round(total)
  if (whole < 60) return `${whole} s`
  if (whole < 3600) {
    const rem = whole % 60
    return rem ? `${Math.floor(whole / 60)} min ${rem} s` : `${whole / 60} min`
  }
  const minutes = Math.floor((whole % 3600) / 60)
  const hours = Math.floor(whole / 3600)
  return minutes ? `${hours} h ${minutes} min` : `${hours} h`
}

function fmtShare(share: number | null): string {
  if (share === null) return BLANK
  return `${Math.round(share * 100)}%`
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

// created_at is naive UTC from the API; UTC 24-hour, with the date only before today
function fmtArrived(createdAt: string | null): string {
  if (!createdAt) return BLANK
  const date = new Date(`${createdAt}Z`)
  const time = utcClock(`${createdAt}Z`)
  if (!time) return BLANK
  if (date.toISOString().slice(0, 10) === new Date().toISOString().slice(0, 10)) return time
  return `${date.getUTCDate()} ${MONTHS[date.getUTCMonth()]} ${time}`
}

// Detections show the source's alert name, else its description, else the finding id
function alertText(row: TriageRow): string {
  const text = row.kind === 'human_ask' || row.kind === 'schedule' ? row.document : row.title || row.description || row.finding_id
  return text || BLANK
}

function lagNote(source: TriageSource): string {
  if (source.quiet) return 'Quiet'
  return source.lag_seconds === null ? '' : `lag ${fmtDuration(source.lag_seconds)}`
}

// "Grouped into, and why": the door (when there is one) is drawn as a link, this follows it
function groupedRest(row: TriageRow): string {
  if (!row.case_door) return row.reason || row.workflow_id || BLANK
  const why = row.state === 'launched' ? ' (new)' : row.state === 'merged' && row.merge_note ? ` · ${row.merge_note}` : ''
  return `${why}${row.workflow_id ? ` · ${row.workflow_id}` : ''}`
}

function EvidenceBody({ row }: { row: TriageRow }) {
  const evidence = row.source_evidence
  if (!evidence) return <p>No source evidence on this finding.</p>
  if (evidence.payload_included !== false) {
    const parsed = parseSourceEvidence(evidence)
    if (parsed) return <SourceEvidenceSection evidence={parsed} />
  }
  const kind = typeof evidence.telemetry_kind === 'string' ? evidence.telemetry_kind : 'Evidence'
  const status = typeof evidence.status === 'string' ? evidence.status : 'unknown'
  return (
    <p>
      {kind} · {status}
      {evidence.payload_included === false ? ' · records omitted from this list' : ''}
    </p>
  )
}

function Cards({ data, waitingOn, toggleWaiting }: { data: TriagePayload; waitingOn: boolean; toggleWaiting: () => void }) {
  const picked = data.strip.picked_up
  const info = data.strip_info
  const today = data.range !== 'all'
  const floor = info.trust_floor
  return (
    <StatCards label="Intake strip">
      <StatCard
        label="Picked up automatically"
        info={<InfoTip label="How picked up automatically is worked out" {...info.picked_up} />}
        value={<span className={picked.share === null ? 'unmeasured' : undefined}>{fmtShare(picked.share)}</span>}
        sub={picked.created_today > 0 && `${picked.launched_or_merged.toLocaleString()} of ${picked.created_today.toLocaleString()}${today ? ' today' : ''}`}
      />
      <StatCard
        label={today ? 'Cases created today' : 'Cases created'}
        info={<InfoTip label={`How cases created${today ? ' today' : ''} is worked out`} {...info.cases_created_today} />}
        value={data.strip.cases_created_today.toLocaleString()}
      />
      {/* strip.waiting counts alerts only, so the card filters Kind = Alert and What happened = Waiting together */}
      <StatCard
        label="Waiting for triage"
        info={<InfoTip label="How waiting for triage is worked out" {...info.waiting} />}
        value={data.strip.waiting.toLocaleString()}
        sub={data.strip.waiting_other > 0 && `+${data.strip.waiting_other} hunt or ask`}
        onClick={toggleWaiting}
        pressed={waitingOn}
      />
      <StatCard label="Trust floor" value={<NotMeasured align="end" tip={`${floor.source} ${floor.calculation} ${floor.limit}`} />} />
    </StatCards>
  )
}

function Filters({
  data, kind, source, state, setFilter, clear, autoOpsOff,
}: {
  data: TriagePayload
  autoOpsOff: boolean | null
  kind: string
  source: string
  state: string
  setFilter: (key: FilterKey, value: string) => void
  clear: () => void
}) {
  const { counts } = data
  const badge = useSourceBadge()
  const bySource = new Map(data.sources.map((row) => [row.data_source, row]))
  // an Overview link to a source with no rows still shows it, selected
  const sourceNames = Object.keys(counts.source)
  if (source && !sourceNames.includes(source)) sourceNames.push(source)
  sourceNames.sort()
  const filtered = Boolean(kind || source || state)

  // the first option is All; each option carries its count, zeros stay selectable
  const menu = (key: FilterKey, options: { value: string; label: string; dot?: string; title?: string }[]) => [
    { value: '', label: 'All', count: counts.total },
    ...options.map((o) => ({ ...o, dot: o.dot && `var(--${o.dot})`, count: counts[key][o.value] ?? 0 })),
  ]
  const sources = sourceNames.map((name) => {
    const row = bySource.get(name)
    return {
      value: name,
      label: badge(name).label,
      title: row ? [`${row.arrivals} arrived${data.range === 'all' ? '' : ' today'}`, lagNote(row)].filter(Boolean).join(' · ') : undefined,
    }
  })
  const states = autoOpsOff ? STATES.map((s) => (s.value === QUEUED ? { ...s, label: `Waiting for triage: ${QUEUED_OFF}` } : s)) : STATES

  return (
    <div className="tq-filter-row" role="group" aria-label="Filters">
      <div className="tq-dropdowns">
        <Select name="Kind" value={kind} options={menu('kind', KINDS)} onSelect={(v) => setFilter('kind', v)} />
        <span className="tq-source-pick">
          <Select name="Source" value={source} options={menu('source', sources)} onSelect={(v) => setFilter('source', v)} />
          <InfoTip label={data.arrival_info} text={data.arrival_info} align="start" />
        </span>
        <Select name="What happened" value={state} options={menu('state', states)} onSelect={(v) => setFilter('state', v)} />
      </div>
      {filtered ? (
        <span className="tq-summary">
          Showing {data.rows.length.toLocaleString()} of {data.matched.toLocaleString()} ·{' '}
          <button type="button" onClick={clear}>Clear</button>
        </span>
      ) : data.matched > ROW_CAP ? (
        <span className="tq-summary">Showing {ROW_CAP} of {data.matched.toLocaleString()}</span>
      ) : null}
    </div>
  )
}

function SourceName({ source }: { source: string }) {
  const { label } = useSourceBadge()(source)
  return <span className="tq-source" title={label}>{label}</span>
}

function BreakdownRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="tq-row">
      <span>{label}</span>
      <span className="tq-val">{children}</span>
    </div>
  )
}

const NOT_TRIAGED = 'Not triaged'
const TRIAGE_FAILED = 'Triage failed'

// category and confidence as saved; a part triage did not give is left out, not filled in
function verdictParts(verdict: Extract<TriageVerdict, { state: 'done' }>): string[] {
  const parts: string[] = []
  if (verdict.category) parts.push(verdict.category)
  if (verdict.confidence !== null) parts.push(`${Math.round(verdict.confidence * 100)}%`)
  return parts
}

/** One line for the page snapshot: what triage decided, or why there is no verdict. */
function verdictText(verdict: TriageVerdict | null): string {
  if (!verdict) return '-'
  if (verdict.state === 'failed') return TRIAGE_FAILED
  if (verdict.state === 'none') return NOT_TRIAGED
  return [verdict.severity ?? '', ...verdictParts(verdict)].filter(Boolean).join(' · ') || '-'
}

function Verdict({ verdict }: { verdict: TriageVerdict | null }) {
  if (!verdict) return <>{BLANK}</>
  if (verdict.state === 'failed') {
    return <span className="tq-verdict muted" title={verdict.error}>{TRIAGE_FAILED}</span>
  }
  if (verdict.state === 'none') {
    return (
      <span className="tq-verdict muted" title="No verdict is saved: not triaged yet, or it arrived while triage was off">
        {NOT_TRIAGED}
      </span>
    )
  }
  const parts = verdictParts(verdict)
  if (!verdict.severity && parts.length === 0) return <>{BLANK}</>
  return (
    <span className="tq-verdict" title={verdict.reasoning ?? undefined}>
      {verdict.severity && <SeverityMark level={verdict.severity} />}
      {parts.length > 0 && <span className="tq-verdict-rest">{parts.join(' · ')}</span>}
    </span>
  )
}

function VerdictPanel({ verdict }: { verdict: TriageVerdict }) {
  return (
    <>
      <span className="tq-h">What triage decided</span>
      {verdict.state === 'done' && (
        <>
          <BreakdownRow label="Severity">{verdict.severity ?? BLANK}</BreakdownRow>
          <BreakdownRow label="Category">{verdict.category ?? BLANK}</BreakdownRow>
          <BreakdownRow label="Confidence">{verdict.confidence === null ? BLANK : `${Math.round(verdict.confidence * 100)}%`}</BreakdownRow>
          <BreakdownRow label="Triaged at">{fmtArrived(verdict.triaged_at)}</BreakdownRow>
          {verdict.reasoning && <p className="tq-reasoning">{verdict.reasoning}</p>}
        </>
      )}
      {verdict.state === 'failed' && <BreakdownRow label={TRIAGE_FAILED}>{verdict.error}</BreakdownRow>}
      {verdict.state === 'none' && <BreakdownRow label={NOT_TRIAGED}>No verdict is saved for this alert</BreakdownRow>}
    </>
  )
}

function CaseDoor({ id, openCase }: { id: string; openCase: (id: string) => void }) {
  return (
    <Link
      to={`/cases?case=${encodeURIComponent(id)}`}
      onClick={(event) => {
        event.stopPropagation()
        if (event.metaKey || event.ctrlKey || event.shiftKey) return // new tab or window
        event.preventDefault()
        openCase(id)
      }}
    >
      {id}
    </Link>
  )
}

/** Where the work on this row lives: its own run when it started one, else the case it joined. */
function WorkDone({ row, openCase }: { row: TriageRow; openCase: (id: string) => void }) {
  const { run_id: run, case_door: door } = row
  if (!run && !door) return null
  const started = row.state === 'launched'
  return (
    <>
      <span className="tq-h">What Vigil did</span>
      {!run && (
        <p className="tq-reasoning">
          {started
            ? 'This alert started a run, but the record of that run is not available.'
            : 'This alert started no run of its own. The runs on its case are listed on the case.'}
        </p>
      )}
      <div className="tq-actions">
        {run && door && (
          <Link to={`/cases?case=${encodeURIComponent(door)}&tab=Checked&run=${encodeURIComponent(run)}`} onClick={(event) => event.stopPropagation()}>
            What it checked
          </Link>
        )}
        {run && (
          <Link to={`/workflows?run=${encodeURIComponent(run)}`} onClick={(event) => event.stopPropagation()}>
            Watch the run
          </Link>
        )}
        {door && <span>{run || started ? 'Case' : 'Added to'} <CaseDoor id={door} openCase={openCase} /></span>}
      </div>
    </>
  )
}

function Expanded({ row, data, openCase }: { row: TriageRow; data: TriagePayload; openCase: (id: string) => void }) {
  const tips = data.breakdown_info
  return (
    <div className="tq-panel" aria-label="Expanded row">
      {row.reason && <BreakdownRow label="Why it was not picked up">{row.reason}</BreakdownRow>}
      <WorkDone row={row} openCase={openCase} />
      {row.triage && <VerdictPanel verdict={row.triage} />}
      <span className="tq-h">How the ranking was worked out</span>
      <BreakdownRow label="Severity band">{row.severity_band}</BreakdownRow>
      <BreakdownRow label="Age">{fmtDuration(row.age_seconds)}</BreakdownRow>
      <BreakdownRow label="Time to live">{fmtDuration(row.ttl_seconds)}</BreakdownRow>
      <BreakdownRow label="In the last quarter of its wait">{row.last_quarter ? 'Yes' : 'No'}</BreakdownRow>
      <BreakdownRow label="Source trust"><NotMeasured align="end" tip={tips.trust} /></BreakdownRow>
      <BreakdownRow label="Weight"><NotMeasured align="end" tip={tips.weight} /></BreakdownRow>
      <BreakdownRow label="Score against a floor"><NotMeasured align="end" tip={tips.score} /></BreakdownRow>
      {row.kind === 'detection' && <div className="tq-evidence"><EvidenceBody row={row} />{row.finding_id && <FindingDetails findingId={row.finding_id} />}</div>}
      <div className="tq-actions">
        {row.source_link && (
          <a href={row.source_link} target="_blank" rel="noopener noreferrer">Open in source</a>
        )}
        <span title="Coming in a later release">
          <button type="button" className="btn ghost" disabled>Rescue</button>
        </span>
      </div>
    </div>
  )
}

const TriageScreen: (props: ConsoleScreenProps) => JSX.Element = ({ openCase, setViewFull, goSettings }) => {
  const autoOpsOff = useAutoOpsOff()
  const [searchParams, setSearchParams] = useSearchParams()
  const kind = searchParams.get('kind') ?? ''
  const source = searchParams.get('source') ?? ''
  const state = searchParams.get('state') ?? ''
  const [range, setRange] = useCountRange()
  const [phase, setPhase] = useState<Phase>('loading')
  const [data, setData] = useState<TriagePayload | null>(null)
  usePageSnapshot('triage', phase === 'ready' && data ? [
    `Triage queue, ${data.range === 'all' ? 'all time' : 'today (UTC)'}: ${data.matched} items. Picked up automatically ${data.strip.picked_up.launched_or_merged} of ${data.strip.picked_up.created_today}; cases created ${data.strip.cases_created_today}; waiting in line ${data.strip.waiting}.`,
    'Rows (arrived | kind | source | alert | severity | triage verdict | what happened | picked up in | grouped into | finding):',
    ...data.rows.slice(0, 60).map((r) => [r.created_at ?? '-', r.kind_label, r.source || '-', r.title || r.description || r.document || '-', r.severity_band, verdictText(r.triage), r.state_label, r.pickup_seconds ?? '-', r.case_door || r.reason || r.workflow_id || '-', r.finding_id ?? '-'].join(' | ')),
  ].join('\n') : '')
  const [openId, setOpenId] = useState<number | null>(null)
  const loadTicket = useRef(0)
  const loadedFilter = useRef('')

  // one URL update for any number of params, so none reads stale searchParams
  const setFilters = (values: Partial<Record<FilterKey, string>>) => {
    const next = new URLSearchParams(searchParams)
    for (const [key, value] of Object.entries(values)) {
      if (value) next.set(key, value)
      else next.delete(key)
    }
    setSearchParams(next, { replace: true })
  }
  const setFilter = (key: FilterKey, value: string) => setFilters({ [key]: value })
  const waitingOn = kind === DETECTION && state === QUEUED
  const toggleWaiting = () => setFilters(waitingOn ? { kind: '', state: '' } : { kind: DETECTION, state: QUEUED })

  const load = useCallback(() => {
    // only a filter change supersedes an in-flight request; a poll must not starve a slow one
    const filter = [kind, source, state, range].join('|')
    if (filter !== loadedFilter.current) {
      loadedFilter.current = filter
      loadTicket.current++
    }
    const ticket = loadTicket.current
    const params: TriageQuery = {}
    if (range === 'all') params.range = range
    if (kind) params.kind = kind
    if (source) params.source = source
    if (state) params.state = state
    triageApi
      .get(params)
      .then((res) => {
        if (ticket !== loadTicket.current) return
        setData(res.data)
        setPhase('ready')
      })
      .catch(() => {
        if (ticket !== loadTicket.current) return
        setPhase('error')
      })
  }, [kind, source, state, range])

  useEffect(() => {
    load()
    const id = setInterval(load, POLL_MS)
    return () => clearInterval(id)
  }, [load])

  // the queue fills the view and scrolls inside its card (as Cases, #2151)
  useEffect(() => {
    setViewFull(true)
  }, [setViewFull])

  const toggle = (id: number) => setOpenId((current) => (current === id ? null : id))

  // no column carries a sortVal: rows stay in the server's order
  const columns = useMemo<ColumnDef<TriageRow>[]>(() => [
    { key: 'kind', label: 'Kind', render: (row) => <span className="tq-kind">{row.kind_label}</span> },
    { key: 'source', label: 'Source', className: 'col-low', render: (row) => row.source ? <SourceName source={row.source} /> : BLANK },
    {
      key: 'alert',
      label: 'Alert, in the source’s words',
      // shown only while the col-low columns are hidden (styles.css, .col-low-note)
      header: <>Alert, in the source’s words<span className="col-low-note tq-hide-note">Source, intake score, arrived and picked up in are hidden while Ask Vigil is open</span></>,
      render: (row) => {
        const text = alertText(row)
        // the full description stays reachable when a title stands in for it
        const full = row.title && row.description ? row.description : text
        return <span className="tq-alert" title={full === BLANK ? undefined : full}>{text}</span>
      },
    },
    {
      key: 'verdict',
      label: 'Triage verdict',
      render: (row) => <Verdict verdict={row.triage} />,
    },
    {
      key: 'score',
      label: 'Intake score',
      className: 'col-low',
      header: <span className="tq-th-info">Intake score{data && <InfoTip label="How the intake score is worked out" text={data.breakdown_info.score} align="start" />}</span>,
      render: (row) => (
        <button
          type="button"
          className="tq-score"
          title="Show how this was ranked"
          aria-label="Show how this was ranked"
          aria-expanded={openId === row.id}
          onClick={(event) => {
            event.stopPropagation()
            toggle(row.id)
          }}
        >
          {BLANK}
        </button>
      ),
    },
    { key: 'arrived', label: 'Arrived', className: 'col-low', render: (row) => <span className="tq-time">{fmtArrived(row.created_at)}</span> },
    {
      key: 'pickup',
      label: 'Picked up in',
      className: 'col-low',
      render: (row) => <span className="tq-time">{row.pickup_seconds === null ? BLANK : fmtDuration(row.pickup_seconds)}</span>,
    },
    {
      key: 'state',
      label: 'What happened',
      render: (row) => {
        if (row.dismissed) return <span className="tq-pill dismissed">{row.state_label}</span>
        const label = autoOpsOff && row.state === 'queued' ? QUEUED_OFF : row.state_label
        return label ? <span className={`tq-pill ${row.state}`}>{label}</span> : BLANK
      },
    },
    {
      key: 'why',
      label: 'Grouped into, and why',
      header: <span className="tq-th-info">Grouped into, and why{data && <InfoTip label="Where this comes from" text={data.breakdown_info.reason} />}</span>,
      render: (row) => {
        const door = row.case_door
        const rest = groupedRest(row)
        return (
          <span className={`tq-why${row.reason ? ' reason' : ''}`} title={`${door ?? ''}${rest}`}>
            {door && <CaseDoor id={door} openCase={openCase} />}
            {rest}
          </span>
        )
      },
    },
  ], [openCase, openId, data, autoOpsOff])

  const filtered = Boolean(kind || source || state)

  return (
    <div className="tq-page">
      <div className="tq-head">
        <div className="tq-title">
          {/* inline weight: the shell's unlayered h1 rule would beat the stylesheet's */}
          <h1 style={{ fontWeight: 700, letterSpacing: '-0.2px' }}>Triage queue</h1>
          <p>{autoOpsOff ? PAGE_DESC_OFF : PAGE_DESC}</p>
        </div>
        <div className="tq-head-r">
          <RangeToggle range={range} onChange={setRange} />
          <button type="button" className="btn ghost icon" title="Refresh" aria-label="Refresh" onClick={load}>
            <Icon name="refresh" />
          </button>
        </div>
      </div>

      <IntakeBanners autoOpsOff={autoOpsOff} goSettings={goSettings} />
      {phase === 'loading' && <EmptyState loading icon="clock" title="Loading triage…" />}
      {phase === 'error' && (
        <EmptyState error icon="alert" title="Couldn’t load triage" primary={{ label: 'Retry', onClick: load, icon: 'refresh' }} />
      )}
      {phase === 'ready' && data && (
        <>
          <Cards data={data} waitingOn={waitingOn} toggleWaiting={toggleWaiting} />
          <Filters
            data={data}
            kind={kind}
            source={source}
            state={state}
            setFilter={setFilter}
            // clearing the filters keeps the range
            clear={() => setSearchParams(range === 'all' ? { range } : {}, { replace: true })}
            autoOpsOff={autoOpsOff}
          />
          <div className="tq-card" data-testid="triage-scroll">
            <DataTable
              className="tbl tq-table"
              columns={columns}
              rows={data.rows}
              rowKey={(row) => String(row.id)}
              onRowClick={(row) => toggle(row.id)}
              expandedKey={openId === null ? null : String(openId)}
              renderExpanded={(row) => <Expanded row={row} data={data} openCase={openCase} />}
              emptyMessage={filtered ? 'Nothing in the queue matches these filters.' : 'Nothing in intake.'}
            />
          </div>
        </>
      )}
    </div>
  )
}

export default TriageScreen
