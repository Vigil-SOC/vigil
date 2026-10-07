import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import WorkflowsStep from './WorkflowsStep'
import { configApi, workflowApi } from '../../services/api'

vi.mock('../../services/api', () => ({
  workflowApi: { listAll: vi.fn(), setEnabled: vi.fn() },
  configApi: {
    getOrchestrator: vi.fn(),
    setOrchestrator: vi.fn(),
    getForceManualApproval: vi.fn(),
    setForceManualApproval: vi.fn(),
  },
}))

const workflows = [
  { id: 'triage', name: 'Alert triage', description: 'Every alert', enabled: true, can_disable: false, triggers: ['alerts'] },
  { id: 'phish', name: 'Phishing triage', description: 'Mail alerts', enabled: true, can_disable: true, triggers: [] },
  { id: 'hunt', name: 'Hypothesis hunt', description: '', enabled: false, can_disable: true, triggers: ['schedule'] },
]

const renderStep = () =>
  render(
    <MemoryRouter>
      <WorkflowsStep />
    </MemoryRouter>,
  )

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
    vi.mocked(configApi.getForceManualApproval).mockResolvedValue({
      data: { enabled: false, environment_wins: false },
    } as never)
    vi.mocked(configApi.setForceManualApproval).mockImplementation(async (enabled: boolean) => ({
      data: { enabled, environment_wins: false },
    }) as never)
  })

  it('shows a loading line, then a switch per workflow bound to enabled', async () => {
    renderStep()
    expect(screen.getByText('Loading workflows…')).toBeInTheDocument()
    expect(await screen.findByRole('switch', { name: 'Phishing triage' })).toBeChecked()
    expect(screen.getByRole('switch', { name: 'Hypothesis hunt' })).not.toBeChecked()
  })

  it('says when the list cannot be read', async () => {
    vi.mocked(workflowApi.listAll).mockRejectedValue(new Error('down'))
    renderStep()
    expect(await screen.findByText('Could not read workflows.')).toBeInTheDocument()
  })

  it('saves a switch flip', async () => {
    renderStep()
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
    renderStep()
    const target = await screen.findByRole('switch', { name: 'Phishing triage' })
    await act(async () => {
      fireEvent.click(target)
    })
    expect(await screen.findByText('Nope, in use.')).toBeInTheDocument()
    expect(screen.getByRole('switch', { name: 'Phishing triage' })).toBeChecked()
  })

  it('shows when each workflow runs, and nothing when the API sends no triggers', async () => {
    renderStep()
    await screen.findByRole('switch', { name: 'Phishing triage' })
    expect(screen.getByText('On alerts')).toBeInTheDocument()
    expect(screen.getByText('Started by hand')).toBeInTheDocument()
    expect(screen.getByText('On a schedule')).toBeInTheDocument()
  })

  it('shows nothing under When it runs for a backend that sends no triggers', async () => {
    vi.mocked(workflowApi.listAll).mockResolvedValue({
      data: { workflows: [{ id: 'phish', name: 'Phishing triage', enabled: true, can_disable: true }] },
    } as never)
    renderStep()
    await screen.findByRole('switch', { name: 'Phishing triage' })
    expect(screen.queryByText('Started by hand')).not.toBeInTheDocument()
  })

  it('renders a locked workflow as Always on with no switch, and its note', async () => {
    renderStep()
    await screen.findByRole('switch', { name: 'Phishing triage' })
    expect(screen.queryByRole('switch', { name: 'Alert triage' })).not.toBeInTheDocument()
    expect(screen.getByText('Always on')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /always on/ }))
    expect(screen.getByText('Where alerts land when nothing else fits')).toBeInTheDocument()
  })

  it('saves the automatic-investigation switch on the fresh config, without profiles', async () => {
    renderStep()
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
    renderStep()
    const target = await screen.findByRole('switch', { name: 'Investigate new alerts automatically' })
    await act(async () => {
      fireEvent.click(target)
    })
    expect(
      await screen.findByText('Could not save the automatic investigation setting.'),
    ).toBeInTheDocument()
    expect(screen.getByRole('switch', { name: 'Investigate new alerts automatically' })).not.toBeChecked()
  })

  it('says there are no workflows when the list is empty', async () => {
    vi.mocked(workflowApi.listAll).mockResolvedValue({ data: { workflows: [] } } as never)
    renderStep()
    expect(await screen.findByText('No workflows yet.')).toBeInTheDocument()
  })

  it('links to the console workflows route', async () => {
    renderStep()
    const link = await screen.findByRole('link', { name: /See how an investigation runs/ })
    expect(link).toHaveAttribute('href', '/workflows')
  })

  it('saves Assist immediately and Act only after confirm', async () => {
    vi.mocked(configApi.getForceManualApproval).mockResolvedValue({
      data: { enabled: true, environment_wins: false },
    } as never)
    renderStep()
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

  it('selects Act when manual approval is off, without saving anything', async () => {
    renderStep()
    expect(await screen.findByRole('button', { name: /Act/ })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByText('Recommended')).toBeInTheDocument()
    expect(configApi.setForceManualApproval).not.toHaveBeenCalled()
  })

  it('says the environment wins and leaves Act unsaved on 409', async () => {
    vi.mocked(configApi.getForceManualApproval).mockResolvedValue({
      data: { enabled: true, environment_wins: true },
    } as never)
    vi.mocked(configApi.setForceManualApproval).mockRejectedValue({
      response: { data: { detail: 'The environment wins; Act was not saved.' } },
    })
    renderStep()
    expect(await screen.findByText(/The environment wins\. Act cannot/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Assist/ })).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(screen.getByRole('button', { name: /Act/ }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(configApi.setForceManualApproval).toHaveBeenCalledWith(false)
    expect(await screen.findByText('The environment wins; Act was not saved.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Assist/ })).toHaveAttribute('aria-pressed', 'true')
    expect(configApi.setOrchestrator).not.toHaveBeenCalled()
  })

  it('says so when the response mode cannot be read, and keeps the workflows', async () => {
    vi.mocked(configApi.getForceManualApproval).mockRejectedValue(new Error('down'))
    renderStep()
    expect(await screen.findByText('Could not read the response mode.')).toBeInTheDocument()
    expect(await screen.findByRole('switch', { name: 'Phishing triage' })).toBeInTheDocument()
  })
})
