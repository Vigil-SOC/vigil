import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import LimitsStep from './LimitsStep'
import { budgetsApi, configApi } from '../../services/api'
import { bifrostApi } from '../../services/bifrostApi'

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
    getIntegrations: vi.fn(),
    setIntegrations: vi.fn(),
  },
  budgetsApi: { getQuota: vi.fn() },
}))

vi.mock('../../services/bifrostApi', () => ({
  bifrostApi: { listVirtualKeys: vi.fn(), updateVirtualKey: vi.fn() },
}))

// the masked secret the gateway lists; it must never be written back
const MASKED = 'sk-bf-****-9f3d'
const vk = (over: Record<string, unknown> = {}) => ({
  id: 'vk1',
  name: 'vigil-soc',
  is_active: true,
  value: MASKED,
  budgets: [{ id: 'b1', max_limit: 100, reset_duration: '1M', current_usage: 12 }],
  ...over,
})
const quota = (name = 'vigil-soc') => ({
  data: { configured: true, available: true, virtual_key_id: 'sk-bf-secret', quota: { virtual_key_name: name } },
})
const gatewayHolds = (...keys: unknown[]) =>
  vi.mocked(bifrostApi.listVirtualKeys).mockResolvedValue({ data: { virtual_keys: keys } } as never)
const integrations = (secrets: Record<string, Record<string, boolean>> = {}, extra: object = {}) => ({
  data: { enabled_integrations: ['splunk'], integrations: { splunk: { url: 'u' } }, secrets_set: secrets, ...extra },
})

