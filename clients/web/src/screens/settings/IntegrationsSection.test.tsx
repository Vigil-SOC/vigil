import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import IntegrationsSection from './IntegrationsSection'
import { tabFromQuery } from './integrationsData'
import { IntegrationsStateProvider } from './IntegrationsState'
import { configApi, mcpApi } from '../../services/api'

vi.mock('../../services/api', () => ({
  default: {},
  aiConfigApi: {},
  budgetsApi: {},
  configApi: {
    getIntegrations: vi.fn(() => Promise.resolve({ data: {} })),
    setIntegrations: vi.fn(),
  },
  detectionRulesApi: {
    listSources: vi.fn(() => Promise.resolve({ data: { sources: [] } })),
    getStats: vi.fn(() => Promise.resolve({ data: {} })),
  },
  federationApi: {},
  ingestionApi: {},
  kafkaApi: {},
  localServicesApi: {},
  mcpApi: {
    listServers: vi.fn(),
    getStatuses: vi.fn(),
    setServerEnabled: vi.fn(),
  },
  orchestratorApi: {},
  storageApi: {},
}))

function renderSection(path = '/settings?section=integrations') {
  render(
    <MemoryRouter initialEntries={[path]}>
      <IntegrationsStateProvider>
        <IntegrationsSection notify={vi.fn()} />
      </IntegrationsStateProvider>
    </MemoryRouter>,
  )
}

function row(name: string) {
  const cell = screen.getByText(name).closest('tr')
  if (!cell) throw new Error(`no row for ${name}`)
  return within(cell as HTMLElement)
}

const NOW = Date.now()
const iso = (msAgo: number) => new Date(NOW - msAgo).toISOString()

