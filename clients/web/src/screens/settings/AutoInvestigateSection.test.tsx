import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import AutoInvestigateSection from './AutoInvestigateSection'
import type { OrchestratorBounds, OrchestratorConfig, Phase } from './useSettings'

vi.mock('./IntentReportCard', () => ({ default: () => null }))

const DEFAULTS: OrchestratorConfig = {
  enabled: false,
  dry_run: false,
  max_concurrent_agents: 3,
  max_iterations_per_agent: 50,
  max_runtime_per_investigation: 3600,
  max_cost_per_investigation: 5,
  max_total_hourly_cost: 20,
  loop_interval: 60,
  stale_threshold: 300,
  workdir_base: 'data/investigations',
}

const bounds: OrchestratorBounds = {
  max_concurrent_agents: { min: 1, max: 10, step: 1 },
  max_iterations_per_agent: { min: 1, max: 500, step: 1 },
  max_runtime_per_investigation: { min: 60, max: 86400, step: 60 },
  max_cost_per_investigation: { min: 0.5, max: 100, step: 0.5 },
  max_total_hourly_cost: { min: 1, max: 500, step: 1 },
  loop_interval: { min: 10, max: 600, step: 10 },
  stale_threshold: { min: 60, max: 86400, step: 60 },
}

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
  phase: 'ready' as Phase,
  save: vi.fn(() => Promise.resolve()),
  setConfig: vi.fn(),
  reload: vi.fn(),
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
      defaults: DEFAULTS,
      bounds,
      profiles,
      status: null,
      phase: state.phase,
      reload: state.reload,
      save: state.save,
    }),
    useForceManualApproval: () => ({
      ...state.approval,
      save: state.approvalSave,
    }),
  }
})

const notify = vi.fn()
const field = (name: string) => screen.getByRole('spinbutton', { name })