describe('LimitsStep', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(configApi.getOrchestrator).mockResolvedValue({ data: { ...stored, profiles } } as never)
    vi.mocked(configApi.setOrchestrator).mockResolvedValue({ data: {} } as never)
    vi.mocked(budgetsApi.getQuota).mockResolvedValue(quota() as never)
    gatewayHolds(vk())
    vi.mocked(bifrostApi.updateVirtualKey).mockResolvedValue({ data: {} } as never)
    vi.mocked(configApi.getIntegrations).mockResolvedValue(integrations() as never)
    vi.mocked(configApi.setIntegrations).mockResolvedValue({ data: {} } as never)
  })

  it('words each card from the served values and keeps the limits behind "Show the limits"', async () => {
    render(<LimitsStep />)
    expect(await screen.findByRole('button', { name: /Balanced/ })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: /Conservative/ })).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByText('2 agents at once, $1.00 per investigation')).toBeInTheDocument()
    expect(screen.getByText('3 agents at once, $5.00 per investigation')).toBeInTheDocument()
    expect(screen.getByText('5 agents at once, $15.00 per investigation')).toBeInTheDocument()
    expect(screen.getAllByText('Recommended')).toHaveLength(1)
    expect(screen.getByText(/Vigil stops an investigation that reaches its limits/)).toBeInTheDocument()
    // the gateway checks the ceiling, not the profile limits
    expect(screen.getAllByText(/model gateway checks it before every call/)).toHaveLength(1)
    expect(screen.queryByText('$20.00 / h')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Show the limits' }))
    expect(screen.getByText('$20.00 / h')).toBeInTheDocument()
    expect(screen.getByText('1 h')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Assist|Act/ })).not.toBeInTheDocument()
    expect(configApi.setOrchestrator).not.toHaveBeenCalled()
  })

  it('saves a lower profile at once, merged into the current config', async () => {
    render(<LimitsStep />)
    const target = await screen.findByRole('button', { name: /Conservative/ })
    await act(async () => {
      fireEvent.click(target)
    })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    const saved = vi.mocked(configApi.setOrchestrator).mock.calls[0][0]
    expect(saved).toMatchObject({ enabled: true, loop_interval: 60, max_concurrent_agents: 2 })
    expect(saved).not.toHaveProperty('profiles')
    expect(screen.getByRole('button', { name: /Conservative/ })).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(screen.getByRole('button', { name: 'Show the limits' }))
    expect(screen.getByText('$5.00 / h')).toBeInTheDocument()
    expect(screen.getByText('30 min')).toBeInTheDocument()
  })

  it('confirms before a pick that raises a limit, and saves nothing on cancel', async () => {
    render(<LimitsStep />)
    fireEvent.click(await screen.findByRole('button', { name: /Broad/ }))
    expect(screen.getByText('Raise investigation limits?')).toBeInTheDocument()
    expect(configApi.setOrchestrator).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(configApi.setOrchestrator).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /Balanced/ })).toHaveAttribute('aria-pressed', 'true')

    fireEvent.click(screen.getByRole('button', { name: /Broad/ }))
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    })
    expect(configApi.setOrchestrator).toHaveBeenCalledWith(
      expect.objectContaining({ max_concurrent_agents: 5, max_total_hourly_cost: 60 }),
    )
    expect(screen.getByRole('button', { name: /Broad/ })).toHaveAttribute('aria-pressed', 'true')
  })

  it('keeps a failed profile save unselected and shows the detail', async () => {
    vi.mocked(configApi.setOrchestrator).mockRejectedValue({
      response: { data: { detail: 'Storage is read-only.' } },
    })
    render(<LimitsStep />)
    const target = await screen.findByRole('button', { name: /Conservative/ })
    await act(async () => {
      fireEvent.click(target)
    })
    expect(await screen.findByText('Storage is read-only.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Balanced/ })).toHaveAttribute('aria-pressed', 'true')
  })

  it('reads Custom limits in effect, selects no card, and shows the saved values', async () => {
    vi.mocked(configApi.getOrchestrator).mockResolvedValue({
      data: { ...stored, max_concurrent_agents: 4, profiles },
    } as never)
    render(<LimitsStep />)
    expect(await screen.findByText(/Custom limits in effect/)).toBeInTheDocument()
    for (const name of [/Conservative/, /Balanced/, /Broad/])
      expect(screen.getByRole('button', { name })).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(screen.getByRole('button', { name: 'Show the limits' }))
    expect(screen.getByText('4')).toBeInTheDocument()
  })

  it('says so when the profiles cannot be read', async () => {
    vi.mocked(configApi.getOrchestrator).mockRejectedValue(new Error('down'))
    render(<LimitsStep />)
    expect(await screen.findByText('Could not read investigation profiles.')).toBeInTheDocument()
  })

  it('shows a loading line first', () => {
    render(<LimitsStep />)
    expect(screen.getByText('Loading limits…')).toBeInTheDocument()
  })
  describe('monthly ceiling', () => {
    const field = () => screen.findByRole('spinbutton', { name: 'Monthly ceiling' })

    it('reads the default key by the quota name, not by its masked value', async () => {
      gatewayHolds(vk({ id: 'other', name: 'other', budgets: [{ max_limit: 7, reset_duration: '1M' }] }), vk())
      render(<LimitsStep />)
      expect(await field()).toHaveValue(100)
      expect(screen.getByText('Estimated monthly spend: Not measured yet')).toBeInTheDocument()
      expect(screen.queryByText(/Resets/)).not.toBeInTheDocument()
    })

    it('saves a lower ceiling at once, keeping the reset period and never writing the masked value', async () => {
      render(<LimitsStep />)
      fireEvent.change(await field(), { target: { value: '40' } })
      gatewayHolds(vk({ budgets: [{ id: 'b1', max_limit: 40, reset_duration: '1M' }] }))
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Save ceiling' }))
      })
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
      expect(bifrostApi.updateVirtualKey).toHaveBeenCalledWith('vk1', {
        name: 'vigil-soc',
        budgets: [{ max_limit: 40, reset_duration: '1M' }],
      })
      expect(JSON.stringify(vi.mocked(bifrostApi.updateVirtualKey).mock.calls)).not.toContain(MASKED)
      // the field shows what the gateway holds after the reload
      expect(await field()).toHaveValue(40)
    })

    it('confirms a raise, and treats a cleared field as one that removes the ceiling', async () => {
      render(<LimitsStep />)
      fireEvent.change(await field(), { target: { value: '250' } })
      fireEvent.click(screen.getByRole('button', { name: 'Save ceiling' }))
      expect(screen.getByText('Raise the monthly ceiling?')).toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
      expect(bifrostApi.updateVirtualKey).not.toHaveBeenCalled()

      fireEvent.change(screen.getByRole('spinbutton', { name: 'Monthly ceiling' }), { target: { value: '' } })
      fireEvent.click(screen.getByRole('button', { name: 'Save ceiling' }))
      gatewayHolds(vk({ budgets: undefined }))
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Save' }))
      })
      expect(bifrostApi.updateVirtualKey).toHaveBeenCalledWith('vk1', { name: 'vigil-soc', budgets: [] })
      expect(await field()).toHaveValue(null)
    })

    it('shows a reset period other than monthly beside the field', async () => {
      gatewayHolds(vk({ budgets: [{ max_limit: 5, reset_duration: '1d' }] }))
      render(<LimitsStep />)
      await field()
      expect(screen.getByText('Resets daily')).toBeInTheDocument()
    })

    it('says so when the gateway refuses the save', async () => {
      vi.mocked(bifrostApi.updateVirtualKey).mockRejectedValue({
        response: { data: { error: { message: 'budget max limit cannot be negative' } } },
      })
      render(<LimitsStep />)
      fireEvent.change(await field(), { target: { value: '40' } })
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Save ceiling' }))
      })
      expect(screen.getByText('budget max limit cannot be negative')).toBeInTheDocument()
    })

    it.each([
      ['no default key is set', () => vi.mocked(budgetsApi.getQuota).mockResolvedValue({ data: { configured: false } } as never)],
      ['the key is not on the gateway', () => vi.mocked(budgetsApi.getQuota).mockResolvedValue(quota('gone') as never)],
      ['two keys share the name', () => gatewayHolds(vk(), vk({ id: 'vk2' }))],
    ])('is Later when %s', async (_why, arrange) => {
      arrange()
      render(<LimitsStep />)
      expect(await screen.findByText('Later')).toBeInTheDocument()
      expect(screen.queryByRole('spinbutton', { name: 'Monthly ceiling' })).not.toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'Why the monthly ceiling is not available' }))
      expect(screen.getByText(/Settings › AI models/)).toBeInTheDocument()
    })

    it('reports a budget read error and a key list error separately', async () => {
      vi.mocked(budgetsApi.getQuota).mockRejectedValue(new Error('down'))
      const first = render(<LimitsStep />)
      expect(await screen.findByText(/Could not read the gateway budget/)).toBeInTheDocument()
      first.unmount()

      vi.mocked(budgetsApi.getQuota).mockResolvedValue(quota() as never)
      vi.mocked(bifrostApi.listVirtualKeys).mockRejectedValue(new Error('down'))
      render(<LimitsStep />)
      expect(await screen.findByText(/Could not read the gateway’s virtual keys/)).toBeInTheDocument()
      expect(screen.queryByText(/Could not read the gateway budget/)).not.toBeInTheDocument()
    })
  })

  describe('Slack route', () => {
    it('reads Connected from the Slack bot token', async () => {
      vi.mocked(configApi.getIntegrations).mockResolvedValue(integrations({ slack: { bot_token: true } }) as never)
      render(<LimitsStep />)
      expect(await screen.findByText('Connected')).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Connect Slack' })).not.toBeInTheDocument()
      expect(screen.getByText('Post urgent notifications to a channel')).toBeInTheDocument()
    })

    it('does not read a PagerDuty-only install as Slack connected', async () => {
      vi.mocked(configApi.getIntegrations).mockResolvedValue(
        integrations({ pagerduty: { api_token: true }, slack: { bot_token: false } }) as never,
      )
      render(<LimitsStep />)
      expect(await screen.findByRole('button', { name: 'Connect Slack' })).toBeInTheDocument()
      expect(screen.queryByText('Connected')).not.toBeInTheDocument()
    })

    it('connects inline, merges into the saved integrations, and flips to Connected', async () => {
      render(<LimitsStep />)
      fireEvent.click(await screen.findByRole('button', { name: 'Connect Slack' }))
      fireEvent.click(screen.getByRole('button', { name: 'Save Slack' }))
      expect(await screen.findByText('Please fill in: Bot Token')).toBeInTheDocument()
      expect(configApi.setIntegrations).not.toHaveBeenCalled()

      fireEvent.change(screen.getByPlaceholderText('xoxb-your-bot-token'), { target: { value: 'xoxb-1' } })
      fireEvent.change(screen.getByPlaceholderText('#security-alerts'), { target: { value: '#soc' } })
      vi.mocked(configApi.getIntegrations).mockResolvedValue(integrations({ slack: { bot_token: true } }) as never)
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Save Slack' }))
      })
      expect(configApi.setIntegrations).toHaveBeenCalledWith({
        enabled_integrations: ['splunk', 'slack'],
        integrations: { splunk: { url: 'u' }, slack: { bot_token: 'xoxb-1', default_channel: '#soc' } },
      })
      expect(await screen.findByText('Connected')).toBeInTheDocument()
    })

    it('says so when the integrations cannot be read', async () => {
      vi.mocked(configApi.getIntegrations).mockResolvedValue(integrations({}, { error: 'boom' }) as never)
      render(<LimitsStep />)
      expect(await screen.findByText(/Could not read the Slack connection/)).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Connect Slack' })).not.toBeInTheDocument()
    })
  })
})
