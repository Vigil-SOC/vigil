import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { DataTable, sortRows, useTableSort, type ColumnDef } from '../../shared/DataTable'
import { Icon } from '../../shared/icons'
import { EmptyState, Popup } from '../../shared/ui'
import type { ConsoleScreenProps } from '../../shared/types'
import { parseSourceEvidence } from '../../data/sourceEvidence'
import { SourceEvidenceSection } from '../dashboard/SourceEvidenceSection'
import {
  overviewApi,
  type OverviewAgent,
  type OverviewFeedItem,
  type OverviewPayload,
} from '../../services/api'

const POLL_MS = 10_000

type Phase = 'loading' | 'error' | 'ready'
type Level = OverviewAgent['level']

function levelWord(level: Level): string {
  switch (level) {
    case 'good':
      return 'Good'
    case 'fair':
      return 'Fair'
    case 'poor':
      return 'Poor'
    case null:
      return '—'
    default: {
      const unexpected: never = level
      return unexpected
    }
  }
}

function fmtRate(rate: number | null): string {
  if (rate === null) return '—'
  return `${(rate * 100).toFixed(1)}%`
}

function EvidenceBody({ item }: { item: OverviewFeedItem }) {
  const evidence = item.source_evidence
  if (!evidence) return <p>No source evidence on this finding.</p>
  if (evidence.payload_included !== false) {
    const parsed = parseSourceEvidence(evidence)
    if (parsed) return <SourceEvidenceSection evidence={parsed} />
  }
  const kind = typeof evidence.telemetry_kind === 'string' ? evidence.telemetry_kind : 'Evidence'
  const status = typeof evidence.status === 'string' ? evidence.status : 'unknown'
  const schema = typeof evidence.schema_id === 'string' ? evidence.schema_id : ''
  return (
    <p>
      {kind} · {status}
      {schema ? ` · ${schema}` : ''}
      {evidence.payload_included === false ? ' · records omitted from this list' : ''}
    </p>
  )
}

function Flow({ data }: { data: OverviewPayload }) {
  return (
    <div className="kpi-strip" aria-label="Today's flow">
      {data.arrivals.map((arrival) => (
        <div className="kpi" key={arrival.data_source} aria-label={arrival.data_source}>
          <div className="k-label as-stored">{arrival.data_source}</div>
          <Link className="k-val" to={`/triage?source=${encodeURIComponent(arrival.data_source)}`}>
            {arrival.count}
          </Link>
          <div className="k-note">{arrival.source_text}</div>
        </div>
      ))}
      <div className="kpi" aria-label="Engine">
        <div className="k-label">Engine</div>
        <div className="k-note">{data.engine.source_text}</div>
      </div>
      {data.outcomes.map((node) => (
        <div className="kpi" key={node.state} aria-label={node.label}>
          <div className="k-label">{node.label}</div>
          {node.count === null ? (
            <div className="k-val unmeasured">{node.unmeasured_text}</div>
          ) : (
            <div className="k-val">{node.count}</div>
          )}
          <div className="k-note">{node.source_text}</div>
          {node.info && (
            <button type="button" className="btn ghost icon" aria-label={node.info} title={node.info}>
              <Icon name="info" size={14} />
            </button>
          )}
        </div>
      ))}
    </div>
  )
}

