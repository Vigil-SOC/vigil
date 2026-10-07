import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import LimitsStep from './LimitsStep'
import { normalizeResetPeriod, resolveCeiling } from './ceiling'
import { budgetsApi, configApi, type BudgetQuotaResponse } from '../../services/api'
import { bifrostApi, type BifrostVirtualKey } from '../../services/bifrostApi'

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
  budgetsApi: {
    get: vi.fn(),
    getQuota: vi.fn(),
  },
}))

vi.mock('../../services/bifrostApi', () => ({
  bifrostApi: {
    listVirtualKeys: vi.fn(),
    updateVirtualKey: vi.fn(),
  },
}))

const noIntegrations = { enabled_integrations: [], integrations: {}, secrets_set: {} }

/* The default key: its list value is masked, and `default_vk` (the secret the
   budget endpoint returns) matches nothing in the list — resolution has to go
   through the quota endpoint's key name. */
const defaultKey = (over: Partial<BifrostVirtualKey> = {}): BifrostVirtualKey => ({
  id: 'vk-1',
  name: 'vigil-default',
  description: 'Default key',
  is_active: true,
  value: 'sk-bf-****1234',
  allowed_models: [],
  allowed_providers: [],
  budget: { max_limit: 500, reset_duration: 'monthly', current_usage: 12 },
  rate_limit: null,
  ...over,
})

const quotaFor = (maxLimit: number, resetDuration = '1mo', name = 'vigil-default') => ({
  data: {
    configured: true,
    available: true,
    quota: {
      virtual_key_name: name,
      is_active: true,
      budgets: [
        {
          id: 'b1',
          max_limit: maxLimit,
          current_usage: 12,
          reset_duration: resetDuration,
          calendar_aligned: true,
          last_reset: '',
        },
      ],
    },
  },
})

/** Serves a resolvable ceiling whose quota follows `state.max` (so a save can move it). */
const serveCeiling = (key: BifrostVirtualKey = defaultKey()) => {
  const state = { max: key.budget?.max_limit ?? 0 }
  vi.mocked(budgetsApi.get).mockResolvedValue({
    data: { default_vk: 'sk-bf-the-real-secret', budget_limit_usd: state.max, enforcement_mode: 'warning' },
  } as never)
  vi.mocked(budgetsApi.getQuota).mockImplementation(
    () => Promise.resolve(quotaFor(state.max, '1mo', key.name)) as never,
  )
  vi.mocked(bifrostApi.listVirtualKeys).mockResolvedValue({
    data: { virtual_keys: [key], count: 1 },
  } as never)
  vi.mocked(bifrostApi.updateVirtualKey).mockImplementation((_id, body) => {
    state.max = body.budget?.max_limit ?? 0
    return Promise.resolve({ data: key }) as never
  })
  return state
}

const profileCard = (name: RegExp) => screen.getByRole('button', { name })

