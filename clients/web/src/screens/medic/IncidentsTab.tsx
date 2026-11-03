import { useEffect, useMemo, useState } from 'react'
import { DataTable, type ColumnDef } from '../../shared/DataTable'
import { FilterChip } from '../../shared/FilterChip'
import { InfoTip } from '../../shared/InfoTip'
import { EmptyState } from '../../shared/ui'
import type { Incident, StatusRead } from './medicTypes'
import { safeText, safeTitle } from './safeText'
import { useIncidents, type IncidentFilters } from './useMedic'
import { durationWord, ratingWord, spanWord, stamp, stateTone, stateWord } from './words'
import { IncidentPane } from './IncidentPane'
import './medic.css'

interface Row {
  inc: Incident
  child: boolean
  /** a parent's suppressed symptoms that are loaded; the toggle row opens them */
  kids: number
}

const STATES: { value: IncidentFilters['state']; label: string }[] = [
  { value: 'open', label: 'Open' },
  { value: 'resolved', label: 'Resolved' },
  { value: 'all', label: 'All' },
]
const LANES: { value: IncidentFilters['lane']; label: string }[] = [
  { value: '', label: 'All lanes' },
  { value: '1', label: 'Lane 1' },
  { value: '2', label: 'Lane 2' },
  { value: '3', label: 'Lane 3' },
]
const DEFAULT: IncidentFilters = { state: 'open', lane: '', unrated: false }

const sensors = (n: number) => `${n} sensor${n === 1 ? '' : 's'}`

/** UX-10: "What" is the rule's title plus up to 3 group values, all as text. */
const what = (i: Incident) => safeText(i.title) + (i.group.length ? ` · ${i.group.map((g) => safeText(g.value)).join(' · ')}` : '')

/** Roots in the order read, each followed by its suppressed symptoms (and theirs) when opened (UX-12). A symptom whose cause is not loaded is a row of its own until the cause arrives. */
function nest(items: Incident[], open: Set<string>): Row[] {
  const ids = new Set(items.map((i) => i.incident_id))
  const kids = new Map<string, Incident[]>()
  for (const i of items) {
    if (i.suppressed_by && ids.has(i.suppressed_by)) kids.set(i.suppressed_by, [...(kids.get(i.suppressed_by) ?? []), i])
  }
  // a chain (A suppresses B, B suppresses C) nests flat under A
  const below = (id: string, seen = new Set<string>()): Incident[] =>
    (kids.get(id) ?? []).flatMap((k) => (seen.has(k.incident_id) ? [] : (seen.add(k.incident_id), [k, ...below(k.incident_id, seen)])))
  const rows: Row[] = []
  for (const i of items) {
    if (i.suppressed_by && ids.has(i.suppressed_by)) continue
    const mine = below(i.incident_id)
    rows.push({ inc: i, child: false, kids: mine.length })
    if (open.has(i.incident_id)) mine.forEach((k) => rows.push({ inc: k, child: true, kids: 0 }))
  }
  return rows
}

