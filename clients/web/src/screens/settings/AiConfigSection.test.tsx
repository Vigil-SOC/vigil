import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, useNavigate } from 'react-router-dom'
import AiConfigSection from './AiConfigSection'

vi.mock('./AiProvidersPanel', () => ({ default: () => <div>Providers panel</div> }))
vi.mock('./AiModelsPanel', () => ({ default: () => <div>Models panel</div> }))
vi.mock('./AiBudgetsPanel', () => ({ default: () => <div>Virtual Keys panel</div> }))

vi.mock('../../services/api', () => ({
  agentsApi: {
    listCustom: vi.fn(() => Promise.resolve({ data: { agents: [] } })),
    updateCustom: vi.fn(),
  },
}))

vi.mock('./useSettings', () => ({
  AI_OPS_DEFAULTS: {
    local_ollama_recovery_enabled: true,
    local_ollama_recovery_retry_limit: 1,
    local_ollama_recovery_restart_gateway: true,
  },
  useAiOperations: () => ({
    settings: {
      local_ollama_recovery_enabled: true,
      local_ollama_recovery_retry_limit: 1,
      local_ollama_recovery_restart_gateway: true,
    },
    setSettings: vi.fn(),
    phase: 'loading',
    save: vi.fn(),
  }),
  useModelAssignment: () => ({
    components: [],
    assignments: {},
    models: [],
    phase: 'ready',
    error: null,
    reload: vi.fn(),
    assign: vi.fn(),
    clearAssign: vi.fn(),
  }),
}))

function renderAt(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <AiConfigSection notify={vi.fn()} />
    </MemoryRouter>,
  )
}

function GoAssignment() {
  const navigate = useNavigate()
  return (
    <button onClick={() => navigate('/settings?section=ai-config&tab=assignment')}>
      go assignment
    </button>
  )
}

describe('AiConfigSection tab from query', () => {
  it('opens Model Assignment at ?tab=assignment', async () => {
    renderAt('/settings?section=ai-config&tab=assignment')
    expect(screen.getByRole('button', { name: 'Model Assignment' })).toHaveClass('active')
    expect(
      screen.getByText(/Pick a provider \+ model for each system component/),
    ).toBeInTheDocument()
    expect(await screen.findByText('No custom agents')).toBeInTheDocument()
    expect(screen.queryByText('Providers panel')).not.toBeInTheDocument()
  })

  it('opens Providers & Keys when no tab is named', () => {
    renderAt('/settings?section=ai-config')
    expect(screen.getByRole('button', { name: 'Providers & Keys' })).toHaveClass('active')
    expect(screen.getByText('Providers panel')).toBeInTheDocument()
  })

  it('opens Providers & Keys for an unknown tab value', () => {
    renderAt('/settings?section=ai-config&tab=nope')
    expect(screen.getByRole('button', { name: 'Providers & Keys' })).toHaveClass('active')
    expect(screen.getByText('Providers panel')).toBeInTheDocument()
  })

  it('switches tabs when a link is followed while Settings is already open', async () => {
    render(
      <MemoryRouter initialEntries={['/settings?section=ai-config']}>
        <GoAssignment />
        <AiConfigSection notify={vi.fn()} />
      </MemoryRouter>,
    )
    expect(screen.getByText('Providers panel')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'go assignment' }))

    expect(await screen.findByText('No custom agents')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Model Assignment' })).toHaveClass('active')
    expect(
      screen.getByText(/Pick a provider \+ model for each system component/),
    ).toBeInTheDocument()
    expect(screen.queryByText('Providers panel')).not.toBeInTheDocument()
  })
})
