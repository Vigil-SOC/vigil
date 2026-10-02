import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import IntegrationsSection from './IntegrationsSection'
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
      <IntegrationsSection notify={vi.fn()} />
    </MemoryRouter>,
  )
}

function card(name: string) {
  const title = screen.getByText(name)
  const node = title.closest('.card')
  if (!node) throw new Error(`no card for ${name}`)
  return within(node as HTMLElement)
}

describe('MCP server cards', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve({ json: async () => ({}) })),
    )
    vi.mocked(configApi.getIntegrations).mockResolvedValue({ data: {} } as never)
    vi.mocked(mcpApi.listServers).mockResolvedValue({
      data: { servers: ['github', 'virustotal', 'security-detections'] },
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
        ],
      },
    } as never)
  })

  it('shows Running only for a connected session, and surfaces a failure', async () => {
    renderSection()

    expect(await screen.findByText('1 Running')).toBeInTheDocument()
    expect(screen.getByText('2 Enabled')).toBeInTheDocument()
    expect(screen.getByText('3 Active')).toBeInTheDocument()

    expect(card('Github').getByText('Running')).toBeInTheDocument()

    const failed = card('Virustotal')
    expect(failed.getByText('Enabled')).toBeInTheDocument()
    expect(failed.queryByText('Running')).toBeNull()
    expect(failed.getByText('Missing VT_API_KEY — connection refused')).toBeInTheDocument()

    const disabled = card('Security Detections')
    expect(disabled.getByText('Off')).toBeInTheDocument()
    expect(disabled.queryByText('Running')).toBeNull()
  })

  it('stays on MCP Servers when the query asks for a tab that lives elsewhere', () => {
    renderSection('/settings?section=integrations&tab=detection')
    expect(screen.getByRole('button', { name: 'MCP Servers' })).toHaveClass('active')
    expect(screen.queryByRole('button', { name: 'Manual Upload' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Detection Rules' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Build Custom/ })).toBeInTheDocument()
  })

  it('stays on MCP Servers for any other tab value', () => {
    renderSection('/settings?section=integrations&tab=nope')
    expect(screen.getByRole('button', { name: 'MCP Servers' })).toHaveClass('active')
    expect(screen.queryByRole('button', { name: 'Detection Rules' })).not.toBeInTheDocument()
  })
})