describe('LimitsStep', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(configApi.getOrchestrator).mockResolvedValue({ data: { ...stored, profiles } } as never)
    vi.mocked(configApi.setOrchestrator).mockResolvedValue({ data: {} } as never)
    vi.mocked(configApi.getIntegrations).mockResolvedValue({ data: noIntegrations } as never)
    vi.mocked(configApi.setIntegrations).mockResolvedValue({ data: {} } as never)
    // no default virtual key by default: the ceiling is Later
    vi.mocked(budgetsApi.get).mockResolvedValue({
      data: { default_vk: '', budget_limit_usd: 0, enforcement_mode: 'warning' },
    } as never)
    vi.mocked(budgetsApi.getQuota).mockResolvedValue({ data: { configured: false } } as never)
    vi.mocked(bifrostApi.listVirtualKeys).mockResolvedValue({ data: { virtual_keys: [], count: 0 } } as never)
    vi.mocked(bifrostApi.updateVirtualKey).mockResolvedValue({ data: {} } as never)
  })

  describe('Spending card', () => {
    it('selects the profile the saved config matches, with card copy from the served values', async () => {
      render(<LimitsStep />)
      const balanced = await screen.findByRole('button', { name: /Balanced/ })
      expect(balanced).toHaveAttribute('aria-pressed', 'true')
      expect(profileCard(/Conservative/)).toHaveAttribute('aria-pressed', 'false')
      expect(screen.getByText('3 agents at once, $5.00 per investigation')).toBeInTheDocument()
      expect(screen.getByText('2 agents at once, $1.00 per investigation')).toBeInTheDocument()
      expect(screen.getByText('5 agents at once, $15.00 per investigation')).toBeInTheDocument()
      expect(screen.getAllByText('Recommended')).toHaveLength(1)
      expect(screen.queryByRole('button', { name: /Assist|Act/ })).not.toBeInTheDocument()
      expect(configApi.setOrchestrator).not.toHaveBeenCalled()
    })

    it('does not claim the gateway checks the profile limits', async () => {
      render(<LimitsStep />)
      await screen.findByRole('button', { name: /Balanced/ })
      expect(
        screen.getByText('Pick a starting profile. Vigil stops an investigation that reaches its limits.'),
      ).toBeInTheDocument()
      // the ceiling is Later here, so the gateway line appears nowhere on the step
      expect(screen.queryByText(/model gateway checks it before every call/)).not.toBeInTheDocument()
    })

    it('keeps the five limits behind the collapsed "Show the limits" row', async () => {
      render(<LimitsStep />)
      await screen.findByRole('button', { name: /Balanced/ })
      expect(screen.queryByText('Default case limits')).not.toBeInTheDocument()
      expect(screen.queryByText('Max cost per investigation')).not.toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'Show the limits' }))
      expect(screen.getByText('Max cost per investigation')).toBeInTheDocument()
      expect(screen.getByText('$5.00')).toBeInTheDocument()
      expect(screen.getByText('1 h')).toBeInTheDocument()
      expect(screen.getByText('$20.00 / h')).toBeInTheDocument()
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
      expect(profileCard(/Conservative/)).toHaveAttribute('aria-pressed', 'true')
      fireEvent.click(screen.getByRole('button', { name: 'Show the limits' }))
      expect(screen.getByText('$1.00')).toBeInTheDocument()
    })

    it('confirms before a pick that raises a limit, and saves nothing on cancel', async () => {
      render(<LimitsStep />)
      fireEvent.click(await screen.findByRole('button', { name: /Broad/ }))
      expect(screen.getByText('Raise investigation limits?')).toBeInTheDocument()
      expect(configApi.setOrchestrator).not.toHaveBeenCalled()
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
      expect(configApi.setOrchestrator).not.toHaveBeenCalled()
      expect(profileCard(/Balanced/)).toHaveAttribute('aria-pressed', 'true')

      fireEvent.click(screen.getByRole('button', { name: /Broad/ }))
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Save' }))
      })
      expect(configApi.setOrchestrator).toHaveBeenCalledWith(
        expect.objectContaining({ max_concurrent_agents: 5, max_total_hourly_cost: 60 }),
      )
      expect(profileCard(/Broad/)).toHaveAttribute('aria-pressed', 'true')
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
      expect(profileCard(/Balanced/)).toHaveAttribute('aria-pressed', 'true')
    })

    it('reads Custom limits in effect, selects no card, and shows the saved values', async () => {
      vi.mocked(configApi.getOrchestrator).mockResolvedValue({
        data: { ...stored, max_concurrent_agents: 4, profiles },
      } as never)
      render(<LimitsStep />)
      expect(await screen.findByText(/Custom limits in effect/)).toBeInTheDocument()
      for (const card of screen.getAllByRole('button', { name: /Conservative|Balanced|Broad/ }))
        expect(card).toHaveAttribute('aria-pressed', 'false')
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

    it('builds neither the Email/PagerDuty rows nor Invite your team', async () => {
      render(<LimitsStep />)
      await screen.findByRole('button', { name: /Balanced/ })
      expect(screen.queryByText(/PagerDuty/)).not.toBeInTheDocument()
      expect(screen.queryByText(/Invite your team/)).not.toBeInTheDocument()
      expect(screen.queryByText(/^Email$/)).not.toBeInTheDocument()
    })
  })

  describe('Monthly ceiling', () => {
    it('shows the resolved ceiling and a Not measured yet estimate, with the gateway line on the field only', async () => {
      serveCeiling()
      render(<LimitsStep />)
      const field = await screen.findByLabelText('Monthly ceiling')
      expect(field).toHaveValue('500')
      expect(screen.getByText(/Monthly estimate:/)).toBeInTheDocument()
      expect(screen.getByText(/Not measured yet/)).toBeInTheDocument()
      expect(screen.getAllByText(/The model gateway checks it before every call\./)).toHaveLength(1)
      expect(
        screen.getByText('Pick a starting profile. Vigil stops an investigation that reaches its limits.'),
      ).toBeInTheDocument()
    })

    it('lowers without a confirm, sends the full key body, never the masked value, and shows the reloaded value', async () => {
      serveCeiling()
      render(<LimitsStep />)
      const field = await screen.findByLabelText('Monthly ceiling')
      fireEvent.change(field, { target: { value: '250' } })
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Save ceiling' }))
      })
      expect(screen.queryByText('Raise the monthly ceiling?')).not.toBeInTheDocument()
      expect(bifrostApi.updateVirtualKey).toHaveBeenCalledTimes(1)
      const [id, body] = vi.mocked(bifrostApi.updateVirtualKey).mock.calls[0]
      expect(id).toBe('vk-1')
      expect(body).toMatchObject({
        name: 'vigil-default',
        is_active: true,
        budget: { max_limit: 250, reset_duration: 'monthly' },
        rate_limit: null,
      })
      expect(body).not.toHaveProperty('value')
      expect(JSON.stringify(body)).not.toContain('****')
      expect(JSON.stringify(body)).not.toContain('sk-bf')
      // reloaded after the save: the quota now serves 250 and the field follows it
      await waitFor(() => expect(screen.getByLabelText('Monthly ceiling')).toHaveValue('250'))
      expect(bifrostApi.listVirtualKeys).toHaveBeenCalledTimes(2)
    })

    it('confirms before a raise, and saves nothing on cancel', async () => {
      serveCeiling()
      render(<LimitsStep />)
      const field = await screen.findByLabelText('Monthly ceiling')
      fireEvent.change(field, { target: { value: '900' } })
      fireEvent.click(screen.getByRole('button', { name: 'Save ceiling' }))
      expect(await screen.findByText('Raise the monthly ceiling?')).toBeInTheDocument()
      expect(bifrostApi.updateVirtualKey).not.toHaveBeenCalled()
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
      expect(bifrostApi.updateVirtualKey).not.toHaveBeenCalled()

      fireEvent.click(screen.getByRole('button', { name: 'Save ceiling' }))
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Save' }))
      })
      expect(bifrostApi.updateVirtualKey).toHaveBeenCalledWith(
        'vk-1',
        expect.objectContaining({ budget: { max_limit: 900, reset_duration: 'monthly' } }),
      )
    })

    it('treats a cleared field as a raise to no ceiling', async () => {
      serveCeiling()
      render(<LimitsStep />)
      const field = await screen.findByLabelText('Monthly ceiling')
      fireEvent.change(field, { target: { value: '' } })
      fireEvent.click(screen.getByRole('button', { name: 'Save ceiling' }))
      expect(await screen.findByText('Raise the monthly ceiling?')).toBeInTheDocument()
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Save' }))
      })
      expect(bifrostApi.updateVirtualKey).toHaveBeenCalledWith(
        'vk-1',
        expect.objectContaining({ budget: null }),
      )
    })

    it('shows the reset period beside the field when it is not monthly, and preserves it on save', async () => {
      serveCeiling(defaultKey({ budget: { max_limit: 500, reset_duration: 'weekly', current_usage: 0 } }))
      render(<LimitsStep />)
      const field = await screen.findByLabelText('Monthly ceiling')
      expect(screen.getByText('Resets weekly')).toBeInTheDocument()
      fireEvent.change(field, { target: { value: '100' } })
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Save ceiling' }))
      })
      expect(bifrostApi.updateVirtualKey).toHaveBeenCalledWith(
        'vk-1',
        expect.objectContaining({ budget: { max_limit: 100, reset_duration: 'weekly' } }),
      )
    })

    it('is Later with no default virtual key, and its tip points at Settings › AI models', async () => {
      render(<LimitsStep />)
      expect(await screen.findByText('Later')).toBeInTheDocument()
      expect(screen.queryByLabelText('Monthly ceiling', { selector: 'input' })).not.toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'Monthly ceiling' }))
      expect(await screen.findByText(/Settings › AI models/)).toBeInTheDocument()
    })

    it('is Later when the quota name matches no key, and when it matches two', async () => {
      vi.mocked(budgetsApi.get).mockResolvedValue({
        data: { default_vk: 'sk-bf-secret', budget_limit_usd: 0, enforcement_mode: 'warning' },
      } as never)
      vi.mocked(budgetsApi.getQuota).mockResolvedValue(quotaFor(500) as never)
      vi.mocked(bifrostApi.listVirtualKeys).mockResolvedValue({ data: { virtual_keys: [], count: 0 } } as never)
      const { unmount } = render(<LimitsStep />)
      expect(await screen.findByText('Later')).toBeInTheDocument()
      unmount()

      vi.mocked(bifrostApi.listVirtualKeys).mockResolvedValue({
        data: { virtual_keys: [defaultKey(), defaultKey({ id: 'vk-2' })], count: 2 },
      } as never)
      render(<LimitsStep />)
      expect(await screen.findByText('Later')).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Save ceiling' })).not.toBeInTheDocument()
    })

    it('shows a budget read error apart from a quota/list failure', async () => {
      vi.mocked(budgetsApi.get).mockRejectedValue(new Error('down'))
      const { unmount } = render(<LimitsStep />)
      expect(await screen.findByText('Could not read the budget settings.')).toBeInTheDocument()
      expect(screen.queryByText(/default virtual key from the gateway/)).not.toBeInTheDocument()
      unmount()

      vi.mocked(budgetsApi.get).mockResolvedValue({
        data: { default_vk: 'sk-bf-secret', budget_limit_usd: 0, enforcement_mode: 'warning' },
      } as never)
      vi.mocked(budgetsApi.getQuota).mockRejectedValue(new Error('down'))
      render(<LimitsStep />)
      expect(
        await screen.findByText('Could not read the default virtual key from the gateway.'),
      ).toBeInTheDocument()
      expect(screen.queryByText('Could not read the budget settings.')).not.toBeInTheDocument()
    })

    it('shows a failed ceiling save and keeps the typed value', async () => {
      serveCeiling()
      vi.mocked(bifrostApi.updateVirtualKey).mockRejectedValue(new Error('gateway said no'))
      render(<LimitsStep />)
      const field = await screen.findByLabelText('Monthly ceiling')
      fireEvent.change(field, { target: { value: '250' } })
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Save ceiling' }))
      })
      expect(await screen.findByRole('alert')).toHaveTextContent('gateway said no')
      expect(screen.getByLabelText('Monthly ceiling')).toHaveValue('250')
    })
  })

  describe('Slack row', () => {
    it('shows the row with its line and a Connect Slack button when Slack is not connected', async () => {
      render(<LimitsStep />)
      expect(await screen.findByRole('button', { name: 'Connect Slack' })).toBeInTheDocument()
      expect(screen.getByText('Slack')).toBeInTheDocument()
      expect(screen.getByText('Post urgent notifications to a channel')).toBeInTheDocument()
      expect(screen.queryByText('Connected')).not.toBeInTheDocument()
    })

    it('reads Connected from the Slack bot token being set', async () => {
      vi.mocked(configApi.getIntegrations).mockResolvedValue({
        data: { ...noIntegrations, secrets_set: { slack: { bot_token: true } } },
      } as never)
      render(<LimitsStep />)
      expect(await screen.findByText('Connected')).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Connect Slack' })).not.toBeInTheDocument()
    })

    it('does not read Connected from a PagerDuty-only install', async () => {
      vi.mocked(configApi.getIntegrations).mockResolvedValue({
        data: { ...noIntegrations, secrets_set: { pagerduty: { api_token: true } } },
      } as never)
      render(<LimitsStep />)
      expect(await screen.findByRole('button', { name: 'Connect Slack' })).toBeInTheDocument()
      expect(screen.queryByText('Connected')).not.toBeInTheDocument()
    })

    it('shows an error when the integration status cannot be read', async () => {
      vi.mocked(configApi.getIntegrations).mockRejectedValue(new Error('down'))
      render(<LimitsStep />)
      expect(await screen.findByText('Could not read integration status.')).toBeInTheDocument()
    })

    it('opens the inline form, blocks an empty token, then saves and flips to Connected', async () => {
      render(<LimitsStep />)
      fireEvent.click(await screen.findByRole('button', { name: 'Connect Slack' }))
      const token = await screen.findByLabelText(/Bot Token/)
      expect(screen.queryByRole('button', { name: 'Connect Slack' })).not.toBeInTheDocument()

      fireEvent.click(screen.getByRole('button', { name: 'Save' }))
      expect(await screen.findByRole('alert')).toHaveTextContent('Please fill in: Bot Token')
      expect(configApi.setIntegrations).not.toHaveBeenCalled()

      fireEvent.change(token, { target: { value: 'xoxb-test' } })
      fireEvent.change(screen.getByLabelText(/Default Channel/), { target: { value: '#alerts' } })
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Save' }))
      })
      expect(configApi.setIntegrations).toHaveBeenCalledWith({
        enabled_integrations: ['slack'],
        integrations: { slack: { bot_token: 'xoxb-test', default_channel: '#alerts' } },
      })
      expect(await screen.findByText('Connected')).toBeInTheDocument()
      expect(screen.queryByLabelText(/Bot Token/)).not.toBeInTheDocument()
    })

    it('shows a failed Slack save and keeps the form open', async () => {
      vi.mocked(configApi.setIntegrations).mockRejectedValue({
        response: { data: { detail: 'Integrations are read-only.' } },
      })
      render(<LimitsStep />)
      fireEvent.click(await screen.findByRole('button', { name: 'Connect Slack' }))
      fireEvent.change(await screen.findByLabelText(/Bot Token/), { target: { value: 'xoxb-test' } })
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Save' }))
      })
      expect(await screen.findByText('Integrations are read-only.')).toBeInTheDocument()
      expect(screen.getByLabelText(/Bot Token/)).toBeInTheDocument()
      expect(screen.queryByText('Connected')).not.toBeInTheDocument()
    })
  })
})

