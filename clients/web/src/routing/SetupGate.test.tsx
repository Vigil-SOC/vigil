import { beforeEach, describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import SetupGate from './SetupGate'
import { SETUP_DISMISSED_KEY } from '../screens/setup/setupDismissed'

describe('SetupGate', () => {
  beforeEach(() => {
    localStorage.clear()
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

  it('sends an unset dismissal to setup, even with no provider check', () => {
    renderAtDashboard()
    expect(screen.getByText('setup-route')).toBeInTheDocument()
    expect(screen.queryByText('console')).not.toBeInTheDocument()
  })

  it('leaves a dismissed install on the console', () => {
    localStorage.setItem(SETUP_DISMISSED_KEY, '1')
    renderAtDashboard()
    expect(screen.getByText('console')).toBeInTheDocument()
    expect(screen.queryByText('setup-route')).not.toBeInTheDocument()
  })
})