describe('Integrations: Connected table', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve({ json: async () => ({}) })),
    )
    vi.mocked(configApi.getIntegrations).mockResolvedValue({
      data: {
        enabled_integrations: ['github', 'virustotal'],
        integrations: {},
        secrets_set: {},
        last_test: {
          github: { at: iso(5 * 60_000), success: true, error: null },
          virustotal: { at: iso(3 * 3600_000), success: false, error: 'HTTP 403 from VirusTotal' },
        },
      },
    } as never)
    vi.mocked(mcpApi.listServers).mockResolvedValue({
      data: { servers: ['github', 'virustotal', 'security-detections', 'okta'] },
    } as never)
    vi.mocked(mcpApi.getStatuses).mockResolvedValue({
      data: {
        statuses: [
          { name: 'github', status: 'running', enabled: true },
          {
            name: 'virustotal',
            status: 'disconnected',
            enabled: true,
            error: 'connection refused',
            missing_credentials: ['VT_API_KEY'],
          },
          { name: 'security-detections', status: 'disconnected', enabled: false },
          { name: 'okta', status: 'disconnected', enabled: false, missing_credentials: ['OKTA_API_TOKEN'] },
        ],
      },
    } as never)
  })

  it('reads health from the session and the last test, and counts what needs attention', async () => {
    renderSection()
    expect(await screen.findByRole('tab', { name: 'Connected 2' })).toHaveAttribute('aria-selected', 'true')

    const good = row('Github')
    expect(good.getByText('Good')).toBeInTheDocument()
    expect(good.getByText('5 min ago')).toHaveAttribute('title', expect.stringContaining('T'))

    const poor = row('Virustotal')
    expect(poor.getByText('Poor')).toBeInTheDocument()
    expect(poor.getByText('3 h ago')).toBeInTheDocument()
    expect(poor.getByText('Missing VT_API_KEY — connection refused — HTTP 403 from VirusTotal')).toBeInTheDocument()
    expect(poor.getByRole('button', { name: 'Fix' })).toBeInTheDocument()

    // tiles: Connected, Healthy, Need attention
    const tile = (k: string) => screen.getByText(k, { selector: '.int-tile-k' }).closest('.int-tile') as HTMLElement
    expect(tile('Connected')).toHaveTextContent('2')
    expect(tile('Healthy')).toHaveTextContent('1')
    expect(tile('Need attention')).toHaveTextContent('1')

    // the banner names the first failing integration
    expect(screen.getByRole('status')).toHaveTextContent('Virustotal needs a fix.')
    expect(screen.getByRole('status')).toHaveTextContent('HTTP 403 from VirusTotal')
  })

  it('keeps unconnected servers off the table and offers them under Add integration', async () => {
    renderSection()
    await screen.findByRole('tab', { name: 'Connected 2' })
    expect(screen.queryByText('Security Detections')).not.toBeInTheDocument()
    expect(screen.queryByText('Okta')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: /Add integration/ }))
    expect(screen.getByText('Security Detections')).toBeInTheDocument()
    // an unconnected server is not "Poor" for the credentials it was never given
    expect(screen.queryByText('Poor')).not.toBeInTheDocument()
    expect(screen.getByText('Okta')).toBeInTheDocument()
  })

  it('shows a dash, not a level, where nothing was ever tested', async () => {
    vi.mocked(configApi.getIntegrations).mockResolvedValue({
      data: { enabled_integrations: ['github'], integrations: {}, secrets_set: {}, last_test: {} },
    } as never)
    vi.mocked(mcpApi.getStatuses).mockResolvedValue({
      data: { statuses: [{ name: 'github', status: 'running', enabled: true }] },
    } as never)
    renderSection()
    await screen.findByRole('tab', { name: /Connected/ })
    expect(row('Github').getByText('—')).toHaveAttribute('title', 'Never tested')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('says so when nothing is connected', async () => {
    vi.mocked(configApi.getIntegrations).mockResolvedValue({ data: {} } as never)
    vi.mocked(mcpApi.getStatuses).mockResolvedValue({ data: { statuses: [] } } as never)
    renderSection()
    expect(await screen.findByText('Nothing is connected yet')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Add integration/ }))
    expect(screen.getByRole('tab', { name: /Add integration/ })).toHaveAttribute('aria-selected', 'true')
  })

  it('shows loading, then an error with Retry', async () => {
    vi.mocked(mcpApi.listServers).mockRejectedValue(new Error('boom'))
    renderSection()
    expect(screen.getByText('Loading integrations…')).toBeInTheDocument()
    expect(await screen.findByText('Couldn’t load MCP servers')).toBeInTheDocument()
    vi.mocked(mcpApi.listServers).mockResolvedValue({ data: { servers: [] } } as never)
    fireEvent.click(screen.getByRole('button', { name: /Retry/ }))
    await waitFor(() => expect(screen.queryByText('Couldn’t load MCP servers')).not.toBeInTheDocument())
  })
})

describe('Integrations: tab key', () => {
  it('maps the old servers value to connected and keeps the rest', () => {
    expect(tabFromQuery('servers')).toBe('connected')
    expect(tabFromQuery('connected')).toBe('connected')
    expect(tabFromQuery('add')).toBe('add')
    expect(tabFromQuery('custom')).toBe('custom')
    expect(tabFromQuery('surface')).toBe('surface')
    expect(tabFromQuery('detection')).toBe('connected')
    expect(tabFromQuery(null)).toBe('connected')
  })

  it('opens the tab the query asks for, and Connected for a tab that lives elsewhere', () => {
    vi.mocked(mcpApi.listServers).mockResolvedValue({ data: { servers: [] } } as never)
    vi.mocked(mcpApi.getStatuses).mockResolvedValue({ data: { statuses: [] } } as never)
    renderSection('/settings?section=integrations&tab=custom')
    expect(screen.getByRole('tab', { name: /Custom/ })).toHaveAttribute('aria-selected', 'true')
  })

  it('stays on Connected for a tab that lives elsewhere', () => {
    vi.mocked(mcpApi.listServers).mockResolvedValue({ data: { servers: [] } } as never)
    vi.mocked(mcpApi.getStatuses).mockResolvedValue({ data: { statuses: [] } } as never)
    renderSection('/settings?section=integrations&tab=detection')
    expect(screen.getByRole('tab', { name: /Connected/ })).toHaveAttribute('aria-selected', 'true')
    expect(screen.queryByRole('button', { name: 'Manual Upload' })).not.toBeInTheDocument()
  })
})
