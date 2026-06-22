// frontend/src/redesign/screens/setup/__tests__/SetupScreen.test.tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

vi.mock('../../../../services/api', () => ({
  llmProviderApi: { list: vi.fn(), setDefault: vi.fn() },
  mcpApi: { getConnections: vi.fn() },
  aiConfigApi: { getConfig: vi.fn() },
  budgetsApi: { get: vi.fn() },
  configApi: { getOrchestrator: vi.fn() },
}))
vi.mock('../../../../contexts/AuthContext', () => ({ useAuth: vi.fn() }))
// Stub the provider wizard — its internals are tested elsewhere; here we only
// care that the provider step expands it inline. SetupScreen renders the named
// LlmProviderWizard; Settings uses the default modal wrapper. Mock both.
vi.mock('../../settings/LlmProviderDialog', () => {
  const Mock = ({ onSaved }: { onSaved: () => void }) => (
    <div data-testid="provider-dialog">
      <button onClick={onSaved}>mock-save</button>
    </div>
  )
  return { default: Mock, LlmProviderWizard: Mock }
})

import SetupScreen from '../SetupScreen'
import { llmProviderApi, mcpApi, aiConfigApi, budgetsApi, configApi } from '../../../../services/api'
import { useAuth } from '../../../../contexts/AuthContext'

const ok = (data: unknown) => ({ data })

const renderScreen = () =>
  render(
    <MemoryRouter initialEntries={['/setup']}>
      <SetupScreen />
    </MemoryRouter>,
  )

describe('SetupScreen (redesign /setup)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(useAuth as any).mockReturnValue({ hasPermission: () => true })
    // Fresh install: nothing configured.
    ;(llmProviderApi.list as any).mockResolvedValue(ok([]))
    ;(mcpApi.getConnections as any).mockResolvedValue(ok({ connections: [] }))
    ;(aiConfigApi.getConfig as any).mockResolvedValue(ok({ components: [], assignments: {} }))
    ;(budgetsApi.get as any).mockResolvedValue(ok({ default_vk: '', budget_limit_usd: 0, enforcement_mode: 'warning' }))
    ;(configApi.getOrchestrator as any).mockResolvedValue(ok({ enabled: false }))
  })

  it('lists the steps and opens the provider dialog from the required step', async () => {
    renderScreen()
    // Provider step renders with a Connect action; dashboard is gated until ready.
    const connect = await screen.findByRole('button', { name: /connect/i })
    expect(screen.getByRole('button', { name: /go to dashboard/i })).toBeDisabled()
    // Optional steps deep-link via "Configure".
    expect(screen.getAllByRole('button', { name: /configure/i }).length).toBeGreaterThan(0)

    fireEvent.click(connect)
    expect(screen.getByTestId('provider-dialog')).toBeInTheDocument()
  })

  it('treats an active+default provider as ready and enables the app', async () => {
    (llmProviderApi.list as any).mockResolvedValue(
      ok([{ provider_id: 'p1', is_active: true, is_default: true }]),
    )
    renderScreen()
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /go to dashboard/i })).not.toBeDisabled(),
    )
    // Provider step is satisfied — no Connect affordance.
    expect(screen.queryByRole('button', { name: /connect/i })).not.toBeInTheDocument()
  })

  it('shows an admin-only message without settings.write', async () => {
    (useAuth as any).mockReturnValue({ hasPermission: () => false })
    renderScreen()
    expect(await screen.findByText(/ask an administrator/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /connect/i })).not.toBeInTheDocument()
  })
})
