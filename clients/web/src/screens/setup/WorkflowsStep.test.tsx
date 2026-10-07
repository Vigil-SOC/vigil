import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import WorkflowsStep from './WorkflowsStep'
import { configApi, workflowApi } from '../../services/api'

vi.mock('../../services/api', () => ({
  workflowApi: { listAll: vi.fn(), setEnabled: vi.fn() },
  configApi: { getOrchestrator: vi.fn(), setOrchestrator: vi.fn() },
}))

const workflows = [
  { id: 'triage', name: 'Alert triage', description: 'Every alert', enabled: true, can_disable: false },
  { id: 'phish', name: 'Phishing triage', description: 'Mail alerts', enabled: true, can_disable: true },
  { id: 'hunt', name: 'Hypothesis hunt', description: '', enabled: false, can_disable: true },
]

const orchestrator = {
  enabled: false,
  dry_run: false,
  max_concurrent_agents: 3,
  loop_interval: 60,
  profiles: { balanced: { label: 'Balanced', values: {} } },
}

describe('WorkflowsStep', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    vi.mocked(workflowApi.listAll).mockResolvedValue({ data: { workflows } } as never)
    vi.mocked(workflowApi.setEnabled).mockResolvedValue({ data: {} } as never)
    vi.mocked(configApi.getOrchestrator).mockResolvedValue({ data: orchestrator } as never)
    vi.mocked(configApi.setOrchestrator).mockResolvedValue({ data: {} } as never)
  })

  it('shows a loading line, then a switch per workflow bound to enabled', async () => {
    render(<WorkflowsStep />)
    expect(screen.getByText('Loading workflows…')).toBeInTheDocument()
    expect(await screen.findByRole('switch', { name: 'Phishing triage' })).toBeChecked()
    expect(screen.getByRole('switch', { name: 'Hypothesis hunt' })).not.toBeChecked()
  })

  it('says when the list cannot be read', async () => {
    vi.mocked(workflowApi.listAll).mockRejectedValue(new Error('down'))
    render(<WorkflowsStep />)
    expect(await screen.findByText('Could not read workflows.')).toBeInTheDocument()
  })

  it('saves a switch flip', async () => {
    render(<WorkflowsStep />)
    const hunt = await screen.findByRole('switch', { name: 'Hypothesis hunt' })
    await act(async () => {
      fireEvent.click(hunt)
    })
    expect(workflowApi.setEnabled).toHaveBeenCalledWith('hunt', true)
    expect(screen.getByRole('switch', { name: 'Hypothesis hunt' })).toBeChecked()
  })

  it('puts the switch back and shows the detail when the save fails', async () => {
    vi.mocked(workflowApi.setEnabled).mockRejectedValue({
      response: { data: { detail: 'Nope, in use.' } },
    })
    render(<WorkflowsStep />)
    const target = await screen.findByRole('switch', { name: 'Phishing triage' })
    await act(async () => {
      fireEvent.click(target)
    })
    expect(await screen.findByText('Nope, in use.')).toBeInTheDocument()
    expect(screen.getByRole('switch', { name: 'Phishing triage' })).toBeChecked()
  })

  it('renders a locked workflow on and disabled, with its note', async () => {
    render(<WorkflowsStep />)
    const locked = await screen.findByRole('switch', { name: 'Alert triage' })
    expect(locked).toBeChecked()
    expect(locked).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: /always on/ }))
    expect(screen.getByText('Where alerts land when nothing else fits')).toBeInTheDocument()
  })

  it('saves the automatic-investigation switch on the fresh config, without profiles', async () => {
    render(<WorkflowsStep />)
    const auto = await screen.findByRole('switch', { name: 'Investigate new alerts automatically' })
    expect(auto).not.toBeChecked()
    // Another tab changes a limit after this step opened
    vi.mocked(configApi.getOrchestrator).mockResolvedValue({
      data: { ...orchestrator, max_concurrent_agents: 5 },
    } as never)
    await act(async () => {
      fireEvent.click(auto)
    })
    const saved = vi.mocked(configApi.setOrchestrator).mock.calls[0][0]
    expect(saved).toMatchObject({ enabled: true, max_concurrent_agents: 5 })
    expect(saved).not.toHaveProperty('profiles')
    expect(screen.getByRole('switch', { name: 'Investigate new alerts automatically' })).toBeChecked()
  })

  it('reverts the automatic-investigation switch when saving fails', async () => {
    vi.mocked(configApi.setOrchestrator).mockRejectedValue(new Error('500'))
    render(<WorkflowsStep />)
    const target = await screen.findByRole('switch', { name: 'Investigate new alerts automatically' })
    await act(async () => {
      fireEvent.click(target)
    })
    expect(
      await screen.findByText('Could not save the automatic investigation setting.'),
    ).toBeInTheDocument()
    expect(screen.getByRole('switch', { name: 'Investigate new alerts automatically' })).not.toBeChecked()
  })
})