export default function OverviewScreen({ goSettings, setWallMode }: ConsoleScreenProps) {
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [data, setData] = useState<OverviewPayload | null>(null)
  const [wall, setWall] = useState(false)
  const [open, setOpen] = useState<OverviewFeedItem | null>(null)

  const load = useCallback(() => {
    overviewApi
      .get()
      .then((res) => {
        setData(res.data)
        setPhase('ready')
        setError(null)
      })
      .catch(() => {
        setPhase('error')
        setError('Couldn’t load overview')
      })
  }, [])

  useEffect(() => {
    load()
    const id = setInterval(load, POLL_MS)
    return () => clearInterval(id)
  }, [load])

  useEffect(() => () => setWallMode?.(false), [setWallMode])

  const toggleWall = () => {
    const next = !wall
    setWall(next)
    setWallMode?.(next)
  }

  const agentColumns = useMemo<ColumnDef<OverviewAgent>[]>(() => [
    { key: 'name', label: 'Workflow', render: (row) => row.name, sortVal: (row) => row.name, searchVal: (row) => row.name },
    { key: 'running', label: 'Running', render: (row) => row.running, sortVal: (row) => row.running },
    { key: 'rate', label: '30-day rate', render: (row) => fmtRate(row.rate), sortVal: (row) => row.rate ?? -1 },
    { key: 'sample_size', label: 'Sample', render: (row) => row.sample_size, sortVal: (row) => row.sample_size },
    { key: 'level', label: 'Level', render: (row) => levelWord(row.level), sortVal: (row) => row.level ?? '' },
    {
      key: 'current_step',
      label: 'Current step',
      render: (row) => row.current_step ?? '—',
      sortVal: (row) => row.current_step ?? '',
    },
  ], [])
  const feedColumns = useMemo<ColumnDef<OverviewFeedItem>[]>(() => [
    { key: 'finding_id', label: 'Finding', render: (row) => row.finding_id, sortVal: (row) => row.finding_id },
    { key: 'severity', label: 'Severity', render: (row) => row.severity ?? '—', sortVal: (row) => row.severity ?? '' },
    { key: 'data_source', label: 'Source', render: (row) => row.data_source, sortVal: (row) => row.data_source },
    { key: 'status', label: 'Status', render: (row) => row.status, sortVal: (row) => row.status },
    {
      key: 'terminal_state',
      label: 'Triage',
      render: (row) => <span className="tag">{row.terminal_label}</span>,
      sortVal: (row) => row.terminal_label,
    },
    { key: 'description', label: 'Description', render: (row) => row.description ?? '—', sortVal: (row) => row.description ?? '' },
    { key: 'created_at', label: 'Arrived', render: (row) => row.created_at ?? '—', sortVal: (row) => row.created_at ?? '' },
  ], [])
  const agentSort = useTableSort(agentColumns, { key: 'name', dir: 'asc' })
  const feedSort = useTableSort(feedColumns, { key: 'created_at', dir: 'desc' })

  const refs = (open?.evidence_links ?? []).flatMap((link) => (typeof link.ref === 'string' && link.ref ? [link.ref] : []))

  return (
    <>
      <div className="flex items-center gap-3 flex-wrap px-[22px] py-[13px] border-b border-line">
        <span className="text-[11px] font-semibold tracking-[0.06em] uppercase text-tx-3">
          {data ? `UTC ${data.day}` : 'Today'}
        </span>
        <div className="flex-1" />
        <button type="button" className="btn ghost" aria-pressed={wall} onClick={toggleWall}>
          {wall ? 'Exit wall' : 'Wall'}
        </button>
        <button type="button" className="btn ghost icon" title="Refresh" aria-label="Refresh" onClick={load}>
          <Icon name="refresh" />
        </button>
      </div>

      {phase === 'loading' && <EmptyState loading icon="graph" title="Loading overview…" />}
      {phase === 'error' && (
        <EmptyState error icon="alert" title="Couldn’t load overview" body={error} primary={{ label: 'Retry', onClick: load, icon: 'refresh' }} />
      )}
      {phase === 'ready' && data?.empty && (
        <EmptyState
          icon="gear"
          title="No sources enabled"
          body="Enable a federation source to see what arrives today."
          primary={{ label: 'Settings', onClick: () => goSettings('federation') }}
        />
      )}
      {phase === 'ready' && data && !data.empty && (
        <>
          <Flow data={data} />
          <section className="section">
            <div className="card">
              <div className="card-h">
                <h3>Agents</h3>
                <button type="button" className="btn ghost icon" aria-label={data.rate_info} title={data.rate_info}>
                  <Icon name="info" size={14} />
                </button>
              </div>
              <p className="text-[12px] text-tx-3 px-[18px] py-2">{data.running_source}</p>
              <p className="text-[12px] text-tx-3 px-[18px] pb-2">{data.step_source}</p>
              <DataTable
                columns={agentColumns}
                rows={sortRows(data.agents, agentColumns, agentSort.sort)}
                rowKey={(row) => row.workflow_id}
                sort={agentSort.sort}
                onSort={agentSort.toggle}
                emptyMessage="No workflows."
              />
            </div>
          </section>
          <section className="section">
            <div className="card">
              <div className="card-h"><h3>Alerts</h3></div>
              <DataTable
                columns={feedColumns}
                rows={sortRows(data.feed, feedColumns, feedSort.sort)}
                rowKey={(row) => row.finding_id}
                sort={feedSort.sort}
                onSort={feedSort.toggle}
                onRowClick={setOpen}
                emptyMessage="No alerts."
              />
            </div>
          </section>
        </>
      )}

      <Popup open={open !== null} onClose={() => setOpen(null)} title={open?.finding_id ?? 'Alert'}>
        {open && (
          <>
            <p className="text-[13px] text-tx-2">{open.description ?? 'No description.'}</p>
            <p className="text-[12px] text-tx-3">{open.status} · <span className="tag">{open.terminal_label}</span></p>
            <EvidenceBody item={open} />
            {refs.map((ref) => (
              <p key={ref}><a href={ref}>{ref}</a></p>
            ))}
          </>
        )}
      </Popup>
    </>
  )
}
