import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'

vi.mock('../../services/api', () => ({
  llmProviderApi: {
    list: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    test: vi.fn(),
    listModels: vi.fn(),
    setDefault: vi.fn(),
    discoverModels: vi.fn(),
  },
}))
vi.mock('../../hooks/useSetupStatus', () => ({ default: vi.fn() }))
vi.mock('../../contexts/AuthContext', () => ({ useAuth: vi.fn() }))

import Setup from '../Setup'
import { llmProviderApi } from '../../services/api'
import useSetupStatus from '../../hooks/useSetupStatus'
import { useAuth } from '../../contexts/AuthContext'

const renderSetup = () =>
  render(
    <MemoryRouter initialEntries={['/setup']}>
      <Routes>
        <Route path="/setup" element={<Setup />} />
        <Route path="/" element={<div>HOME DASHBOARD</div>} />
      </Routes>
    </MemoryRouter>,
  )

describe('Setup wizard (first-access)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(useAuth as any).mockReturnValue({ hasPermission: () => true })
    ;(useSetupStatus as any).mockReturnValue({ configured: false, loading: false, refetch: vi.fn() })
    ;(llmProviderApi.discoverModels as any).mockResolvedValue({ data: { models: [] } })
    ;(llmProviderApi.create as any).mockResolvedValue({ data: { provider_id: 'p1' } })
    ;(llmProviderApi.test as any).mockResolvedValue({
      data: { success: true, provider_id: 'p1', error: null },
    })
    ;(llmProviderApi.listModels as any).mockResolvedValue({
      data: { models: ['claude-sonnet-4-5-20250929'] },
    })
    ;(llmProviderApi.update as any).mockResolvedValue({ data: {} })
    ;(llmProviderApi.setDefault as any).mockResolvedValue({ data: {} })
  })

  it('configures the first provider end-to-end and makes it the default', async () => {
    renderSetup()

    // Step 0 — Ollama preselected (first in the list; keyless local default).
    expect(screen.getByRole('radio', { name: /ollama/i })).toBeChecked()
    fireEvent.click(screen.getByRole('button', { name: /next/i }))

    // Step 1 — base URL is seeded for Ollama, so we can test straight away.
    expect(await screen.findByDisplayValue('http://localhost:11434')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /test & continue/i }))

    // Step 2 — connection OK; "set as default" toggle is hidden in onboarding.
    expect(await screen.findByText(/connection ok/i)).toBeInTheDocument()
    expect(screen.queryByLabelText(/set as default/i)).not.toBeInTheDocument()

    // Save — created as ollama, promoted to default, and we land on the app.
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }))
    await waitFor(() => {
      expect(llmProviderApi.create).toHaveBeenCalledWith(
        expect.objectContaining({ provider_type: 'ollama' }),
      )
      expect(llmProviderApi.setDefault).toHaveBeenCalledWith('p1')
      expect(screen.getByText('HOME DASHBOARD')).toBeInTheDocument()
    })
  })

  it('shows an admin-only message when the user cannot configure providers', () => {
    (useAuth as any).mockReturnValue({ hasPermission: () => false })
    renderSetup()
    expect(screen.getByText(/ask an administrator/i)).toBeInTheDocument()
    expect(screen.queryByText(/provider type/i)).not.toBeInTheDocument()
  })
})
