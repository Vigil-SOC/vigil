import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import SetupScreen from './SetupScreen'
import { SETUP_DISMISSED_KEY } from './setupDismissed'

const auth = vi.hoisted(() => ({
  allowed: true,
}))

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ hasPermission: () => auth.allowed }),
}))

vi.mock('../../contexts/ColorSchemeContext', () => ({
  useColorScheme: () => ({ scheme: 'dark', setScheme: vi.fn(), toggleScheme: vi.fn() }),
}))

vi.mock('../../services/api', () => ({
  consoleApi: { getHealth: vi.fn(() => Promise.resolve({ data: { status: 'healthy' } })) },
  storageApi: { getStatus: vi.fn(() => Promise.resolve({ data: { backend: 'none' } })) },
  llmProviderApi: { list: vi.fn(() => Promise.resolve({ data: [] })) },
  workflowApi: { listAll: vi.fn(() => Promise.resolve({ data: { workflows: [] } })) },
  configApi: {
    getOrchestrator: vi.fn(() => Promise.resolve({ data: { profiles: {} } })),
    setOrchestrator: vi.fn(),
    getForceManualApproval: vi.fn(() =>
      Promise.resolve({ data: { enabled: false, environment_wins: false } }),
    ),
    setForceManualApproval: vi.fn(),
    getIntegrations: vi.fn(() => Promise.resolve({ data: {} })),
    getAutonomy: vi.fn(() =>
      Promise.resolve({ data: { auto_response_enabled: false, force_manual_approval: true } }),
    ),
  },
  mcpApi: { listServers: vi.fn(() => Promise.resolve({ data: { servers: [] } })) },
}))

vi.mock('../../services/bifrostApi', () => ({
  anyRoutableBifrostProvider: vi.fn(() => Promise.resolve(false)),
  COMMON_PROVIDERS: ['ollama', 'openai'],
  keyRefusal: vi.fn(),
  secretText: () => '',
  bifrostApi: {
    listProviders: vi.fn(() => Promise.resolve({ data: { providers: [] } })),
    listKeys: vi.fn(),
    routability: vi.fn(() => Promise.resolve({ data: { providers: {} } })),
    createProvider: vi.fn(),
    createKey: vi.fn(),
  },
}))

function renderSetup() {
  return render(
    <MemoryRouter initialEntries={['/setup']}>
      <Routes>
        <Route path="/setup" element={<SetupScreen />} />
        <Route path="/" element={<div>console-home</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('SetupScreen', () => {
  beforeEach(() => {
    localStorage.clear()
    auth.allowed = true
  })

  it('themes the page root from the scheme', () => {
    const { container } = renderSetup()
    expect(container.querySelector('.soc-console')).toHaveClass('vg-dark')
  })

  it('skips the pass onto the console and stays dismissed', () => {
    renderSetup()
    fireEvent.click(screen.getByRole('button', { name: 'Skip setup' }))
    expect(screen.getByText('console-home')).toBeInTheDocument()
    expect(localStorage.getItem(SETUP_DISMISSED_KEY)).toBe('1')
  })

  it('skips a single step without leaving setup', async () => {
    renderSetup()
    fireEvent.click(screen.getByRole('button', { name: 'Skip' }))
    expect(await screen.findByText('No connectable data sources found.')).toBeInTheDocument()
    expect(screen.queryByText('console-home')).not.toBeInTheDocument()
    expect(localStorage.getItem(SETUP_DISMISSED_KEY)).toBeNull()
  })

  it('lets someone who cannot write settings leave for the console', () => {
    auth.allowed = false
    renderSetup()
    fireEvent.click(screen.getByRole('button', { name: /Continue to console/ }))
    expect(screen.getByText('console-home')).toBeInTheDocument()
    expect(localStorage.getItem(SETUP_DISMISSED_KEY)).toBe('1')
  })

  it('opens the summary on the last step and jumps back to a step from Change', async () => {
    renderSetup()
    for (let i = 0; i < 5; i++) fireEvent.click(screen.getByRole('button', { name: 'Skip' }))
    expect(await screen.findByText('Nothing connected yet')).toBeInTheDocument()
    expect(screen.getByText('No provider')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Go to console' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Change Workflows' }))
    expect(await screen.findByText('No workflows yet.')).toBeInTheDocument()
    expect(screen.getByText(/4 of 6/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Change Limits' }))
    expect(await screen.findByRole('button', { name: /Assist/ })).toBeInTheDocument()
    expect(screen.getByText(/5 of 6/)).toBeInTheDocument()
  })
})
