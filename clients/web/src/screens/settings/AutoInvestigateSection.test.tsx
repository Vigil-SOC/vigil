import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, within } from '@testing-library/react'
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
  ceilingSave: vi.fn(() => Promise.resolve()),
  stop: vi.fn(() => Promise.resolve()),
  resume: vi.fn(() => Promise.resolve()),
  ceiling: null as Record<string, boolean> | null,
  ceilingPhase: 'ready' as Phase,
  perms: new Set<string>(),
}))

const WRITE = ['ai_autonomy.write', 'ai_autonomy.stop']
vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ hasPermission: (p: string) => state.perms.has(p) }),
}))

const CEILING = {
  act_reversible: false,
  act_irreversible: false,
  stopped: false,
  read: true,
  write: true,
  propose: true,
  environment_locked: false,
  four_eyes: false,
}

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
      phase: state.phase,
      reload: state.reload,
      save: state.save,
    }),
    useCeiling: () => ({
      ceiling: state.ceiling,
      phase: state.ceilingPhase,
      save: state.ceilingSave,
      stop: state.stop,
      resume: state.resume,
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
    state.ceilingSave.mockReset()
    state.ceilingSave.mockResolvedValue(undefined as never)
    state.stop.mockClear()
    state.resume.mockClear()
    state.ceiling = { ...CEILING }
    state.ceilingPhase = 'ready'
    state.perms = new Set(WRITE)
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

    expect(screen.queryByText(/active agent\(s\)/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Conservative/ })).toHaveTextContent('Least spend')
    expect(screen.getByRole('button', { name: /Broad/ })).toHaveTextContent('Widest coverage')
    expect(screen.getByRole('button', { name: /Balanced/ })).not.toHaveTextContent(/Least spend|Widest coverage|Most teams/)

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

  const seg = (group: string, name: string) =>
    within(screen.getByRole('group', { name: group })).getByRole('button', { name })

  it('stores Act behind a confirm, Assist as both act flags off, and irreversible as a third control', async () => {
    const view = render(<AutoInvestigateSection notify={notify} />)
    expect(screen.getByText('On. Never need approval.')).toBeInTheDocument()
    expect(seg('Tools that change something', 'Ask first')).toHaveAttribute('aria-pressed', 'true')

    fireEvent.click(seg('Tools that change something', 'On their own'))
    expect(state.ceilingSave).not.toHaveBeenCalled()
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    })
    expect(state.ceilingSave).toHaveBeenCalledWith({ act_reversible: true })

    state.ceiling = { ...CEILING, act_reversible: true }
    state.ceilingSave.mockClear()
    view.rerender(<AutoInvestigateSection notify={notify} />)
    fireEvent.click(seg('Tools that cannot be undone', 'On their own'))
    expect(screen.getByRole('dialog', { name: /cannot be undone/ })).toBeInTheDocument()
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    })
    expect(state.ceilingSave).toHaveBeenCalledWith({ act_reversible: true, act_irreversible: true })

    state.ceiling = { ...CEILING, act_reversible: true, act_irreversible: true }
    state.ceilingSave.mockClear()
    view.rerender(<AutoInvestigateSection notify={notify} />)
    await act(async () => {
      fireEvent.click(seg('Tools that change something', 'Ask first'))
    })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(state.ceilingSave).toHaveBeenCalledWith({ act_reversible: false, act_irreversible: false })
  })

  it('stops behind a confirm, reloads automatic investigation, and resumes', async () => {
    const view = render(<AutoInvestigateSection notify={notify} />)
    fireEvent.click(screen.getByRole('button', { name: /Stop/ }))
    expect(screen.getByRole('dialog', { name: 'Stop autonomy?' })).toHaveTextContent('marked failed')
    await act(async () => {
      fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Stop' }))
    })
    expect(state.stop).toHaveBeenCalledTimes(1)
    expect(state.reload).toHaveBeenCalled()

    state.ceiling = { ...CEILING, act_reversible: true, stopped: true, write: false, propose: false }
    view.rerender(<AutoInvestigateSection notify={notify} />)
    expect(screen.getByText(/Off while stopped: write, propose/)).toBeInTheDocument()
    expect(screen.getByText(/reads continue/)).toBeInTheDocument()
    // the stored flag stays and stays editable
    expect(seg('Tools that change something', 'On their own')).toHaveAttribute('aria-pressed', 'true')
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Resume/ }))
    })
    expect(state.resume).toHaveBeenCalledTimes(1)
  })

  it('shows both act controls locked by the environment, even when a stored flag is on', () => {
    state.ceiling = { ...CEILING, act_reversible: true, environment_locked: true }
    render(<AutoInvestigateSection notify={notify} />)

    expect(screen.getByText(/The environment wins/)).toBeInTheDocument()
    expect(screen.getAllByText('Locked by the environment')).toHaveLength(2)
    for (const group of ['Tools that change something', 'Tools that cannot be undone']) {
      expect(seg(group, 'On their own')).toBeDisabled()
      expect(seg(group, 'Ask first')).toHaveAttribute('aria-pressed', 'true')
    }
  })

  it('saves four-eyes and says what it enforces', async () => {
    render(<AutoInvestigateSection notify={notify} />)
    expect(screen.getByText(/Nobody approves an action they proposed/)).toBeInTheDocument()
    await act(async () => {
      fireEvent.click(screen.getByRole('switch', { name: 'Four-eyes approval' }))
    })
    expect(state.ceilingSave).toHaveBeenCalledWith({ four_eyes: true })
  })

  it('gives a stop holder Stop and lowering, and no raise, four-eyes or Resume', async () => {
    state.perms = new Set(['ai_autonomy.stop'])
    state.ceiling = { ...CEILING, act_reversible: true }
    const view = render(<AutoInvestigateSection notify={notify} />)
    expect(screen.queryByRole('group', { name: 'Tools that change something' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /On their own/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('switch', { name: 'Four-eyes approval' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Stop/ })).toBeInTheDocument()
    // only the flag that is on offers to switch off
    expect(screen.getAllByRole('button', { name: 'Switch to Ask first' })).toHaveLength(1)
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Switch to Ask first' }))
    })
    expect(state.ceilingSave).toHaveBeenCalledWith({ act_reversible: false, act_irreversible: false })

    state.ceiling = { ...CEILING, act_reversible: true, stopped: true }
    view.rerender(<AutoInvestigateSection notify={notify} />)
    expect(screen.queryByRole('button', { name: /Resume/ })).not.toBeInTheDocument()
  })

  it('shows status only to a role with neither write nor stop', () => {
    state.perms = new Set()
    state.ceiling = { ...CEILING, act_reversible: true, four_eyes: true }
    render(<AutoInvestigateSection notify={notify} />)
    expect(screen.getByText('On their own.')).toBeInTheDocument()
    expect(screen.getByText('Ask first.')).toBeInTheDocument()
    expect(screen.getByText('On')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Stop|Resume|Ask first|On their own/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('switch', { name: 'Four-eyes approval' })).not.toBeInTheDocument()
  })

  it('shows an error, not a default, when the ceiling fails to load', () => {
    state.ceiling = null
    state.ceilingPhase = 'error'
    render(<AutoInvestigateSection notify={notify} />)

    expect(screen.getByText(/Could not load the autonomy ceiling/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /On their own/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Stop/ })).not.toBeInTheDocument()
  })
})
