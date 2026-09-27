import { fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ExclusionsSection from './ExclusionsSection'
import { exclusionsApi, type IpExclusion } from '../../services/api'

vi.mock('../../services/api', () => ({
  exclusionsApi: { list: vi.fn(), create: vi.fn(), remove: vi.fn() },
}))

const active: IpExclusion = {
  exclusion_id: 'excl-1',
  ip: '203.0.113.9',
  reason: 'known scanner',
  origin: 'finding',
  origin_ref: 'f-1',
  created_by: 'analyst-1',
  created_at: '2026-09-23T14:00:00+00:00',
  active: true,
  hidden_findings: 4,
}

function listing(rows: IpExclusion[], hiddenTotal?: number) {
  const hidden_findings_total = hiddenTotal ?? rows.reduce((n, r) => n + (r.active ? r.hidden_findings ?? 0 : 0), 0)
  vi.mocked(exclusionsApi.list).mockResolvedValue({
    data: { exclusions: rows, total: rows.length, hidden_findings_total },
  } as never)
}

function renderSection(onChanged = vi.fn(), onShowExcluded = vi.fn()) {
  render(<ExclusionsSection refreshKey={0} onChanged={onChanged} onShowExcluded={onShowExcluded} />)
  return { onChanged, onShowExcluded }
}

describe('ExclusionsSection', () => {
  beforeEach(() => vi.clearAllMocks())

  it('is collapsed, and says how much it is hiding and that LogLM still scores it', async () => {
    // two exclusions both naming one finding: the header counts it once
    listing([active, { ...active, exclusion_id: 'excl-2', ip: '198.51.100.23', hidden_findings: 1 }], 4)
    renderSection()
    const toggle = await screen.findByRole('button', { name: /Excluded IPs/ })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(await screen.findByText(/hiding 4 findings from the queue/)).toBeInTheDocument()
    expect(screen.getByText(/still ingested and scored by LogLM/)).toBeInTheDocument()
    expect(screen.queryByText('known scanner')).not.toBeInTheDocument()
  })

  it('adds an ad hoc exclusion only with a valid address and a reason', async () => {
    listing([])
    vi.mocked(exclusionsApi.create).mockResolvedValueOnce({ data: { ...active, ip: '198.51.100.7' } } as never)
    const { onChanged } = renderSection()
    fireEvent.click(await screen.findByRole('button', { name: /Excluded IPs/ }))

    const submit = screen.getByRole('button', { name: /^Exclude$/ })
    fireEvent.change(screen.getByLabelText('IP address to exclude'), { target: { value: '10.0.0.0/8' } })
    fireEvent.change(screen.getByLabelText('Reason for excluding'), { target: { value: 'noise' } })
    expect(screen.getByText('Enter one IPv4 or IPv6 address, not a range.')).toBeInTheDocument()
    expect(submit).toBeDisabled()

    fireEvent.change(screen.getByLabelText('IP address to exclude'), { target: { value: ' 198.51.100.7 ' } })
    expect(submit).toBeEnabled()
    fireEvent.click(submit)

    expect(exclusionsApi.create).toHaveBeenCalledWith({ ip: '198.51.100.7', reason: 'noise', origin: 'ad_hoc' })
    await vi.waitFor(() => expect(onChanged).toHaveBeenCalled())
  })

  it('restores an address and can switch the queue to excluded findings', async () => {
    listing([active, { ...active, exclusion_id: 'excl-0', active: false, removed_at: '2026-09-22T10:00:00+00:00', removed_by: 'analyst-2', removal_reason: 'blocked', hidden_findings: null }])
    vi.mocked(exclusionsApi.remove).mockResolvedValueOnce({ data: { ...active, active: false } } as never)
    const { onChanged, onShowExcluded } = renderSection()
    fireEvent.click(await screen.findByRole('button', { name: /Excluded IPs/ }))

    const rows = await screen.findAllByRole('row')
    const current = rows.find((r) => within(r).queryByText('known scanner') && within(r).queryByRole('button', { name: 'Restore' }))!
    expect(within(current).getByText('From finding')).toBeInTheDocument()
    expect(screen.getByText(/Removed .* by analyst-2 — blocked/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Show excluded findings/ }))
    expect(onShowExcluded).toHaveBeenCalled()

    fireEvent.click(within(current).getByRole('button', { name: 'Restore' }))
    expect(exclusionsApi.remove).toHaveBeenCalledWith('excl-1')
    await vi.waitFor(() => expect(onChanged).toHaveBeenCalled())
  })
})
