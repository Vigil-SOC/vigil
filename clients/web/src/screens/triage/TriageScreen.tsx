import { useCallback, useEffect, useMemo, useState, type JSX, type ReactNode } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { DataTable, sortRows, useTableSort, type ColumnDef } from '../../shared/DataTable'
import { Icon } from '../../shared/icons'
import { InfoTip } from '../../shared/InfoTip'
import { NotMeasured } from '../../shared/NotMeasured'
import SourceChip from '../../shared/SourceChip'
import { EmptyState, FilterButton, FilterGroup } from '../../shared/ui'
import type { ConsoleScreenProps } from '../../shared/types'
import { parseSourceEvidence } from '../../data/sourceEvidence'
import { SourceEvidenceSection } from '../dashboard/SourceEvidenceSection'
import { triageApi, type TriagePayload, type TriageRow, type TriageSource } from '../../services/api'

const POLL_MS = 10_000
const BLANK = '—'

type Phase = 'loading' | 'error' | 'ready'

const KINDS = [
  { value: '', label: 'Any' },
  { value: 'detection', label: 'Alert' },
  { value: 'schedule', label: 'Schedule' },
  { value: 'human_ask', label: 'Ask' },
]

const STATES = [
  { value: '', label: 'Any' },
  { value: 'queued', label: 'Waiting' },
  { value: 'launched', label: 'Picked up or started a case' },
  { value: 'merged', label: 'Added to a case' },
  { value: 'expired', label: 'Expired' },
]

function fmtDuration(seconds: number): string {
  const whole = Math.max(0, Math.round(seconds))
  if (whole < 60) return `${whole}s`
  const minutes = Math.round(whole / 60)
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  const rem = minutes % 60
  return rem ? `${hours}h ${rem}m` : `${hours}h`
}

function fmtShare(share: number | null): string {
  if (share === null) return BLANK
  return `${(share * 100).toFixed(1)}%`
}

// created_at is naive UTC from the API; without the Z it would read as local time
function fmtArrived(createdAt: string | null): string {
  if (!createdAt) return BLANK
  const date = new Date(`${createdAt}Z`)
  return Number.isNaN(date.getTime()) ? BLANK : date.toLocaleString()
}

function alertText(row: TriageRow): string {
  const text = row.kind === 'human_ask' || row.kind === 'schedule' ? row.document : row.description
  return text || BLANK
}

