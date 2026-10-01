import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import AutoInvestigateSection from './AutoInvestigateSection'
import { ORCHESTRATOR_DEFAULTS, type OrchestratorConfig, type Phase } from './useSettings'

vi.mock('./IntentReportCard', () => ({ default: () => null }))

const profiles = {
  conservative: {
    label: 'Conservative',
    recommended: false,
    values: {
      max_concurrent_agents: 2,
      max_iterations_per_agent: 25,
      max_runtime_per_investigation: 1800,
      max_cost_per_investigation: 1,
      max_total_hourly_cost: 5,
    },
  },
  balanced: {
    label: 'Balanced',
    recommended: true,
    values: {
      max_concurrent_agents: 3,
      max_iterations_per_agent: 50,
      max_runtime_per_investigation: 3600,
      max_cost_per_investigation: 5,
      max_total_hourly_cost: 20,
    },
  },
  aggressive: {
    label: 'Broad',
    recommended: false,
    values: {
      max_concurrent_agents: 5,
      max_iterations_per_agent: 100,
      max_runtime_per_investigation: 7200,
      max_cost_per_investigation: 15,
      max_total_hourly_cost: 60,
    },
  },
}

const state = vi.hoisted(() => ({
  config: null as OrchestratorConfig | null,
  save: vi.fn(() => Promise.resolve()),
  setConfig: vi.fn(),
  approvalSave: vi.fn(() => Promise.resolve()),
  approval: { enabled: true, environment_wins: false, phase: 'ready' as Phase },
}))

vi.mock('./useSettings', async () => {
  const actual = await vi.importActual<typeof import('./useSettings')>('./useSettings')
  return {
    ...actual,
    useOrchestrator: () => ({
      config: state.config,
      setConfig: state.setConfig,
      profiles,
      status: null,
      phase: 'ready' as const,
      save: state.save,
    }),
    useForceManualApproval: () => ({
      ...state.approval,
      save: state.approvalSave,
    }),
  }
})

const notify = vi.fn()

describe('Auto Investigate profiles and approval', () => {
  beforeEach(() => {
    state.config = { ...ORCHESTRATOR_DEFAULTS }
    state.save.mockClear()
    state.setConfig.mockClear()
    state.approvalSave.mockReset()
    state.approvalSave.mockResolvedValue(undefined as never)
    state.approval = { enabled: true, environment_wins: false, phase: 'ready' }
    notify.mockClear()
  })

  it('renders profiles from the hook, with Balanced recommended and custom when nothing matches', () => {
    state.config = { ...ORCHESTRATOR_DEFAULTS, max_concurrent_agents: 9 }
    render(<AutoInvestigateSection notify={notify} />)

    expect(screen.getByRole('button', { name: /Conservative/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Balanced/ })).toHaveTextContent('Recommended')
    expect(screen.getByRole('button', { name: /Broad/ })).toBeInTheDocument()
    expect(screen.getByText(/Custom limits in effect/)).toBeInTheDocument()
  })

  it('confirms once when a profile raises a limit, and saves a lower profile immediately', async () => {
    state.config = { ...ORCHESTRATOR_DEFAULTS, max_concurrent_agents: 9 }
    const { rerender } = render(<AutoInvestigateSection notify={notify} />)

    fireEvent.click(screen.getByRole('button', { name: /Broad/ }))
    expect(state.save).not.toHaveBeenCalled()
    expect(screen.getByRole('dialog', { name: 'Raise investigation limits?' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(state.save).not.toHaveBeenCalled()

    state.config = { ...ORCHESTRATOR_DEFAULTS }
    rerender(<AutoInvestigateSection notify={notify} />)
    fireEvent.click(screen.getByRole('button', { name: /Broad/ }))
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    })
    expect(state.save).toHaveBeenCalledTimes(1)

    state.save.mockClear()
    state.config = { ...ORCHESTRATOR_DEFAULTS }
    rerender(<AutoInvestigateSection notify={notify} />)
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Conservative/ }))
    })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(state.save).toHaveBeenCalledTimes(1)
  })

  it('confirms switching to Act, and saves Assist immediately', async () => {
    const view = render(<AutoInvestigateSection notify={notify} />)
    fireEvent.click(screen.getByRole('button', { name: /^Act/ }))
    expect(state.approvalSave).not.toHaveBeenCalled()
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    })
    expect(state.approvalSave).toHaveBeenCalledWith(false)

    state.approval = { enabled: false, environment_wins: false, phase: 'ready' }
    state.approvalSave.mockClear()
    view.rerender(<AutoInvestigateSection notify={notify} />)
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /^Assist/ }))
    })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(state.approvalSave).toHaveBeenCalledWith(true)
  })

  it('says the environment wins and does not keep Act', async () => {
    state.approval = { enabled: false, environment_wins: true, phase: 'ready' }
    state.approvalSave.mockRejectedValue({
      response: { data: { detail: 'The environment wins; Act was not saved.' } },
    })
    render(<AutoInvestigateSection notify={notify} />)

    expect(screen.getByText(/The environment wins/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /^Act/ }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(state.approvalSave).toHaveBeenCalledWith(false)
    expect(await screen.findByText(/The environment wins/)).toBeInTheDocument()
    expect(notify).toHaveBeenCalledWith('err', 'The environment wins; Act was not saved.')
  })

  it('shows an error, not Act, when the response mode fails to load', () => {
    state.approval = { enabled: false, environment_wins: false, phase: 'error' }
    render(<AutoInvestigateSection notify={notify} />)

    expect(screen.getByText(/Could not load the response mode/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Act/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Assist/ })).not.toBeInTheDocument()
  })
})
