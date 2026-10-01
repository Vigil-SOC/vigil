import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import LimitsStep from './LimitsStep'
import { configApi } from '../../services/api'

const profiles = {
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
}

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
    vi.mocked(configApi.getOrchestrator).mockResolvedValue({ data: { profiles } } as never)
    vi.mocked(configApi.setOrchestrator).mockResolvedValue({ data: {} } as never)
    vi.mocked(configApi.getForceManualApproval).mockResolvedValue({
      data: { enabled: false, environment_wins: false },
    } as never)
    vi.mocked(configApi.setForceManualApproval).mockImplementation(async (enabled: boolean) => ({
      data: { enabled, environment_wins: false },
    }) as never)
  })

  it('shows profile labels and values and preselects Act when the flag is false', async () => {
    render(<LimitsStep />)
    expect(await screen.findByText('Balanced')).toBeInTheDocument()
    expect(screen.getByText('Max cost per investigation')).toBeInTheDocument()
    expect(screen.getByText('5')).toBeInTheDocument()
    expect(screen.getByText('3600')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Act/ })).toHaveAttribute('aria-pressed', 'true')
    expect(configApi.setOrchestrator).not.toHaveBeenCalled()
    expect(configApi.setForceManualApproval).not.toHaveBeenCalled()
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
