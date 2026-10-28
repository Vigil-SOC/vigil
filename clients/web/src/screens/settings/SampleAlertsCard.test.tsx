import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { SampleAlertsCard } from './DataIngestion'
import { ingestionApi } from '../../services/api'

vi.mock('../../services/api', () => ({
  configApi: {},
  kafkaApi: {},
  ingestionApi: {
    sampleAlerts: vi.fn(),
    loadSampleAlerts: vi.fn(),
    clearSampleAlerts: vi.fn(),
  },
}))

const api = vi.mocked(ingestionApi)

function renderCard() {
  const notify = vi.fn()
  render(<SampleAlertsCard notify={notify} />)
  return notify
}

describe('SampleAlertsCard', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('stays hidden when the status cannot be read (demo mode)', async () => {
    api.sampleAlerts.mockRejectedValue({ response: { status: 409 } })
    renderCard()
    await waitFor(() => expect(api.sampleAlerts).toHaveBeenCalled())
    expect(screen.queryByText('Sample alerts')).not.toBeInTheDocument()
  })

  it('loads the set and then offers to clear it', async () => {
    api.sampleAlerts.mockResolvedValue({ data: { loaded: 0, total: 8 } } as never)
    api.loadSampleAlerts.mockResolvedValue({ data: { loaded: 8, total: 8 } } as never)
    const notify = renderCard()
    fireEvent.click(await screen.findByRole('button', { name: 'Load sample alerts' }))
    await screen.findByRole('button', { name: 'Clear sample alerts' })
    expect(screen.queryByRole('button', { name: 'Load sample alerts' })).not.toBeInTheDocument()
    expect(screen.getByText(/8 of 8 stored/)).toBeInTheDocument()
    expect(notify).toHaveBeenCalledWith('ok', expect.stringContaining('8 sample alerts stored'))
  })

  it('clears after a confirm and reports the cases it removed', async () => {
    api.sampleAlerts.mockResolvedValue({ data: { loaded: 8, total: 8 } } as never)
    api.clearSampleAlerts.mockResolvedValue({ data: { findings_deleted: 8, cases_deleted: 1 } } as never)
    const notify = renderCard()
    fireEvent.click(await screen.findByRole('button', { name: 'Clear sample alerts' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
    await screen.findByRole('button', { name: 'Load sample alerts' })
    expect(notify).toHaveBeenCalledWith('ok', 'Removed 8 sample alerts and 1 case made from them.')
  })

  it('shows the server reason when a load is refused', async () => {
    api.sampleAlerts.mockResolvedValue({ data: { loaded: 0, total: 8 } } as never)
    api.loadSampleAlerts.mockRejectedValue({ response: { data: { detail: 'Permission denied' } } })
    const notify = renderCard()
    fireEvent.click(await screen.findByRole('button', { name: 'Load sample alerts' }))
    await waitFor(() => expect(notify).toHaveBeenCalledWith('err', 'Permission denied'))
  })
})
