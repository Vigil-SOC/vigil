import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'

vi.mock('../../../services/api', () => ({ llmProviderApi: { list: vi.fn() } }))
// Stub RedesignLoader: SetupGate's loading state renders it, and it pulls in the
// theme/config stack (useTheme → configApi) this suite doesn't mock.
vi.mock('../../../redesign/shell/Loader', () => ({ default: () => null }))

import SetupGate from '../SetupGate'
import { llmProviderApi } from '../../../services/api'

const provider = (over = {}) => ({
  provider_id: 'p',
  provider_type: 'anthropic',
  name: 'p',
  base_url: null,
  has_api_key: true,
  default_model: 'm',
  is_active: true,
  is_default: true,
  config: {},
  last_test_at: null,
  last_test_success: true,
  last_error: null,
  created_at: null,
  updated_at: null,
  ...over,
})

const renderGate = () =>
  render(
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route
          path="/"
          element={
            <SetupGate>
              <div>PROTECTED APP</div>
            </SetupGate>
          }
        />
        <Route path="/setup" element={<div>SETUP WIZARD</div>} />
      </Routes>
    </MemoryRouter>,
  )

describe('SetupGate readiness gating', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('admits the user when an active default provider exists', async () => {
    (llmProviderApi.list as any).mockResolvedValue({ data: [provider()] })
    renderGate()
    expect(await screen.findByText('PROTECTED APP')).toBeInTheDocument()
  })

  it('admits a keyless local default (Ollama / OpenAI-compatible)', async () => {
    (llmProviderApi.list as any).mockResolvedValue({
      data: [provider({ provider_type: 'ollama', has_api_key: false })],
    })
    renderGate()
    expect(await screen.findByText('PROTECTED APP')).toBeInTheDocument()
  })

  it('redirects to /setup when the only provider is active but not default', async () => {
    (llmProviderApi.list as any).mockResolvedValue({
      data: [provider({ is_default: false })],
    })
    renderGate()
    expect(await screen.findByText('SETUP WIZARD')).toBeInTheDocument()
  })

  it('redirects to /setup when there are no providers', async () => {
    (llmProviderApi.list as any).mockResolvedValue({ data: [] })
    renderGate()
    expect(await screen.findByText('SETUP WIZARD')).toBeInTheDocument()
  })

  it('fails open (admits the user) when the readiness check errors', async () => {
    (llmProviderApi.list as any).mockRejectedValue(new Error('boom'))
    renderGate()
    expect(await screen.findByText('PROTECTED APP')).toBeInTheDocument()
  })
})