describe('Limits and autonomy', () => {
  beforeEach(() => {
    state.config = { ...DEFAULTS }
    state.phase = 'ready'
    state.save.mockClear()
    state.setConfig.mockClear()
    state.reload.mockClear()
    state.approvalSave.mockReset()
    state.approvalSave.mockResolvedValue(undefined as never)
    state.approval = { enabled: true, environment_wins: false, phase: 'ready' }
    notify.mockClear()
  })

  it('shows a loading state, then an error with Retry when the limits cannot be read', () => {
    state.phase = 'loading'
    const view = render(<AutoInvestigateSection notify={notify} />)
    expect(screen.getByText(/Loading limits/)).toBeInTheDocument()

    state.phase = 'error'
    state.config = null
    view.rerender(<AutoInvestigateSection notify={notify} />)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(state.reload).toHaveBeenCalled()
    expect(screen.queryByRole('spinbutton')).not.toBeInTheDocument()
  })

  it('renders profiles with their values from the server, and Custom when nothing matches', () => {
    state.config = { ...DEFAULTS, max_concurrent_agents: 9 }
    render(<AutoInvestigateSection notify={notify} />)

    expect(screen.getByRole('button', { name: /Conservative/ })).toHaveTextContent(
      '2 agents at once · $1 per investigation · $5 per hour',
    )
    expect(screen.getByRole('button', { name: /Balanced/ })).toHaveTextContent('Recommended')
    expect(screen.getByRole('button', { name: /Broad/ })).toHaveTextContent('5 agents at once')
    expect(screen.getByText(/Custom limits in effect/)).toBeInTheDocument()
  })

  it('tags rows Default or Changed against the served defaults, and has no Unlimited switch', () => {
    state.config = { ...DEFAULTS, max_iterations_per_agent: 80 }
    render(<AutoInvestigateSection notify={notify} />)

    expect(field('Budget per case')).toHaveAttribute('aria-valuemin', '0.5')
    expect(field('Budget per case')).toHaveAttribute('aria-valuemax', '100')
    expect(screen.getByText('Changed · default 50')).toBeInTheDocument()
    expect(screen.getAllByText('Default').length).toBeGreaterThan(0)
    expect(screen.queryByText('Unlimited')).not.toBeInTheDocument()
  })

  it('applies a tightening at once and asks before a loosening', async () => {
    const view = render(<AutoInvestigateSection notify={notify} />)

    await act(async () => {
      fireEvent.keyDown(field('Steps per case'), { key: 'ArrowLeft' })
    })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(state.save).toHaveBeenCalledWith({ ...DEFAULTS, max_iterations_per_agent: 49 })

    state.save.mockClear()
    view.rerender(<AutoInvestigateSection notify={notify} />)
    fireEvent.keyDown(field('Steps per case'), { key: 'ArrowRight' })
    expect(state.save).not.toHaveBeenCalled()
    expect(screen.getByRole('dialog', { name: 'Raise investigation limits?' })).toBeInTheDocument()
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    })
    expect(state.save).toHaveBeenCalledWith({ ...DEFAULTS, max_iterations_per_agent: 51 })
  })

  it('confirms once when a profile raises a limit, and saves a lower profile immediately', async () => {
    state.config = { ...DEFAULTS, max_concurrent_agents: 9 }
    const { rerender } = render(<AutoInvestigateSection notify={notify} />)

    fireEvent.click(screen.getByRole('button', { name: /Broad/ }))
    expect(state.save).not.toHaveBeenCalled()
    expect(screen.getByRole('dialog', { name: 'Raise investigation limits?' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(state.save).not.toHaveBeenCalled()

    state.config = { ...DEFAULTS }
    rerender(<AutoInvestigateSection notify={notify} />)
    fireEvent.click(screen.getByRole('button', { name: /Broad/ }))
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    })
    expect(state.save).toHaveBeenCalledTimes(1)

    state.save.mockClear()
    state.config = { ...DEFAULTS }
    rerender(<AutoInvestigateSection notify={notify} />)
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Conservative/ }))
    })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(state.save).toHaveBeenCalledTimes(1)
  })

  it('loads a config saved outside the bounds, flags it, and saves it moved into range', async () => {
    state.config = { ...DEFAULTS, max_concurrent_agents: 0, max_cost_per_investigation: 0 }
    render(<AutoInvestigateSection notify={notify} />)

    expect(screen.getByText(/outside the allowed range/)).toBeInTheDocument()
    expect(screen.getAllByText('Outside the allowed range')).toHaveLength(2)
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Conservative/ }))
    })
    // 0 -> 1 raises the fleet cap, so it asks first; the POST never carries a 0
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    })
    const posted = (state.save.mock.calls[0] as unknown as [OrchestratorConfig])[0]
    expect(posted.max_concurrent_agents).toBe(2)
    expect(posted.max_cost_per_investigation).toBe(1)
  })

  it('writes force_manual_approval only from the middle tool class, and confirms On their own', async () => {
    const view = render(<AutoInvestigateSection notify={notify} />)
    expect(screen.getByText('Read-only tools')).toBeInTheDocument()
    expect(screen.getByText(/This cannot be changed/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'On their own' }))
    expect(state.approvalSave).not.toHaveBeenCalled()
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    })
    expect(state.approvalSave).toHaveBeenCalledWith(false)

    state.approval = { enabled: false, environment_wins: false, phase: 'ready' }
    state.approvalSave.mockClear()
    view.rerender(<AutoInvestigateSection notify={notify} />)
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Ask first' }))
    })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(state.approvalSave).toHaveBeenCalledWith(true)
  })

  it('says the environment wins and does not keep On their own', async () => {
    state.approval = { enabled: false, environment_wins: true, phase: 'ready' }
    state.approvalSave.mockRejectedValue({
      response: { data: { detail: 'The environment wins; Act was not saved.' } },
    })
    render(<AutoInvestigateSection notify={notify} />)

    expect(screen.getByText(/The environment wins/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'On their own' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(state.approvalSave).toHaveBeenCalledWith(false)
    expect(await screen.findByText(/The environment wins/)).toBeInTheDocument()
    expect(notify).toHaveBeenCalledWith('err', 'The environment wins; Act was not saved.')
  })

  it('shows an error, not a default, when the tool setting fails to load', () => {
    state.approval = { enabled: false, environment_wins: false, phase: 'error' }
    render(<AutoInvestigateSection notify={notify} />)

    expect(screen.getByText(/Could not load the tool setting/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'On their own' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Ask first' })).not.toBeInTheDocument()
  })
})
