import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import LoglmCard from './LoglmCard'
import { configApi } from '../../services/api'

vi.mock('../../services/api', () => ({
  configApi: {
    getIntegrations: vi.fn(),
    setIntegrations: vi.fn(),
    testIntegration: vi.fn(),
  },
}))
vi.mock('../../config/integrations', () => ({ loadCustomIntegrations: vi.fn(() => Promise.resolve()) }))

const configured = {
  data: {
    configured: true,
    enabled_integrations: [],
    integrations: { loglm: { connectorUrl: 'https://loglm.acme.internal' } },
    secrets_set: { loglm: { mint_secret: true, mcp_token: false } },
  },
}

function renderCard(notify = vi.fn()) {
  render(
    <MemoryRouter>
      <LoglmCard notify={notify} />
    </MemoryRouter>,
  )
  return notify
}

describe('LogLM pipeline card', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(configApi.getIntegrations).mockResolvedValue(configured as never)
  })

  it('shows the stored URL and which secrets are saved, never a value', async () => {
    renderCard()

    expect(await screen.findByText('https://loglm.acme.internal')).toBeInTheDocument()
    expect(screen.getByText('•••••• saved')).toBeInTheDocument()
    expect(screen.getByText('Not set')).toBeInTheDocument() // the agent access token
    expect(screen.getByText('Not tested yet')).toBeInTheDocument()
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  })

  it('starts from the last stored test instead of Not tested yet', async () => {
    vi.mocked(configApi.getIntegrations).mockResolvedValue({
      data: { ...configured.data, last_test: { loglm: { at: new Date().toISOString(), success: false, error: 'Connector rejected the session request (401)' } } },
    } as never)
    renderCard()

    expect(await screen.findByText('Connector rejected the session request (401)')).toBeInTheDocument()
    expect(screen.queryByText('Not tested yet')).not.toBeInTheDocument()
  })

  it('hands editing to Integrations rather than saving here', async () => {
    renderCard()

    const link = await screen.findByRole('link', { name: /Configure/ })
    expect(link).toHaveAttribute('href', '/settings?section=integrations')
    expect(configApi.setIntegrations).not.toHaveBeenCalled()
  })

  it('is not set up, and cannot be tested, without a connector URL', async () => {
    vi.mocked(configApi.getIntegrations).mockResolvedValue({ data: { configured: false } } as never)
    renderCard()

    expect(await screen.findByText('Not set up')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Test connection/ })).toBeDisabled()
  })

  it('shows a loading state, then an error with a retry', async () => {
    vi.mocked(configApi.getIntegrations).mockRejectedValue(new Error('down'))
    renderCard()

    expect(screen.getByText('Loading…')).toBeInTheDocument()
    expect(await screen.findByText(/Couldn’t load the LogLM settings/)).toBeInTheDocument()

    vi.mocked(configApi.getIntegrations).mockResolvedValue(configured as never)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('https://loglm.acme.internal')).toBeInTheDocument()
  })

  it('reads an error answer from the endpoint as a failed load', async () => {
    vi.mocked(configApi.getIntegrations).mockResolvedValue({ data: { configured: false, error: 'db down' } } as never)
    renderCard()

    expect(await screen.findByText(/Couldn’t load the LogLM settings/)).toBeInTheDocument()
  })

  it('turns Good with the probe’s message after a passing test', async () => {
    vi.mocked(configApi.testIntegration).mockResolvedValue({
      data: { success: true, message: 'Connector reachable and it accepted the session secret.' },
    } as never)
    renderCard()
    fireEvent.click(await screen.findByRole('button', { name: /Test connection/ }))

    expect(await screen.findByText('Good')).toBeInTheDocument()
    expect(screen.getByText(/accepted the session secret/)).toBeInTheDocument()
    expect(configApi.testIntegration).toHaveBeenCalledWith('loglm')
  })

  it('turns Poor with the reason after a failing test', async () => {
    vi.mocked(configApi.testIntegration).mockResolvedValue({
      data: { success: false, message: 'Connector rejected the session request (401)' },
    } as never)
    renderCard()
    fireEvent.click(await screen.findByRole('button', { name: /Test connection/ }))

    expect(await screen.findByText('Poor')).toBeInTheDocument()
    expect(screen.getByText(/rejected the session request \(401\)/)).toBeInTheDocument()
  })

  it('reports a test that could not run', async () => {
    vi.mocked(configApi.testIntegration).mockRejectedValue({ response: { data: { detail: 'Forbidden' } } })
    const notify = renderCard()
    fireEvent.click(await screen.findByRole('button', { name: /Test connection/ }))

    await waitFor(() => expect(notify).toHaveBeenCalledWith('err', 'Forbidden'))
    expect(screen.getByText('Poor')).toBeInTheDocument()
  })
})
