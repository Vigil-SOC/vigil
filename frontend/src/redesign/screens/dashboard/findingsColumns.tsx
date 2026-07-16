import { Icon } from '../../shared/icons'
import type { ColumnDef } from '../../shared/DataTable'
import type { Finding } from '../../data/data'

const NDASH = '—'

const SEV_RANK: Record<Finding['sev'], number> = { Critical: 4, High: 3, Medium: 2, Low: 1 }
const STATUS_RANK: Record<Finding['status'], number> = { open: 0, investigating: 1, closed: 2 }

/** comparable time key: findings normally carry epoch-ms `ts`; when it's
 *  missing, fall back to the YYYYMMDD in the id plus HH:MM from the display string */
function timeKey(f: Finding): number {
  if (typeof f.ts === 'number') return f.ts
  const d = /(\d{8})/.exec(f.id)?.[1]
  if (!d) return 0
  const t = /(\d{1,2}):(\d{2})/.exec(f.time)
  return Number(d) * 10000 + (t ? Number(t[1]) * 100 + Number(t[2]) : 0)
}

/** Turn an entity_context key into a column label: dest_ips -> "Dest Ips". */
function labelFor(key: string): string {
  return key.replace(/[._-]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

/** The columns every finding has, whatever produced it. */
export function baseFindingColumns(
  onView: (f: Finding) => void,
  onInvestigate: (f: Finding) => void,
): ColumnDef<Finding>[] {
  return [
    {
      key: 'id', label: 'Finding ID',
      render: (f) => <span className="id-cell">{f.id}</span>,
      searchVal: (f) => f.id,
    },
    {
      key: 'sev', label: 'Severity',
      render: (f) => <span className={`sev ${f.sev.toLowerCase()}`}><span className="dot" />{f.sev}</span>,
      sortVal: (f) => SEV_RANK[f.sev], defaultDir: 'desc',
    },
    {
      key: 'tech', label: 'MITRE Technique',
      render: (f) => <><span className="tag">{f.tech}</span> <span className="muted">{f.conf}%</span></>,
      searchVal: (f) => f.tech,
    },
    { key: 'tactic', label: 'Tactic', render: (f) => f.tactic },
    {
      key: 'src', label: 'Source',
      render: (f) => <span className="muted">{f.src}</span>,
      searchVal: (f) => f.src,
    },
    {
      key: 'host', label: 'Host',
      render: (f) => <span className="mono">{f.host}</span>,
      searchVal: (f) => f.host,
    },
    {
      key: 'user', label: 'User',
      render: (f) => <span className="mono muted">{f.user}</span>,
      searchVal: (f) => f.user,
    },
    {
      key: 'time', label: 'Time',
      render: (f) => <span className="muted">{f.time}</span>,
      sortVal: timeKey, defaultDir: 'desc',
    },
    {
      key: 'score', label: 'Score',
      render: (f) => (
        <span className="scorebar">
          <span className="track"><i className={f.score >= 0.8 ? 'hot' : ''} style={{ width: `${f.score * 100}%` }} /></span>
          <span className="num">{f.score.toFixed(2)}</span>
        </span>
      ),
      sortVal: (f) => f.score, defaultDir: 'desc',
    },
    {
      key: 'status', label: 'Status',
      render: (f) => <span className={`status ${f.status}`}>{f.status}</span>,
      // status reads best low->high (open first); the rest read best high->low
      sortVal: (f) => STATUS_RANK[f.status], defaultDir: 'asc',
    },
    {
      key: 'actions', label: 'Actions', headless: true,
      render: (f) => (
        <span className="row-act">
          <button title="View" onClick={(e) => { e.stopPropagation(); onView(f) }}><Icon name="eye" /></button>
          <button title="Investigate with Vigil" onClick={(e) => { e.stopPropagation(); onInvestigate(f) }}><Icon name="brain" /></button>
        </span>
      ),
    },
  ]
}

/**
 * Columns for whatever source-specific entity keys the loaded rows carry.
 *
 * Derived from the data rather than declared, so a source that sends a field no
 * other source does (CrowdStrike's device_id) shows up without a code change.
 * Hidden by default — they're additive detail, not part of the default view.
 */
export function extraFindingColumns(rows: Finding[]): ColumnDef<Finding>[] {
  const keys = new Set<string>()
  for (const r of rows) {
    if (r.extra) for (const k of Object.keys(r.extra)) keys.add(k)
  }
  return [...keys].sort().map((key) => ({
    key: `extra:${key}`,
    label: labelFor(key),
    visible: false,
    render: (f: Finding) => <span className="mono muted">{f.extra?.[key] || NDASH}</span>,
    searchVal: (f: Finding) => f.extra?.[key] || '',
  }))
}
