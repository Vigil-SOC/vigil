import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import LimitsStep from './LimitsStep'
import { configApi } from '../../services/api'

const balancedValues = {
  max_concurrent_agents: 3,
  max_iterations_per_agent: 50,
  max_runtime_per_investigation: 3600,
  max_cost_per_investigation: 5,
  max_total_hourly_cost: 20,
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
  balanced: { label: 'Balanced', recommended: true, values: balancedValues },
  broad: {
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
// what the API sends: the saved flat config plus the profiles
const stored = { enabled: true, dry_run: false, loop_interval: 60, ...balancedValues }

vi.mock('../../services/api', () => ({
  configApi: {
    getOrchestrator: vi.fn(),
    setOrchestrator: vi.fn(),
    getForceManualApproval: vi.fn(),
    setForceManualApproval: vi.fn(),
  },
}))

describe('LimitsStep', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(configApi.getOrchestrator).mockResolvedValue({ data: { ...stored, profiles } } as never)
    vi.mocked(configApi.setOrchestrator).mockResolvedValue({ data: {} } as never)
    vi.mocked(configApi.getForceManualApproval).mockResolvedValue({
      data: { enabled: false, environment_wins: false },
    } as never)
    vi.mocked(configApi.setForceManualApproval).mockImplementation(async (enabled: boolean) => ({
      data: { enabled, environment_wins: false },
    }) as never)
  })

  it('selects the profile the saved config matches and lists its limits with units', async () => {
    render(<LimitsStep />)
    expect(await screen.findByRole('radio', { name: /Balanced/ })).toBeChecked()
    expect(screen.getByRole('radio', { name: /Conservative/ })).not.toBeChecked()
    expect(screen.getByText('Default case limits')).toBeInTheDocument()
    expect(screen.getByText('$5.00')).toBeInTheDocument()
    expect(screen.getByText('1 h')).toBeInTheDocument()
    expect(screen.getByText('$20.00 / h')).toBeInTheDocument()
    expect(screen.getAllByText('Recommended')).toHaveLength(2) // Balanced and Act
    expect(screen.getByRole('button', { name: /Act/ })).toHaveAttribute('aria-pressed', 'true')
    expect(configApi.setOrchestrator).not.toHaveBeenCalled()
    expect(configApi.setForceManualApproval).not.toHaveBeenCalled()
  })

  it('saves a lower profile at once, merged into the current config', async () => {
    render(<LimitsStep />)
    const target = await screen.findByRole('radio', { name: /Conservative/ })
    await act(async () => {
      fireEvent.click(target)
    })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    const saved = vi.mocked(configApi.setOrchestrator).mock.calls[0][0]
    expect(saved).toMatchObject({ enabled: true, loop_interval: 60, max_concurrent_agents: 2 })
    expect(saved).not.toHaveProperty('profiles')
    expect(screen.getByRole('radio', { name: /Conservative/ })).toBeChecked()
    expect(screen.getByText('$1.00')).toBeInTheDocument()
  })

  it('confirms before a pick that raises a limit, and saves nothing on cancel', async () => {
    render(<LimitsStep />)
    fireEvent.click(await screen.findByRole('radio', { name: /Broad/ }))
    expect(screen.getByText('Raise investigation limits?')).toBeInTheDocument()
    expect(configApi.setOrchestrator).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(configApi.setOrchestrator).not.toHaveBeenCalled()
    expect(screen.getByRole('radio', { name: /Balanced/ })).toBeChecked()

    fireEvent.click(screen.getByRole('radio', { name: /Broad/ }))
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    })
    expect(configApi.setOrchestrator).toHaveBeenCalledWith(
      expect.objectContaining({ max_concurrent_agents: 5, max_total_hourly_cost: 60 }),
    )
    expect(screen.getByRole('radio', { name: /Broad/ })).toBeChecked()
  })

  it('keeps a failed profile save unselected and shows the detail', async () => {
    vi.mocked(configApi.setOrchestrator).mockRejectedValue({
      response: { data: { detail: 'Storage is read-only.' } },
    })
    render(<LimitsStep />)
    const target = await screen.findByRole('radio', { name: /Conservative/ })
    await act(async () => {
      fireEvent.click(target)
    })
    expect(await screen.findByText('Storage is read-only.')).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: /Balanced/ })).toBeChecked()
  })

  it('reads Custom limits in effect, selects no card, and shows the saved values', async () => {
    vi.mocked(configApi.getOrchestrator).mockResolvedValue({
      data: { ...stored, max_concurrent_agents: 4, profiles },
    } as never)
    render(<LimitsStep />)
    expect(await screen.findByText(/Custom limits in effect/)).toBeInTheDocument()
    for (const radio of screen.getAllByRole('radio')) expect(radio).not.toBeChecked()
    expect(screen.getByText('4')).toBeInTheDocument()
  })

  it('says so when the profiles cannot be read', async () => {
    vi.mocked(configApi.getOrchestrator).mockRejectedValue(new Error('down'))
    render(<LimitsStep />)
    expect(await screen.findByText('Could not read investigation profiles.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Assist/ })).toBeInTheDocument()
  })

  it('shows a loading line first', () => {
    render(<LimitsStep />)
    expect(screen.getByText('Loading limits…')).toBeInTheDocument()
  })

  it('saves Assist immediately and Act only after confirm', async () => {
    vi.mocked(configApi.getForceManualApproval).mockResolvedValue({
      data: { enabled: true, environment_wins: false },
    } as never)
    render(<LimitsStep />)
    const assist = await screen.findByRole('button', { name: /Assist/ })
    expect(assist).toHaveAttribute('aria-pressed', 'true')

    fireEvent.click(screen.getByRole('button', { name: /Act/ }))
    expect(configApi.setForceManualApproval).not.toHaveBeenCalled()
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    })
    expect(configApi.setForceManualApproval).toHaveBeenCalledWith(false)
    expect(configApi.setOrchestrator).not.toHaveBeenCalled()

    vi.mocked(configApi.setForceManualApproval).mockClear()
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Assist/ }))
    })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(configApi.setForceManualApproval).toHaveBeenCalledWith(true)
  })

  it('says the environment wins and leaves Act unsaved on 409', async () => {
    vi.mocked(configApi.getForceManualApproval).mockResolvedValue({
      data: { enabled: true, environment_wins: true },
    } as never)
    vi.mocked(configApi.setForceManualApproval).mockRejectedValue({
      response: { data: { detail: 'The environment wins; Act was not saved.' } },
    })
    render(<LimitsStep />)
    expect(await screen.findByText(/The environment wins/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Assist/ })).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(screen.getByRole('button', { name: /Act/ }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(configApi.setForceManualApproval).toHaveBeenCalledWith(false)
    expect(await screen.findByText('The environment wins; Act was not saved.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Assist/ })).toHaveAttribute('aria-pressed', 'true')
    expect(configApi.setOrchestrator).not.toHaveBeenCalled()
  })
})