describe('ceiling resolution', () => {
  const budget = { default_vk: 'sk-bf-secret', budget_limit_usd: 0, enforcement_mode: 'warning' as const }
  const quota = (name: string | null): BudgetQuotaResponse =>
    ({
      configured: name !== null,
      available: true,
      quota: name
        ? {
            virtual_key_name: name,
            is_active: true,
            budgets: [{ id: 'b', max_limit: 42, current_usage: 0, reset_duration: '1mo', calendar_aligned: true, last_reset: '' }],
          }
        : undefined,
    }) as BudgetQuotaResponse

  it('normalises the quota and key-list period spellings in one place', () => {
    expect(normalizeResetPeriod('1mo')).toBe('monthly')
    expect(normalizeResetPeriod('monthly')).toBe('monthly')
    expect(normalizeResetPeriod('1w')).toBe('weekly')
    expect(normalizeResetPeriod('weekly')).toBe('weekly')
    expect(normalizeResetPeriod('1d')).toBe('daily')
    expect(normalizeResetPeriod(undefined)).toBe('monthly')
  })

  it('resolves by the quota name, never by matching the (masked) value', () => {
    const state = resolveCeiling(budget, quota('vigil-default'), [defaultKey()])
    expect(state.kind).toBe('ready')
    if (state.kind === 'ready') {
      expect(state.key.id).toBe('vk-1')
      expect(state.currentLimit).toBe(42)
      expect(state.period).toBe('monthly')
    }
  })

  it('is Later when unconfigured, unnamed, missing or ambiguous', () => {
    expect(resolveCeiling({ ...budget, default_vk: '' }, quota('vigil-default'), [defaultKey()]).kind).toBe('later')
    expect(resolveCeiling(budget, quota(null), [defaultKey()]).kind).toBe('later')
    expect(resolveCeiling(budget, quota('other'), [defaultKey()]).kind).toBe('later')
    expect(resolveCeiling(budget, quota('vigil-default'), [defaultKey(), defaultKey({ id: 'vk-2' })]).kind).toBe(
      'later',
    )
  })
})
