import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import DetectionRulesPanel from './DetectionRulesPanel'
import { detectionRulesApi } from '../../services/api'

vi.mock('../../services/api', () => ({
  detectionRulesApi: {
    listSources: vi.fn(),
    getStats: vi.fn(),
    addSource: vi.fn(),
    removeSource: vi.fn(),
    updateSource: vi.fn(),
    updateAll: vi.fn(),
  },
}))

const sources = [
  { id: 'sigma', name: 'SigmaHQ', type: 'git', git_url: 'https://github.com/SigmaHQ/sigma.git', format: 'sigma', subdirectory: '', story_subdirectory: '', rule_count: 3120, last_updated: null, status: 'ready' },
  { id: 'esc', name: 'Security Content', type: 'git', git_url: 'https://github.com/splunk/security_content', format: 'splunk', subdirectory: '', story_subdirectory: '', rule_count: 0, last_updated: null, status: 'not_cloned' },
  { id: 'odd', name: 'Odd', type: 'local', git_url: '', format: 'weird', subdirectory: '', story_subdirectory: '', rule_count: 5, last_updated: null, status: 'error' },
]

function renderPanel(notify = vi.fn()) {
  render(<DetectionRulesPanel notify={notify} />)
  return notify
}

describe('detection rule sources', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(detectionRulesApi.listSources).mockResolvedValue({ data: { sources } } as never)
    vi.mocked(detectionRulesApi.getStats).mockResolvedValue({
      data: { total_rules: 3125, sources_count: 3, by_format: { sigma: 3120, weird: 5 } },
    } as never)
  })

  it('lists each source as a row with format, where and rule count', async () => {
    renderPanel()
    const row = (await screen.findByText('SigmaHQ')).closest('tr') as HTMLElement

    expect(within(row).getByText('Sigma')).toBeInTheDocument()
    expect(within(row).getByText('github.com/SigmaHQ/sigma')).toBeInTheDocument()
    expect(within(row).getByText('3,120')).toBeInTheDocument()
    expect(screen.getByText(/searches across 3,125 rules/)).toBeInTheDocument()
  })

  it('names a known format plainly and falls back to the raw id', async () => {
    renderPanel()
    const sigma = (await screen.findByText('SigmaHQ')).closest('tr') as HTMLElement

    expect(within(sigma).getByText('Sigma')).not.toHaveClass('chip')
    expect(within(screen.getByText('Security Content').closest('tr') as HTMLElement).getByText('Splunk ESCU')).toBeInTheDocument()
    expect(within(screen.getByText('Odd').closest('tr') as HTMLElement).getByText('weird')).toBeInTheDocument()
    expect(screen.queryByText(/total rules/)).not.toBeInTheDocument()
  })

  it('keeps clone and remove, and updates a source in place', async () => {
    vi.mocked(detectionRulesApi.updateSource).mockResolvedValue({ data: {} } as never)
    const notify = renderPanel()
    const esc = (await screen.findByText('Security Content')).closest('tr') as HTMLElement

    expect(within(esc).getByText('Not cloned')).toBeInTheDocument()
    fireEvent.click(within(esc).getByRole('button', { name: 'Clone' }))
    await waitFor(() => expect(detectionRulesApi.updateSource).toHaveBeenCalledWith('esc'))
    await waitFor(() => expect(notify).toHaveBeenCalledWith('ok', 'Source updated and MCP server restarted.'))

    fireEvent.click(within(esc).getByRole('button', { name: 'Remove Security Content' }))
    expect(await screen.findByText('Remove (keep files)')).toBeInTheDocument()
  })

  it('offers add source and update all in the card header', async () => {
    renderPanel()
    await screen.findByText('SigmaHQ')

    expect(screen.getByRole('button', { name: /Update All/ })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Add source/ }))
    expect(await screen.findByText('Add Detection Rule Source')).toBeInTheDocument()
  })

  it('shows an empty state with an add action', async () => {
    vi.mocked(detectionRulesApi.listSources).mockResolvedValue({ data: { sources: [] } } as never)
    vi.mocked(detectionRulesApi.getStats).mockResolvedValue({ data: { total_rules: 0, sources_count: 0, by_format: {} } } as never)
    renderPanel()

    expect(await screen.findByText('No detection rule sources configured')).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })

  it('shows loading, then an error that retries', async () => {
    vi.mocked(detectionRulesApi.listSources).mockRejectedValueOnce(new Error('boom'))
    renderPanel()

    expect(screen.getByText('Loading detection rules…')).toBeInTheDocument()
    expect(await screen.findByText('Couldn’t load detection rules')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Retry/ }))
    expect(await screen.findByText('SigmaHQ')).toBeInTheDocument()
  })
})
