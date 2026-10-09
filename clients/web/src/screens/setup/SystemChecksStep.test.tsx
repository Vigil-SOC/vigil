import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import SystemChecksStep from './SystemChecksStep'
import { consoleApi, federationApi, mcpApi, storageApi } from '../../services/api'
import { readProviderConfigured } from '../../routing/useSetupStatus'

vi.mock('../../services/api', () => ({
  consoleApi: { getHealth: vi.fn() },
  storageApi: { getStatus: vi.fn() },
  federationApi: { getHealth: vi.fn() },
  mcpApi: { getStatuses: vi.fn() },
}))

vi.mock('../../routing/useSetupStatus', () => ({
  readProviderConfigured: vi.fn(),
}))

const row = (label: string) => screen.getByText(label).closest('[role="listitem"]') as HTMLElement
const mark = (label: string) => within(row(label)).getByRole('img').getAttribute('aria-label')

describe('SystemChecksStep', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(consoleApi.getHealth).mockResolvedValue({
      data: { status: 'healthy' },
    } as never)
    vi.mocked(storageApi.getStatus).mockResolvedValue({
      data: {
        backend: 'postgresql',
        description: 'Using PostgreSQL database',
        database_available: true,
        demo_mode: false,
      },
    } as never)
    vi.mocked(readProviderConfigured).mockResolvedValue(true)
    vi.mocked(federationApi.getHealth).mockResolvedValue({
      data: {
        sources: [{ source_id: 'splunk', enabled: true, consecutive_errors: 0 }],
      },
    } as never)
    vi.mocked(mcpApi.getStatuses).mockResolvedValue({
      data: {
        statuses: [{ name: 'splunk', status: 'running', enabled: true }],
      },
    } as never)
  })

  it('passes all six checks with a detail line each, offers Check again, and shows no summary', async () => {
    render(<SystemChecksStep />)
    expect(await screen.findByText('1 enabled server running')).toBeInTheDocument()
    for (const label of [
      'Database',
      'Model gateway',
      'Storage',
      'API health',
      'Alert collection',
      'Tool servers',
    ]) {
      expect(mark(label)).toBe('Passed')
    }
    expect(screen.getByText('postgresql · Using PostgreSQL database')).toBeInTheDocument()
    expect(screen.getByText('Connected')).toBeInTheDocument()
    expect(screen.getByText('1 source collecting')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Check again' })).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.getByText('About 15 minutes')).toBeInTheDocument()
  })

  it('runs the checks in order, one Checking at a time, with no summary while loading', async () => {
    let releaseProvider!: () => void
    vi.mocked(readProviderConfigured).mockReturnValue(
      new Promise((resolve) => {
        releaseProvider = () => resolve(false)
      }),
    )
    render(<SystemChecksStep />)
    expect(await screen.findByText('Connected')).toBeInTheDocument()
    expect(mark('Database')).toBe('Passed')
    expect(mark('Model gateway')).toBe('Checking')
    for (const label of ['Storage', 'API health', 'Alert collection', 'Tool servers']) {
      expect(mark(label)).toBe('Waiting')
    }
    expect(consoleApi.getHealth).not.toHaveBeenCalled()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    releaseProvider()
    expect(
      await screen.findByText('1 warning. You can continue; fix them before you rely on alerts.'),
    ).toBeInTheDocument()
  })

  it('reads Database and Storage from one storage call', async () => {
    render(<SystemChecksStep />)
    await screen.findByText('1 enabled server running')
    expect(storageApi.getStatus).toHaveBeenCalledTimes(1)
  })

  it('needs you when the database is not available', async () => {
    vi.mocked(storageApi.getStatus).mockResolvedValue({
      data: {
        backend: 'none',
        description: 'PostgreSQL is not connected',
        database_available: false,
        demo_mode: false,
      },
    } as never)
    render(<SystemChecksStep />)
    expect(await screen.findByText('Not connected')).toBeInTheDocument()
    expect(mark('Database')).toBe('Needs you')
    expect(mark('Storage')).toBe('Passed')
  })

  it('passes the database in demo mode', async () => {
    vi.mocked(storageApi.getStatus).mockResolvedValue({
      data: {
        backend: 'demo',
        description: 'Demo mode',
        database_available: false,
        demo_mode: true,
      },
    } as never)
    render(<SystemChecksStep />)
    expect(await screen.findByText('Demo data, no database needed')).toBeInTheDocument()
    expect(mark('Database')).toBe('Passed')
  })

  it('names degraded health', async () => {
    vi.mocked(consoleApi.getHealth).mockResolvedValue({
      data: { status: 'degraded' },
    } as never)
    render(<SystemChecksStep />)
    expect(await screen.findByText('Status is degraded')).toBeInTheDocument()
    expect(mark('API health')).toBe('Needs you')
  })

  it('needs you when the provider is not configured', async () => {
    vi.mocked(readProviderConfigured).mockResolvedValue(false)
    render(<SystemChecksStep />)
    expect(await screen.findByText('You add a provider in step 3')).toBeInTheDocument()
    expect(mark('Model gateway')).toBe('Needs you')
  })

  it('names an enabled federation source that is erroring', async () => {
    vi.mocked(federationApi.getHealth).mockResolvedValue({
      data: {
        sources: [
          { source_id: 'elastic', enabled: false, consecutive_errors: 4 },
          { source_id: 'splunk', enabled: true, consecutive_errors: 3 },
        ],
      },
    } as never)
    render(<SystemChecksStep />)
    expect(await screen.findByText('splunk has consecutive errors')).toBeInTheDocument()
    expect(mark('Alert collection')).toBe('Needs you')
  })

  it('keeps federation Waiting when no source is enabled', async () => {
    vi.mocked(federationApi.getHealth).mockResolvedValue({
      data: {
        sources: [{ source_id: 'elastic', enabled: false, consecutive_errors: 2 }],
      },
    } as never)
    render(<SystemChecksStep />)
    expect(
      await screen.findByText('Nothing to check yet · the next step connects a source'),
    ).toBeInTheDocument()
    expect(mark('Alert collection')).toBe('Waiting')
    // Waiting with no source is not a warning
    await screen.findByText('1 enabled server running')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('shows Needs you when a federation or MCP read has no body', async () => {
    vi.mocked(federationApi.getHealth).mockResolvedValue({ data: undefined } as never)
    vi.mocked(mcpApi.getStatuses).mockResolvedValue({ data: null } as never)
    render(<SystemChecksStep />)
    await waitFor(() => expect(mark('Alert collection')).toBe('Needs you'))
    expect(mark('Tool servers')).toBe('Needs you')
    expect(screen.getAllByText('Could not read')).toHaveLength(2)
  })

  it('names an enabled MCP server that is not running', async () => {
    vi.mocked(mcpApi.getStatuses).mockResolvedValue({
      data: {
        statuses: [
          { name: 'a', status: 'stopped', enabled: false },
          { name: 'splunk-selfhosted', status: 'stopped', enabled: true },
        ],
      },
    } as never)
    render(<SystemChecksStep />)
    expect(await screen.findByText('splunk-selfhosted is stopped')).toBeInTheDocument()
    expect(mark('Tool servers')).toBe('Needs you')
  })

  it('shows Could not read for a read that fails, counts it, and carries on', async () => {
    vi.mocked(storageApi.getStatus).mockRejectedValue(new Error('down'))
    render(<SystemChecksStep />)
    expect(await screen.findByText('1 enabled server running')).toBeInTheDocument()
    expect(mark('Database')).toBe('Needs you')
    expect(mark('Storage')).toBe('Needs you')
    expect(screen.getAllByText('Could not read')).toHaveLength(2)
    expect(
      screen.getByText('2 warnings. You can continue; fix them before you rely on alerts.'),
    ).toBeInTheDocument()
  })

  it('Check again resets and reruns every check, and clears the summary', async () => {
    vi.mocked(consoleApi.getHealth).mockResolvedValue({
      data: { status: 'degraded' },
    } as never)
    render(<SystemChecksStep />)
    expect(
      await screen.findByText('1 warning. You can continue; fix them before you rely on alerts.'),
    ).toBeInTheDocument()
    vi.mocked(consoleApi.getHealth).mockResolvedValue({
      data: { status: 'healthy' },
    } as never)
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }))
    for (const label of [
      'Database',
      'Model gateway',
      'Storage',
      'API health',
      'Alert collection',
      'Tool servers',
    ]) {
      expect(mark(label)).not.toBe('Passed')
    }
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(await screen.findByText('1 enabled server running')).toBeInTheDocument()
    expect(mark('API health')).toBe('Passed')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(mcpApi.getStatuses).toHaveBeenCalledTimes(2)
    expect(storageApi.getStatus).toHaveBeenCalledTimes(2)
  })
})
