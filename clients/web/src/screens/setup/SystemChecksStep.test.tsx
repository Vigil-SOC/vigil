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
    vi.mocked(consoleApi.getHealth).mockResolvedValue({ data: { status: 'healthy' } } as never)
    vi.mocked(storageApi.getStatus).mockResolvedValue({ data: { backend: 'postgresql' } } as never)
    vi.mocked(readProviderConfigured).mockResolvedValue(true)
    vi.mocked(federationApi.getHealth).mockResolvedValue({
      data: { sources: [{ source_id: 'splunk', enabled: true, consecutive_errors: 0 }] },
    } as never)
    vi.mocked(mcpApi.getStatuses).mockResolvedValue({
      data: { statuses: [{ name: 'splunk', status: 'running', enabled: true }] },
    } as never)
  })

  it('passes all five checks with a detail line each, and offers Retry', async () => {
    render(<SystemChecksStep />)
    expect(await screen.findByText('1 enabled server running')).toBeInTheDocument()
    for (const label of ['API health', 'Storage', 'AI provider', 'Federation', 'MCP servers']) {
      expect(mark(label)).toBe('Passed')
    }
    expect(screen.getByText('postgresql')).toBeInTheDocument()
    expect(screen.getByText('1 source collecting')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })

  it('runs the checks in order, one Checking at a time', async () => {
    let releaseHealth!: () => void
    vi.mocked(consoleApi.getHealth).mockReturnValue(
      new Promise((resolve) => {
        releaseHealth = () => resolve({ data: { status: 'healthy' } } as never)
      }),
    )
    render(<SystemChecksStep />)
    expect(mark('API health')).toBe('Checking')
    for (const label of ['Storage', 'AI provider', 'Federation', 'MCP servers']) {
      expect(mark(label)).toBe('Waiting')
    }
    expect(storageApi.getStatus).not.toHaveBeenCalled()
    releaseHealth()
    expect(await screen.findByText('postgresql')).toBeInTheDocument()
    expect(mark('API health')).toBe('Passed')
  })

  it('names degraded health', async () => {
    vi.mocked(consoleApi.getHealth).mockResolvedValue({ data: { status: 'degraded' } } as never)
    render(<SystemChecksStep />)
    expect(await screen.findByText('Status is degraded')).toBeInTheDocument()
    expect(mark('API health')).toBe('Needs you')
  })

  it('needs you when the provider is not configured', async () => {
    vi.mocked(readProviderConfigured).mockResolvedValue(false)
    render(<SystemChecksStep />)
    expect(await screen.findByText('Not configured')).toBeInTheDocument()
    expect(mark('AI provider')).toBe('Needs you')
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
    expect(mark('Federation')).toBe('Needs you')
  })

  it('keeps federation Waiting when no source is enabled', async () => {
    vi.mocked(federationApi.getHealth).mockResolvedValue({
      data: { sources: [{ source_id: 'elastic', enabled: false, consecutive_errors: 2 }] },
    } as never)
    render(<SystemChecksStep />)
    expect(
      await screen.findByText('Nothing to check yet · the next step connects a source'),
    ).toBeInTheDocument()
    expect(mark('Federation')).toBe('Waiting')
  })

  it('shows Needs you when a federation or MCP read has no body', async () => {
    vi.mocked(federationApi.getHealth).mockResolvedValue({ data: undefined } as never)
    vi.mocked(mcpApi.getStatuses).mockResolvedValue({ data: null } as never)
    render(<SystemChecksStep />)
    await waitFor(() => expect(mark('Federation')).toBe('Needs you'))
    expect(mark('MCP servers')).toBe('Needs you')
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
    expect(mark('MCP servers')).toBe('Needs you')
  })

  it('shows Could not read for a read that fails and carries on', async () => {
    vi.mocked(storageApi.getStatus).mockRejectedValue(new Error('down'))
    render(<SystemChecksStep />)
    expect(await screen.findByText('Could not read')).toBeInTheDocument()
    expect(mark('Storage')).toBe('Needs you')
    expect(await screen.findByText('1 enabled server running')).toBeInTheDocument()
  })

  it('Retry resets and reruns every check', async () => {
    render(<SystemChecksStep />)
    await screen.findByText('1 enabled server running')
    vi.mocked(consoleApi.getHealth).mockResolvedValue({ data: { status: 'degraded' } } as never)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(mark('MCP servers')).toBe('Waiting')
    expect(await screen.findByText('Status is degraded')).toBeInTheDocument()
    expect(await screen.findByText('1 enabled server running')).toBeInTheDocument()
    expect(mcpApi.getStatuses).toHaveBeenCalledTimes(2)
  })
})
