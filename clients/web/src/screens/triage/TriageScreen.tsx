import { useCallback, useEffect, useMemo, useState, type JSX, type ReactNode } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { DataTable, type ColumnDef } from '../../shared/DataTable'
import { Icon } from '../../shared/icons'
import { FilterChip } from '../../shared/FilterChip'
import { InfoTip } from '../../shared/InfoTip'
import { NotMeasured } from '../../shared/NotMeasured'
import { useSourceBadge } from '../../shared/SourceChip'
import { EmptyState } from '../../shared/ui'
import { utcClock } from '../../shared/utc'
import type { ConsoleScreenProps } from '../../shared/types'
import { parseSourceEvidence } from '../../data/sourceEvidence'
import { SourceEvidenceSection } from '../dashboard/SourceEvidenceSection'
import { triageApi, type TriagePayload, type TriageRow, type TriageSource } from '../../services/api'

const POLL_MS = 10_000
const ROW_CAP = 200 // the server stops at this many rows
const BLANK = '—'
const PAGE_DESC =
  'Every incoming alert and what triage did with it. Agents pick alerts up on their own; check that grouping looks right.'

type Phase = 'loading' | 'error' | 'ready'
type FilterKey = 'kind' | 'source' | 'state'

const KINDS = [
  { value: 'detection', label: 'Alert' },
  { value: 'schedule', label: 'Schedule' },
  { value: 'human_ask', label: 'Ask' },
]

// dot colours are tokens; the state's pill tone lives in styles.css
const STATES = [
  { value: 'queued', label: 'Waiting for a slot', dot: 'tx1' },
  { value: 'launched', label: 'Picked up or started a case', dot: 'good' },
  { value: 'merged', label: 'Added to a case', dot: 'ac' },
  { value: 'expired', label: 'Expired', dot: 'tx2' },
]

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

function alertText(row: TriageRow): string {
  const text = row.kind === 'human_ask' || row.kind === 'schedule' ? row.document : row.description
  return text || BLANK
}

function lagNote(source: TriageSource): string {
  if (source.quiet) return 'Quiet'
  return source.lag_seconds === null ? '' : `lag ${fmtDuration(source.lag_seconds)}`
}

