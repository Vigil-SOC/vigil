import { useCallback, useEffect, useMemo, useRef, useState, type JSX, type RefObject } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { DataTable, sortRows, useTableSort, type ColumnDef } from '../../shared/DataTable'
import { Icon } from '../../shared/icons'
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
  { value: 'launched', label: 'Launched' },
  { value: 'merged', label: 'Merged' },
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
  return (
    <div aria-label="Intake strip">
      <div className="kpi-strip">
        <div className="kpi" aria-label="Picked up automatically">
          <div className="k-label">Picked up automatically</div>
          <div className={`k-val${picked.share === null ? ' unmeasured' : ''}`}>{fmtShare(picked.share)}</div>
          {picked.created_today > 0 && (
            <div className="k-note">{picked.launched_or_merged} of {picked.created_today}</div>
          )}
        </div>
        <div className="kpi" aria-label="Waiting in line">
          <div className="k-label">Waiting in line</div>
          <div className="k-val">{data.strip.waiting}</div>
        </div>
        <div className="kpi" aria-label="Cases created today">
          <div className="k-label">Cases created today</div>
          <div className="k-val">{data.strip.cases_created_today}</div>
        </div>
        <div className="kpi" aria-label="Trust floor">
          <div className="k-label">Trust floor</div>
          <div className="k-val unmeasured">{data.strip.trust_floor}</div>
        </div>
      </div>
      {data.sources.length > 0 && (
        <div className="kpi-strip" aria-label="Sources">
          {data.sources.map((source) => (
            <div className="kpi" key={source.data_source} aria-label={source.data_source}>
              <div className="k-label as-stored">{source.data_source}</div>
              <div className="k-val">{source.arrivals}</div>
              <div className="k-note">{lagNote(source)}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function Expanded({
  row,
  unmeasured,
  panelRef,
}: {
  row: TriageRow
  unmeasured: string
  panelRef: RefObject<HTMLDivElement>
}) {
  return (
    <div ref={panelRef} className="card" aria-label="Expanded row" style={{ margin: '0 22px 18px' }}>
      <div className="card-h"><h3>{row.kind_label}</h3></div>
      <div className="px-[18px] py-3 text-[13px] text-tx-2 flex flex-col gap-2">
        {row.description && <p>{row.description}</p>}
        {row.document && <p>{row.document}</p>}
        <p>
          Severity {row.severity_band} · age {fmtDuration(row.age_seconds)} · ttl {fmtDuration(row.ttl_seconds)}
          {' · '}{row.last_quarter ? 'Last quarter of its wait' : 'Not in the last quarter'}
        </p>
        <p>Score, trust, and weight: {unmeasured}</p>
        {row.kind === 'detection' && <EvidenceBody row={row} />}
        {row.source_link && (
          <p><a href={row.source_link}>{row.source_link}</a></p>
        )}
        <p>
          <span title="Coming in a later release">
            <button type="button" className="btn ghost" disabled>Rescue</button>
          </span>
        </p>
      </div>
    </div>
  )
}

const TriageScreen: (props: ConsoleScreenProps) => JSX.Element = () => {
  const [searchParams, setSearchParams] = useSearchParams()
  const kind = searchParams.get('kind') ?? ''
  const source = searchParams.get('source') ?? ''
  const state = searchParams.get('state') ?? ''
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [data, setData] = useState<TriagePayload | null>(null)
  const [openId, setOpenId] = useState<number | null>(null)
  const expandedRef = useRef<HTMLDivElement>(null)

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

  useEffect(() => {
    expandedRef.current?.scrollIntoView?.({ block: 'nearest' })
  }, [openId])

  const columns = useMemo<ColumnDef<TriageRow>[]>(() => [
    { key: 'kind', label: 'Kind', render: (row) => row.kind_label, sortVal: (row) => row.kind_label },
    { key: 'source', label: 'Source', render: (row) => row.source || BLANK, sortVal: (row) => row.source },
    {
      key: 'state',
      label: 'State',
      render: (row) => row.case_door ? (
        <Link to={`/cases?case=${encodeURIComponent(row.case_door)}`} onClick={(event) => event.stopPropagation()}>
          {row.state_label}
        </Link>
      ) : row.state_label || BLANK,
      sortVal: (row) => row.state_label,
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
  ], [])
  const tableSort = useTableSort(columns, { key: 'server', dir: 'asc' })
  const open = data?.rows.find((row) => row.id === openId) ?? null

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
                emptyMessage="Nothing in intake."
              />
            </div>
          </section>
          {open && <Expanded row={open} unmeasured={data.unmeasured_text} panelRef={expandedRef} />}
        </>
      )}
    </>
  )
}

export default TriageScreen
