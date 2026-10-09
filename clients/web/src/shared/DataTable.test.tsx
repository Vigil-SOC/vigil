import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { DataTable, type ColumnDef } from './DataTable'

interface Row { id: string; name: string }
const rows: Row[] = [{ id: 'a', name: 'Alpha' }, { id: 'b', name: 'Beta' }]
const columns: ColumnDef<Row>[] = [
  { key: 'id', label: 'Id', render: (r) => r.id, sortVal: (r) => r.id },
  { key: 'name', label: 'Name', render: (r) => r.name },
]

function table(expandedKey: string | null, onRowClick = vi.fn(), onSort = vi.fn()) {
  return render(
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(r) => r.id}
      sort={{ key: 'id', dir: 'asc' }}
      onSort={onSort}
      onRowClick={onRowClick}
      expandedKey={expandedKey}
      renderExpanded={(r) => <button type="button">More on {r.name}</button>}
    />,
  )
}

describe('DataTable expanded row', () => {
  it('draws a full-width row directly under the matching row only', () => {
    table('a')
    const panel = screen.getByRole('button', { name: 'More on Alpha' }).closest('tr')!
    expect(panel.previousElementSibling).toBe(screen.getByText('Alpha').closest('tr'))
    expect(panel.firstElementChild).toHaveAttribute('colspan', '2')
    expect(screen.queryByText('More on Beta')).not.toBeInTheDocument()
  })

  it('draws nothing without a key, and a click in the panel does not click the row or sort', () => {
    const { rerender } = table(null)
    expect(screen.queryByText('More on Alpha')).not.toBeInTheDocument()
    rerender(<></>)
    const onRowClick = vi.fn()
    const onSort = vi.fn()
    table('a', onRowClick, onSort)
    fireEvent.click(screen.getByRole('button', { name: 'More on Alpha' }))
    expect(onRowClick).not.toHaveBeenCalled()
    expect(onSort).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText('Alpha'))
    expect(onRowClick).toHaveBeenCalledWith(rows[0])
  })
})
