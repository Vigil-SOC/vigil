import { Fragment, useMemo, useState, type ReactNode } from 'react'
import { Icon } from './icons'

/**
 * Hand-written <thead>/<td> pairs keep five things in sync by hand: header cells,
 * body cells, the colSpan on the loading/empty/error rows, and the sort and
 * search field unions. A ColumnDef collapses all five, and lets columns be built
 * at runtime — which is what makes a table adaptable to rows whose fields differ
 * by source.
 */
export interface ColumnDef<T> {
  key: string
  label: string
  render: (row: T) => ReactNode
  /** presence makes the column sortable */
  sortVal?: (row: T) => number | string
  /** presence makes the column searchable */
  searchVal?: (row: T) => string
  defaultDir?: 'asc' | 'desc'
  /** false hides the column by default (still toggleable) */
  visible?: boolean
  /** header content when `label` should not be rendered as text (e.g. an actions column) */
  headless?: boolean
  /** replaces the label in an unsortable header, e.g. a label with an ⓘ */
  header?: ReactNode
}

export type SortState = { key: string; dir: 'asc' | 'desc' }

export type TablePhase = 'loading' | 'error' | 'ready'

export function searchRows<T>(rows: T[], columns: ColumnDef<T>[], query: string): T[] {
  const q = query.trim().toLowerCase()
  if (!q) return rows
  const searchable = columns.filter((c) => c.searchVal)
  return rows.filter((r) => searchable.some((c) => c.searchVal!(r).toLowerCase().includes(q)))
}

export function sortRows<T>(rows: T[], columns: ColumnDef<T>[], sort: SortState): T[] {
  const col = columns.find((c) => c.key === sort.key && c.sortVal)
  if (!col) return rows
  const val = col.sortVal!
  return [...rows].sort((a, b) => {
    const x = val(a)
    const y = val(b)
    const d = typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y))
    return sort.dir === 'asc' ? d : -d
  })
}

function SortHeader<T>(
  { col, sort, onSort }: { col: ColumnDef<T>; sort: SortState; onSort: (k: string) => void },
) {
  if (!col.sortVal) return <th>{col.headless ? null : col.header ?? col.label}</th>
  const active = sort.key === col.key
  return (
    <th className={`sortable${active ? ' sorted' : ''}`} onClick={() => onSort(col.key)}>
      {col.label}
      {active && (
        <span className="arr"><Icon name={sort.dir === 'asc' ? 'arrowUp' : 'arrowDn'} size={12} /></span>
      )}
    </th>
  )
}

export interface DataTableProps<T> {
  columns: ColumnDef<T>[]
  rows: T[]
  rowKey: (row: T) => string
  phase?: TablePhase
  error?: string | null
  /** omit both for a table that keeps its rows in the order given */
  sort?: SortState
  onSort?: (key: string) => void
  onRowClick?: (row: T) => void
  /** key of the row whose `renderExpanded` panel is drawn full-width directly under it */
  expandedKey?: string | null
  renderExpanded?: (row: T) => ReactNode
  className?: string
  emptyMessage?: ReactNode
  loadingMessage?: ReactNode
  onRetry?: () => void
}

const NO_SORT: SortState = { key: '', dir: 'asc' }

export function DataTable<T>({
  columns, rows, rowKey, phase = 'ready', error, sort = NO_SORT, onSort = () => {},
  onRowClick, expandedKey = null, renderExpanded, className = 'tbl', emptyMessage = 'No rows found.',
  loadingMessage = 'Loading…', onRetry,
}: DataTableProps<T>) {
  // derived, so a column added or hidden can't desync the placeholder rows
  const span = columns.length

  return (
    <table className={className}>
      <thead>
        <tr>
          {columns.map((c) => <SortHeader key={c.key} col={c} sort={sort} onSort={onSort} />)}
        </tr>
      </thead>
      <tbody>
        {phase === 'loading' && (
          <tr><td colSpan={span} className="muted" style={{ textAlign: 'center', padding: '40px 0' }}>{loadingMessage}</td></tr>
        )}
        {phase === 'error' && (
          <tr><td colSpan={span} className="muted" style={{ textAlign: 'center', padding: '40px 0' }}>
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 10 }}>
              <span>{error}</span>
              {onRetry && <button className="btn ghost" onClick={onRetry}>Retry</button>}
            </div>
          </td></tr>
        )}
        {phase === 'ready' && rows.length === 0 && (
          <tr><td colSpan={span} className="muted" style={{ textAlign: 'center', padding: '40px 0' }}>{emptyMessage}</td></tr>
        )}
        {phase === 'ready' && rows.map((r) => {
          const key = rowKey(r)
          return (
            <Fragment key={key}>
              <tr
                className={onRowClick ? 'clickable' : undefined}
                onClick={onRowClick ? () => onRowClick(r) : undefined}
              >
                {columns.map((c) => <td key={c.key}>{c.render(r)}</td>)}
              </tr>
              {renderExpanded && key === expandedKey && (
                <tr className="row-expanded">
                  {/* the panel is not part of the row: a click in it must not toggle the row */}
                  <td colSpan={span} onClick={(e) => e.stopPropagation()}>{renderExpanded(r)}</td>
                </tr>
              )}
            </Fragment>
          )
        })}
      </tbody>
    </table>
  )
}

export function ColumnPicker<T>(
  { columns, hidden, onToggle }:
  { columns: ColumnDef<T>[]; hidden: Set<string>; onToggle: (key: string) => void },
) {
  const toggleable = useMemo(() => columns.filter((c) => !c.headless), [columns])
  return (
    <div className="filter-group">
      <div className="filter-group-label">Columns</div>
      {toggleable.map((c) => (
        <label key={c.key} className="filter-opt" style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <input type="checkbox" checked={!hidden.has(c.key)} onChange={() => onToggle(c.key)} />
          <span>{c.label}</span>
        </label>
      ))}
    </div>
  )
}

/** honours each column's preferred initial direction */
export function useTableSort<T>(columns: ColumnDef<T>[], initial: SortState) {
  const [sort, setSort] = useState<SortState>(initial)
  const toggle = (key: string) =>
    setSort((s) => (s.key === key
      ? { key, dir: s.dir === 'asc' ? 'desc' : 'asc' }
      : { key, dir: columns.find((c) => c.key === key)?.defaultDir ?? 'desc' }))
  return { sort, setSort, toggle }
}
