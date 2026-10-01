import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import SystemChecksStep from './SystemChecksStep'
import { consoleApi, storageApi } from '../../services/api'
import { readProviderConfigured } from '../../routing/useSetupStatus'

vi.mock('../../services/api', () => ({
  consoleApi: { getHealth: vi.fn() },
  storageApi: { getStatus: vi.fn() },
}))

vi.mock('../../routing/useSetupStatus', () => ({
  readProviderConfigured: vi.fn(),
}))

describe('SystemChecksStep', () => {
  beforeEach(() => {
    vi.mocked(consoleApi.getHealth).mockResolvedValue({ data: { status: 'healthy' } } as never)
    vi.mocked(storageApi.getStatus).mockResolvedValue({ data: { backend: 'postgresql' } } as never)
    vi.mocked(readProviderConfigured).mockResolvedValue(false)
  })

  it('shows pass, fail, the storage backend, and a retry', async () => {
    render(<SystemChecksStep />)
    expect(await screen.findAllByText('Pass')).toHaveLength(2)
    expect(screen.getByText('Fail')).toBeInTheDocument()
    expect(screen.getByText('postgresql')).toBeInTheDocument()
    expect(screen.getByText('Not configured')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })

  it('fails a check whose read rejects', async () => {
    vi.mocked(readProviderConfigured).mockRejectedValue(new Error('down'))
    render(<SystemChecksStep />)
    expect(await screen.findByText('Could not read')).toBeInTheDocument()
    expect(screen.getByText('Fail')).toBeInTheDocument()
  })
})