function lagNote(source: TriageSource): string {
  if (source.quiet) return 'Quiet'
  if (source.lag_seconds === null) return BLANK
  return fmtDuration(source.lag_seconds)
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

function Strip({ data }: { data: TriagePayload }) {
  const picked = data.strip.picked_up
  const info = data.strip_info
  const floor = info.trust_floor
  return (
    <div aria-label="Intake strip">
      <div className="kpi-strip">
        <div className="kpi" aria-label="Picked up automatically">
          <InfoTip label="How picked up automatically is worked out" align="start" {...info.picked_up} />
          <div className="k-label">Picked up automatically</div>
          <div className={`k-val${picked.share === null ? ' unmeasured' : ''}`}>{fmtShare(picked.share)}</div>
          {picked.created_today > 0 && (
            <div className="k-note">{picked.launched_or_merged} of {picked.created_today}</div>
          )}
        </div>
        <div className="kpi" aria-label="Waiting in line">
          <InfoTip label="How waiting in line is worked out" {...info.waiting} />
          <div className="k-label">Waiting in line</div>
          <div className="k-val">{data.strip.waiting}</div>
        </div>
        <div className="kpi" aria-label="Cases created today">
          <InfoTip label="How cases created today is worked out" {...info.cases_created_today} />
          <div className="k-label">Cases created today</div>
          <div className="k-val">{data.strip.cases_created_today}</div>
        </div>
        <div className="kpi" aria-label="Trust floor">
          <div className="k-label">Trust floor</div>
          <NotMeasured className="k-val unmeasured" align="end" tip={`${floor.source} ${floor.calculation} ${floor.limit}`} />
        </div>
      </div>
      {data.sources.length > 0 && (
        <div className="kpi-strip" aria-label="Sources">
          {data.sources.map((source) => (
            <div className="kpi" key={source.data_source} aria-label={source.data_source}>
              <div className="k-label as-stored">{source.data_source}</div>
              <div className="k-val">{source.arrivals}</div>
              <div className="k-note">{lagNote(source)}</div>
              <InfoTip label={data.arrival_info} text={data.arrival_info} />
            </div>
          ))}
        </div>
      )}
    </div>
  )
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

  const setFilter = (key: 'kind' | 'source' | 'state', value: string) => {
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

  const columns = useMemo<ColumnDef<TriageRow>[]>(() => [
    { key: 'kind', label: 'Kind', render: (row) => row.kind_label, sortVal: (row) => row.kind_label },
    { key: 'source', label: 'Source', render: (row) => row.source ? <SourceChip source={row.source} /> : BLANK, sortVal: (row) => row.source },
    {
      key: 'state',
      label: 'State',
      render: (row) => {
        const door = row.case_door
        return door ? (
          <Link
            to={`/cases?case=${encodeURIComponent(door)}`}
            onClick={(event) => {
              event.stopPropagation()
              if (event.metaKey || event.ctrlKey || event.shiftKey) return // new tab or window
              event.preventDefault()
              openCase(door)
            }}
          >
            {row.state_label}
          </Link>
        ) : row.state_label || BLANK
      },
      sortVal: (row) => row.state_label,
    },
    {
      key: 'alert',
      label: 'Alert, in the source’s words',
      render: (row) => {
        const text = alertText(row)
        return <span className="tq-alert" title={text === BLANK ? undefined : text}>{text}</span>
      },
      sortVal: (row) => alertText(row),
    },
    {
      key: 'arrived',
      label: 'Arrived',
      render: (row) => fmtArrived(row.created_at),
      sortVal: (row) => (row.created_at ? new Date(`${row.created_at}Z`).getTime() : 0),
    },
    { key: 'severity', label: 'Severity', render: (row) => row.severity_band, sortVal: (row) => row.severity_band },
    { key: 'age', label: 'Age', render: (row) => fmtDuration(row.age_seconds), sortVal: (row) => row.age_seconds },
    { key: 'workflow', label: 'Workflow', render: (row) => row.workflow_id || BLANK, sortVal: (row) => row.workflow_id },
    {
      key: 'pickup',
      label: 'Time to pickup',
      render: (row) => row.pickup_seconds === null ? BLANK : fmtDuration(row.pickup_seconds),
      sortVal: (row) => row.pickup_seconds ?? -1,
    },
  ], [openCase])
  const tableSort = useTableSort(columns, { key: 'server', dir: 'asc' })

  const sourceOptions = useMemo(() => {
    const names = new Set<string>(['Schedule', 'Ask'])
    for (const row of data?.sources ?? []) names.add(row.data_source)
    if (source) names.add(source)
    return [{ value: '', label: 'Any' }, ...[...names].sort().map((name) => ({ value: name, label: name }))]
  }, [data, source])

  const active = (kind ? 1 : 0) + (source ? 1 : 0) + (state ? 1 : 0)

  return (
    <>
      <div className="flex items-center gap-3 flex-wrap px-[22px] py-[13px] border-b border-line">
        <FilterButton activeCount={active} onClearAll={() => setSearchParams({}, { replace: true })}>
          <FilterGroup label="Kind" value={kind} onSelect={(value) => setFilter('kind', value)} options={KINDS} />
          <FilterGroup label="Source" value={source} onSelect={(value) => setFilter('source', value)} options={sourceOptions} />
          <FilterGroup label="State" value={state} onSelect={(value) => setFilter('state', value)} options={STATES} />
        </FilterButton>
        {source && data && (
          <InfoTip label={data.arrival_info} text={data.arrival_info} align="start" />
        )}
        <div className="flex-1" />
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
          <Strip data={data} />
          <section className="section">
            <div className="card">
              <DataTable
                columns={columns}
                rows={sortRows(data.rows, columns, tableSort.sort)}
                rowKey={(row) => String(row.id)}
                sort={tableSort.sort}
                onSort={tableSort.toggle}
                onRowClick={(row) => setOpenId((current) => current === row.id ? null : row.id)}
                expandedKey={openId === null ? null : String(openId)}
                renderExpanded={(row) => <Expanded row={row} data={data} />}
                emptyMessage="Nothing in intake."
              />
            </div>
          </section>
        </>
      )}
    </>
  )
}

export default TriageScreen