// "Grouped into, and why": the door (when there is one) is drawn as a link, this follows it
function groupedRest(row: TriageRow): string {
  if (!row.case_door) return row.workflow_id || BLANK
  const why = row.state === 'launched' ? ' (new)' : row.state === 'merged' ? ' · overlaps open work' : ''
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

function Tile({ label, info, children }: { label: string; info?: ReactNode; children: ReactNode }) {
  return (
    <div className="tq-tile" aria-label={label}>
      {info}
      <div className="tq-tile-label">{label}</div>
      {children}
    </div>
  )
}

function Tiles({ data }: { data: TriagePayload }) {
  const picked = data.strip.picked_up
  const info = data.strip_info
  const floor = info.trust_floor
  return (
    <div className="tq-tiles" aria-label="Intake strip">
      <Tile label="Picked up automatically" info={<InfoTip label="How picked up automatically is worked out" align="start" {...info.picked_up} />}>
        <div className={`tq-tile-val${picked.share === null ? ' unmeasured' : ''}`}>{fmtShare(picked.share)}</div>
        {picked.created_today > 0 && (
          <div className="tq-tile-sub">{picked.launched_or_merged} of {picked.created_today} today</div>
        )}
      </Tile>
      <Tile label="Cases created today" info={<InfoTip label="How cases created today is worked out" {...info.cases_created_today} />}>
        <div className="tq-tile-val">{data.strip.cases_created_today}</div>
      </Tile>
      <Tile label="Waiting in line" info={<InfoTip label="How waiting in line is worked out" {...info.waiting} />}>
        <div className="tq-tile-val">{data.strip.waiting}</div>
      </Tile>
      <Tile label="Trust floor">
        <NotMeasured className="tq-tile-val unmeasured" align="end" tip={`${floor.source} ${floor.calculation} ${floor.limit}`} />
      </Tile>
    </div>
  )
}

function Filters({
  data, kind, source, state, setFilter, clear,
}: {
  data: TriagePayload
  kind: string
  source: string
  state: string
  setFilter: (key: FilterKey, value: string) => void
  clear: () => void
}) {
  const { counts } = data
  const bySource = new Map(data.sources.map((row) => [row.data_source, row]))
  // an Overview link to a source with no rows still shows its chip, selected
  const sourceNames = Object.keys(counts.source)
  if (source && !sourceNames.includes(source)) sourceNames.push(source)
  sourceNames.sort()
  const filtered = Boolean(kind || source || state)

  const group = (key: FilterKey, current: string, label: string, options: { value: string; label: string; dot?: string; title?: string }[], info?: ReactNode) => (
    <>
      <span className="tq-group-head">
        <span className="tq-group-label">{label}</span>
        {info}
        <FilterChip list label="All" count={counts.total} active={!current} dot="var(--tx2)" onClick={() => setFilter(key, '')} />
      </span>
      {options.map((option) => (
        <FilterChip
          key={option.value}
          list
          label={option.label}
          count={counts[key][option.value] ?? 0}
          active={current === option.value}
          dot={option.dot && `var(--${option.dot})`}
          title={option.title}
          onClick={() => setFilter(key, option.value)}
        />
      ))}
    </>
  )

  return (
    <div className="tq-filters" role="group" aria-label="Filters">
      {group('kind', kind, 'Kind', KINDS)}
      <span className="tq-rule" />
      {group(
        'source',
        source,
        'Source',
        sourceNames.map((name) => {
          const row = bySource.get(name)
          return {
            value: name,
            label: name,
            title: row ? [`${row.arrivals} arrived today`, lagNote(row)].filter(Boolean).join(' · ') : undefined,
          }
        }),
        <InfoTip label={data.arrival_info} text={data.arrival_info} align="start" />,
      )}
      <span className="tq-rule" />
      {group('state', state, 'What happened', STATES)}
      {filtered ? (
        <span className="tq-summary">
          Showing {data.rows.length} of {data.matched} ·{' '}
          <button type="button" onClick={clear}>Clear</button>
        </span>
      ) : data.matched > ROW_CAP ? (
        <span className="tq-summary">Showing {ROW_CAP} of {data.matched}</span>
      ) : null}
    </div>
  )
}

function SourceName({ source }: { source: string }) {
  const { label } = useSourceBadge(source)
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

function Expanded({ row, data }: { row: TriageRow; data: TriagePayload }) {
  const tips = data.breakdown_info
  return (
    <div className="tq-panel" aria-label="Expanded row">
      <span className="tq-h">How the ranking was worked out</span>
      <BreakdownRow label="Severity band">{row.severity_band}</BreakdownRow>
      <BreakdownRow label="Age">{fmtDuration(row.age_seconds)}</BreakdownRow>
      <BreakdownRow label="Time to live">{fmtDuration(row.ttl_seconds)}</BreakdownRow>
      <BreakdownRow label="In the last quarter of its wait">{row.last_quarter ? 'Yes' : 'No'}</BreakdownRow>
      <BreakdownRow label="Source trust"><NotMeasured align="end" tip={tips.trust} /></BreakdownRow>
      <BreakdownRow label="Weight"><NotMeasured align="end" tip={tips.weight} /></BreakdownRow>
      <BreakdownRow label="Score against a floor"><NotMeasured align="end" tip={tips.score} /></BreakdownRow>
      {row.kind === 'detection' && <div className="tq-evidence"><EvidenceBody row={row} /></div>}
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

const TriageScreen: (props: ConsoleScreenProps) => JSX.Element = ({ openCase }) => {
  const [searchParams, setSearchParams] = useSearchParams()
  const kind = searchParams.get('kind') ?? ''
  const source = searchParams.get('source') ?? ''
  const state = searchParams.get('state') ?? ''
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [data, setData] = useState<TriagePayload | null>(null)
  const [openId, setOpenId] = useState<number | null>(null)

  const setFilter = (key: FilterKey, value: string) => {
    const next = new URLSearchParams(searchParams)
    if (value) next.set(key, value)
    else next.delete(key)
    setSearchParams(next, { replace: true })
  }

  const load = useCallback(() => {
    const params: { kind?: string; source?: string; state?: string } = {}
    if (kind) params.kind = kind
    if (source) params.source = source
    if (state) params.state = state
    triageApi
      .get(params)
      .then((res) => {
        setData(res.data)
        setPhase('ready')
        setError(null)
      })
      .catch(() => {
        setPhase('error')
        setError('Couldn’t load triage')
      })
  }, [kind, source, state])

  useEffect(() => {
    load()
    const id = setInterval(load, POLL_MS)
    return () => clearInterval(id)
  }, [load])

  const toggle = (id: number) => setOpenId((current) => (current === id ? null : id))

  // no column carries a sortVal: rows stay in the server's order
  const columns = useMemo<ColumnDef<TriageRow>[]>(() => [
    { key: 'kind', label: 'Kind', render: (row) => <span className="tq-kind">{row.kind_label}</span> },
    { key: 'source', label: 'Source', render: (row) => row.source ? <SourceName source={row.source} /> : BLANK },
    {
      key: 'alert',
      label: 'Alert, in the source’s words',
      render: (row) => {
        const text = alertText(row)
        return <span className="tq-alert" title={text === BLANK ? undefined : text}>{text}</span>
      },
    },
    {
      key: 'score',
      label: 'Score',
      header: <span className="tq-th-info">Score{data && <InfoTip label="How the score is worked out" text={data.breakdown_info.score} align="start" />}</span>,
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
    { key: 'arrived', label: 'Arrived', render: (row) => <span className="tq-time">{fmtArrived(row.created_at)}</span> },
    {
      key: 'pickup',
      label: 'Picked up in',
      render: (row) => <span className="tq-time">{row.pickup_seconds === null ? BLANK : fmtDuration(row.pickup_seconds)}</span>,
    },
    {
      key: 'state',
      label: 'What happened',
      render: (row) => row.state_label ? <span className={`tq-pill ${row.state}`}>{row.state_label}</span> : BLANK,
    },
    {
      key: 'why',
      label: 'Grouped into, and why',
      render: (row) => {
        const door = row.case_door
        const rest = groupedRest(row)
        return (
          <span className="tq-why" title={`${door ?? ''}${rest}`}>
            {door && (
              <Link
                to={`/cases?case=${encodeURIComponent(door)}`}
                onClick={(event) => {
                  event.stopPropagation()
                  if (event.metaKey || event.ctrlKey || event.shiftKey) return // new tab or window
                  event.preventDefault()
                  openCase(door)
                }}
              >
                {door}
              </Link>
            )}
            {rest}
          </span>
        )
      },
    },
  ], [openCase, openId, data])

  const filtered = Boolean(kind || source || state)

  return (
    <div className="tq-page">
      <div className="tq-head">
        <div className="tq-title">
          {/* inline weight: the shell's unlayered h1 rule would beat the stylesheet's */}
          <h1 style={{ fontWeight: 700, letterSpacing: '-0.2px' }}>Triage queue</h1>
          <p>{PAGE_DESC}</p>
        </div>
        <button type="button" className="btn ghost icon" title="Refresh" aria-label="Refresh" onClick={load}>
          <Icon name="refresh" />
        </button>
      </div>

      {phase === 'loading' && <EmptyState loading icon="clock" title="Loading triage…" />}
      {phase === 'error' && (
        <EmptyState error icon="alert" title="Couldn’t load triage" body={error} primary={{ label: 'Retry', onClick: load, icon: 'refresh' }} />
      )}
      {phase === 'ready' && data && (
        <>
          <Tiles data={data} />
          <Filters
            data={data}
            kind={kind}
            source={source}
            state={state}
            setFilter={setFilter}
            clear={() => setSearchParams({}, { replace: true })}
          />
          <div className="tq-card">
            <DataTable
              className="tbl tq-table"
              columns={columns}
              rows={data.rows}
              rowKey={(row) => String(row.id)}
              onRowClick={(row) => toggle(row.id)}
              expandedKey={openId === null ? null : String(openId)}
              renderExpanded={(row) => <Expanded row={row} data={data} />}
              emptyMessage={filtered ? 'Nothing in the queue matches these filters.' : 'Nothing in intake.'}
            />
          </div>
        </>
      )}
    </div>
  )
}

export default TriageScreen
