import FindingVStrikeAction from './FindingVStrikeAction'
import { findingTitle } from '../../data/findingPresentation'
import { Icon } from '../../shared/icons'
import SourceChip from '../../shared/SourceChip'
import type { ColumnDef } from '../../shared/DataTable'
import { MISSING_FINDING_SCORE, type Finding } from '../../data/data'
import { formatFindingScore } from '../../data/mappers'

const NDASH = '—'

const SEV_RANK: Record<Finding['sev'], number> = {
  Critical: 4, High: 3, Medium: 2, Low: 1, Unrated: 0,
}
const STATUS_RANK: Record<Finding['status'], number> = { open: 0, investigating: 1, closed: 2 }

/** comparable time key: epoch-ms `ts` when the source supplied a time */
function timeKey(f: Finding): number {
  return typeof f.ts === 'number' ? f.ts : 0
}

function labelFor(key: string): string {
  return key.replace(/[._-]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

export function baseFindingColumns(
  onView: (f: Finding) => void,
  onInvestigate: (f: Finding) => void,
  onVStrike?: (f: Finding) => void,
): ColumnDef<Finding>[] {
  return [
    {
      key: 'id', label: 'Finding',
      render: (f) => <span className="finding-title-cell"><button onClick={(event) => { event.stopPropagation(); onView(f) }}>{findingTitle(f)}</button><small className="mono muted">{f.id}</small></span>,
      searchVal: (f) => `${f.id} ${findingTitle(f)}`,
    },
    {
      key: 'sev', label: 'Severity',
      render: (f) => <span className={`sev ${f.sev.toLowerCase()}`}><span className="dot" />{f.sev}</span>,
      sortVal: (f) => SEV_RANK[f.sev], defaultDir: 'desc',
    },
    {
      key: 'tech', label: 'MITRE Technique', visible: false,
      render: (f) => <><span className="tag">{f.tech}</span> <span className="muted">{f.conf}%</span></>,
      searchVal: (f) => f.tech,
    },
    { key: 'tactic', label: 'Tactic', visible: false, render: (f) => f.tactic },
    {
      key: 'endpoints', label: 'Endpoints',
      render: (f) => <span className="mono">{f.sourceIp || NDASH} → {f.destinationIp || NDASH}</span>,
      searchVal: (f) => `${f.sourceIp || ''} ${f.destinationIp || ''}`,
    },
    {
      key: 'src', label: 'Source',
      render: (f) => <SourceChip source={f.src} />,
      searchVal: (f) => f.src,
    },
    {
      key: 'host', label: 'Host', visible: false,
      render: (f) => <span className="mono">{f.host}</span>,
      searchVal: (f) => f.host,
    },
    {
      key: 'user', label: 'User', visible: false,
      render: (f) => <span className="mono muted">{f.user}</span>,
      searchVal: (f) => f.user,
    },
    {
      key: 'time', label: 'Time (UTC)',
      render: (f) => <span className="muted">{f.time}</span>,
      sortVal: timeKey, defaultDir: 'desc',
    },
    {
      key: 'score', label: 'Score',
      render: (f) => (
        typeof f.score === 'number' ? (
          <span className="scorebar">
            <span className="track"><i className={f.score >= 0.8 ? 'hot' : ''} style={{ width: `${f.score * 100}%` }} /></span>
            <span className="num">{formatFindingScore(f.score)}</span>
          </span>
        ) : (
          <span className="muted">{MISSING_FINDING_SCORE}</span>
        )
      ),
      sortVal: (f) => f.score ?? Number.NEGATIVE_INFINITY, defaultDir: 'desc',
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
        <span className="finding-actions">
          {onVStrike && <FindingVStrikeAction finding={f} onOpen={onVStrike} />}
          <span className="row-act">
          <button title="View" onClick={(e) => { e.stopPropagation(); onView(f) }}><Icon name="eye" /></button>
          <button title="Investigate with Vigil" onClick={(e) => { e.stopPropagation(); onInvestigate(f) }}><Icon name="brain" /></button></span>
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
