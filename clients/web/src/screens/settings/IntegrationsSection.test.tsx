import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
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
  detectionRulesApi: {},
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
    render(<IntegrationsSection notify={vi.fn()} />)

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
})