export function IncidentsTab({ status, incidentId, open }: {
  status: StatusRead
  incidentId: string | null
  open: (id: string | null) => void
}) {
  const [filters, setFilters] = useState<IncidentFilters>(DEFAULT)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [now, setNow] = useState(Date.now())
  const running = status.state === 'running'
  const list = useIncidents(filters, running)
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 15_000)
    return () => window.clearInterval(t)
  }, [])

  const rows = useMemo(
    () => (list.page ? nest(list.page.items, expanded) : []),
    [list.page, expanded],
  )
  const toggle = (id: string) => setExpanded((s) => {
    const n = new Set(s)
    if (!n.delete(id)) n.add(id)
    return n
  })

  if (incidentId) return <IncidentPane id={incidentId} status={status} onBack={() => open(null)} onOpen={open} />

  const cols: ColumnDef<Row>[] = [
    {
      key: 'state', label: 'State',
      render: ({ inc, child }) => (
        <span className={`mdc-state ${stateTone(inc)}${child ? ' child' : ''}`} title={safeTitle(stateWord(inc))}>
          {child && <span className="mdc-nest" aria-hidden="true">└ </span>}{stateWord(inc)}
        </span>
      ),
    },
    { key: 'lane', label: 'Lane', render: ({ inc }) => <span className="mdc-mono">{inc.lane.value}</span> },
    {
      key: 'what', label: 'What',
      render: ({ inc, child, kids }) => (
        <div className="mdc-what">
          <button type="button" className={`mdc-open${child ? ' child' : ''}`} title={safeTitle(what(inc))} onClick={(e) => { e.stopPropagation(); open(inc.incident_id) }}>
            {what(inc)}
          </button>
          {!child && inc.suppressed_children.count > 0 && (
            <button
              type="button" className="mdc-kids" aria-expanded={expanded.has(inc.incident_id)}
              disabled={kids === 0}
              onClick={(e) => { e.stopPropagation(); toggle(inc.incident_id) }}
            >
              {inc.suppressed_children.count} symptom{inc.suppressed_children.count === 1 ? '' : 's'} suppressed by this
              <span aria-hidden="true">{expanded.has(inc.incident_id) ? ' ▾' : ' ›'}</span>
            </button>
          )}
        </div>
      ),
    },
    { key: 'opened', label: 'Opened', render: ({ inc }) => <span className="mdc-mono" title={safeTitle(inc.opened_at)}>{stamp(inc.opened_at)}</span> },
    { key: 'duration', label: 'Duration', render: ({ inc }) => <span className="mdc-mono">{durationWord(inc, now)}</span> },
    {
      key: 'rating', label: 'Rating',
      render: ({ inc }) => {
        const w = ratingWord(inc)
        return w ? <span title={safeTitle(w)}>{safeText(w)}</span> : <span className="mdc-unrated">Unrated</span>
      },
    },
  ]

  const counts = list.page?.counts
  const filtered = filters.state !== DEFAULT.state || filters.lane !== '' || filters.unrated
  const watching = status.medic?.sensors
  const emptyBody = running && !filtered && watching
    ? `Medic is watching ${sensors(watching.reporting)}.${watching.cant_see > 0 ? ` It can't see ${sensors(watching.cant_see)}.` : ''}`
    : undefined
  const staleMin = list.failedSince && list.lastGood ? spanWord(now - list.lastGood) : null

  // Medic is not answering and no list was ever read: nothing to keep
  if (!running && !list.page) {
    return (
      <div className="mdc-body">
        <EmptyState
          error icon="alert"
          title={status.state === 'down' ? 'Medic is down' : "Vigil can't tell if Medic is running"}
          body="There is no earlier list of incidents to show. This page reads them again once Medic answers."
        />
      </div>
    )
  }

  return (
    <div className="mdc-body">
      {!running && (
        <div className="mdc-strip poor" role="status">
          {status.state === 'down' ? 'Medic is down' : "Vigil can't tell if Medic is running"}: showing data up to{' '}
          {stamp(new Date(Math.min(list.lastGood ?? Infinity, status.last_seen_at ? Date.parse(status.last_seen_at) : Infinity)).toISOString())}
        </div>
      )}
      <div className="mdc-bar">
        <div className={`mdc-filters${running ? '' : ' off'}`} role="group" aria-label="Filter incidents" aria-disabled={!running}>
          {STATES.map((s) => <FilterChip key={s.value} label={s.label} active={filters.state === s.value} onClick={() => running && setFilters((f) => ({ ...f, state: s.value }))} />)}
          <span className="mdc-sep" aria-hidden="true" />
          {LANES.map((l) => <FilterChip key={l.label} label={l.label} active={filters.lane === l.value} onClick={() => running && setFilters((f) => ({ ...f, lane: l.value }))} />)}
          <span className="mdc-sep" aria-hidden="true" />
          <FilterChip label="Unrated" active={filters.unrated} onClick={() => running && setFilters((f) => ({ ...f, unrated: !f.unrated }))} title="Only incidents that can be rated and have no rating yet" />
        </div>
        {counts && (
          <span className="mdc-rate">
            Rated {counts.rated} of {counts.rateable} routed incidents
            {counts.rateable > 0 && ` (${Math.round((counts.rated / counts.rateable) * 100)}%)`}
            <InfoTip
              label="About the rated count"
              source="Admins' ratings stored by Medic, over incidents that were routed and can be rated."
              calculation="Rated incidents divided by routed, rateable incidents. It counts responses, not agreement."
              limit="An unrated incident is left out of Medic's evaluation; it is never counted as agree."
            />
          </span>
        )}
      </div>
      {staleMin && running && <p className="mdc-stale" role="status">Couldn't refresh. Last updated {staleMin} ago.</p>}
      <div className="mdc-card">
        <DataTable
          className="tbl mdc-tbl"
          columns={cols}
          rows={rows}
          rowKey={(r) => `${r.child ? 'c:' : ''}${r.inc.incident_id}`}
          phase={list.phase}
          error="Couldn't load incidents."
          onRetry={list.reload}
          onRowClick={(r) => open(r.inc.incident_id)}
          loadingMessage="Loading incidents…"
          emptyMessage={
            <EmptyState table compact icon={watching?.cant_see ? 'info' : 'check'}
              title={filtered ? 'No incidents match these filters.' : 'No incidents.'}
              body={emptyBody}
            />
          }
        />
      </div>
      {list.page?.next_cursor && (
        <div className="mdc-more">
          <button className="btn ghost" disabled={list.more || !running} onClick={list.loadMore}>{list.more ? 'Loading…' : 'Load more'}</button>
        </div>
      )}
    </div>
  )
}
