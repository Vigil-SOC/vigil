import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import SetupGate from './SetupGate'
import { SETUP_DISMISSED_KEY } from '../screens/setup/setupDismissed'
import { configApi } from '../services/api'

vi.mock('../services/api', () => ({
  configApi: { getSetupState: vi.fn() },
}))

describe('SetupGate', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.mocked(configApi.getSetupState)
      .mockReset()
      .mockResolvedValue({ data: { furthest: 1, passed: [], finished: false } } as never)
  })

  function renderAtDashboard() {
    return render(
      <MemoryRouter initialEntries={['/dashboard']}>
        <Routes>
          <Route path="/setup" element={<div>setup-route</div>} />
          <Route
            path="/dashboard"
            element={
              <SetupGate>
                <div>console</div>
              </SetupGate>
            }
          />
        </Routes>
      </MemoryRouter>,
    )
  }

  it('sends an unset dismissal to setup, even with no provider check', async () => {
    renderAtDashboard()
    expect(await screen.findByText('setup-route')).toBeInTheDocument()
    expect(screen.queryByText('console')).not.toBeInTheDocument()
  })

  it('leaves a dismissed browser on the console without asking the server', () => {
    localStorage.setItem(SETUP_DISMISSED_KEY, '1')
    renderAtDashboard()
    expect(screen.getByText('console')).toBeInTheDocument()
    expect(screen.queryByText('setup-route')).not.toBeInTheDocument()
    expect(configApi.getSetupState).not.toHaveBeenCalled()
  })

  it('lets a new browser through once an admin finished setup for the install', async () => {
    vi.mocked(configApi.getSetupState).mockResolvedValue({
      data: { furthest: 5, passed: [1, 2, 3, 4, 5], finished: true },
    } as never)
    renderAtDashboard()
    expect(await screen.findByText('console')).toBeInTheDocument()
    expect(localStorage.getItem(SETUP_DISMISSED_KEY)).toBe('1')
  })

  it('sends the operator to setup when the install record cannot be read', async () => {
    vi.mocked(configApi.getSetupState).mockRejectedValue(new Error('down'))
    renderAtDashboard()
    expect(await screen.findByText('setup-route')).toBeInTheDocument()
  })
})
