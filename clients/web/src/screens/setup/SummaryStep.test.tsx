import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import SummaryStep from './SummaryStep'
import { configApi, llmProviderApi, workflowApi } from '../../services/api'

vi.mock('../../services/api', () => ({
  configApi: {
    getIntegrations: vi.fn(),
    getAutonomy: vi.fn(),
    getOrchestrator: vi.fn(),
  },
  llmProviderApi: { list: vi.fn() },
  workflowApi: { listAll: vi.fn() },
}))

const values = {
  max_concurrent_agents: 3,
  max_iterations_per_agent: 50,
  max_runtime_per_investigation: 3600,
  max_cost_per_investigation: 5,
  max_total_hourly_cost: 20,
}

describe('SummaryStep', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    vi.mocked(configApi.getIntegrations).mockResolvedValue({
      data: { enabled_integrations: ['splunk', 'crowdstrike'] },
    } as never)
    vi.mocked(llmProviderApi.list).mockResolvedValue({
      data: [
        { name: 'Other', is_default: false, default_model: 'x' },
        { name: 'Anthropic', is_default: true, default_model: 'claude-sonnet-5-5' },
      ],
    } as never)
    vi.mocked(workflowApi.listAll).mockResolvedValue({
      data: { workflows: [{ enabled: true }, { enabled: true }, { enabled: false }] },
    } as never)
    vi.mocked(configApi.getAutonomy).mockResolvedValue({
      data: { auto_response_enabled: true, force_manual_approval: false },
    } as never)
    vi.mocked(configApi.getOrchestrator).mockResolvedValue({
      data: { enabled: true, ...values, profiles: { balanced: { label: 'Balanced', values } } },
    } as never)
  })

  it('recaps each step from its own API', async () => {
    render(<SummaryStep onChange={vi.fn()} />)
    expect(screen.getAllByText('Loading…')).toHaveLength(5)
    expect(await screen.findByText('splunk, crowdstrike')).toBeInTheDocument()
    expect(screen.getByText('Anthropic · claude-sonnet-5-5')).toBeInTheDocument()
    expect(screen.getByText('2 of 3 on · investigates new alerts automatically')).toBeInTheDocument()
    expect(screen.getByText('Act · reversible changes on its own')).toBeInTheDocument()
    expect(screen.getByText('Balanced')).toBeInTheDocument()
  })

  it('reads the empty and off cases', async () => {
    vi.mocked(configApi.getIntegrations).mockResolvedValue({ data: { enabled_integrations: [] } } as never)
    vi.mocked(llmProviderApi.list).mockResolvedValue({ data: [] } as never)
    vi.mocked(configApi.getAutonomy).mockResolvedValue({
      data: { auto_response_enabled: false, force_manual_approval: true },
    } as never)
    vi.mocked(configApi.getOrchestrator).mockResolvedValue({
      data: { enabled: false, ...values, max_concurrent_agents: 9, profiles: { balanced: { label: 'Balanced', values } } },
    } as never)
    render(<SummaryStep onChange={vi.fn()} />)
    expect(await screen.findByText('Nothing connected yet')).toBeInTheDocument()
    expect(screen.getByText('No provider')).toBeInTheDocument()
    expect(screen.getByText('Assist · asks before changes')).toBeInTheDocument()
    expect(await screen.findByText('2 of 3 on · starts only when asked')).toBeInTheDocument()
    expect(screen.getByText('Custom')).toBeInTheDocument()
  })

  it('shows a short error in the failing row and keeps the others', async () => {
    vi.mocked(llmProviderApi.list).mockRejectedValue(new Error('down'))
    render(<SummaryStep onChange={vi.fn()} />)
    expect(await screen.findByText('Could not read this.')).toBeInTheDocument()
    expect(await screen.findByText('splunk, crowdstrike')).toBeInTheDocument()
    expect(screen.getByText('Balanced')).toBeInTheDocument()
  })

  it('jumps to the step a Change link belongs to', async () => {
    const onChange = vi.fn()
    render(<SummaryStep onChange={onChange} />)
    await screen.findByText('Balanced')
    for (const [name, target] of [
      ['Change Data', 'data'],
      ['Change AI', 'ai'],
      ['Change Workflows', 'workflows'],
      ['Change On their own', 'workflows'],
      ['Change Limits', 'limits'],
    ]) {
      fireEvent.click(screen.getByRole('button', { name }))
      expect(onChange).toHaveBeenLastCalledWith(target)
    }
  })
})
