/* The spending-limit card edits the one key Vigil sends. enforcement_mode and
   budget_limit_usd are stored and never read at dispatch, so there is no control
   for them and they only round-trip. */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import AiBudgetsPanel from './AiBudgetsPanel'

const m = vi.hoisted(() => ({
  save: vi.fn(() => Promise.resolve({})),
  saveVk: vi.fn(() => Promise.resolve({ name: 'vigil-soc', value: 'sk-bf-new' }) as Promise<unknown>),
  reloadVigil: vi.fn(),
  budgets: {} as Record<string, unknown>,
  keys: {} as Record<string, unknown>,
}))

vi.mock('./useSettings', () => ({
  useBudgets: () => ({ ...m.budgets, save: m.save, reload: m.reloadVigil }),
}))
vi.mock('./useBifrost', () => ({
  useVirtualKeys: () => ({ reload: vi.fn(), save: m.saveVk, remove: vi.fn(), ...m.keys }),
  bifrostError: (_e: unknown, fallback: string) => fallback,
}))

const VK = {
  id: 'vk1',
  name: 'vigil-soc',
  description: 'Vigil',
  value: 'sk-bf-x',
  is_active: true,
  budget: { max_limit: 100, reset_duration: '1mo', current_usage: 69 },
  rate_limit: { request_max_limit: 60, request_reset_duration: 'minute', token_max_limit: 5000, token_reset_duration: 'minute' },
}
const QUOTA = {
  configured: true,
  available: true,
  quota: { virtual_key_name: 'vigil-soc', is_active: true, budgets: [{ max_limit: 100, current_usage: 69, reset_duration: '1mo' }] },
}

const setup = (o: { settings?: object; quota?: object | null; vks?: unknown[]; keyPhase?: string; vigilPhase?: string } = {}) => {
  m.budgets = {
    settings: { default_vk: 'sk-bf-x', budget_limit_usd: 500, enforcement_mode: 'hard_stop', ...o.settings },
    quota: o.quota === undefined ? QUOTA : o.quota,
    phase: o.vigilPhase ?? 'ready',
  }
  m.keys = { vks: o.vks ?? [VK], phase: o.keyPhase ?? 'ready', error: 'boom' }
  render(<AiBudgetsPanel notify={() => {}} />)
}

beforeEach(() => vi.clearAllMocks())

describe('AiBudgetsPanel', () => {
  it('shows live usage against the ceiling, labelled with the real reset period', () => {
    setup()
    expect(screen.getByText('Used this month')).toBeInTheDocument()
    expect(screen.getByText('$69 of $100')).toBeInTheDocument()
    expect(screen.getByText('Good')).toBeInTheDocument()
    expect(screen.getByText('69%')).toBeInTheDocument()
  })

  it.each([[75, 'Fair'], [90, 'Fair'], [91, 'Poor']])('reads %i%% used as %s', (used, level) => {
    const budgets = [{ max_limit: 100, current_usage: used, reset_duration: '1mo' }]
    setup({ quota: { ...QUOTA, quota: { ...QUOTA.quota, budgets } } })
    expect(screen.getByText(level)).toBeInTheDocument()
  })

  it('offers no enforcement-mode choice', () => {
    setup()
    expect(screen.queryByText(/ceiling is reached/i)).not.toBeInTheDocument()
    expect(screen.getByText(/refuses further LLM calls/)).toBeInTheDocument()
  })

  it('shows loading, then the error with a retry', () => {
    setup({ keyPhase: 'loading', vks: [] })
    expect(screen.getByText('Loading spending limit…')).toBeInTheDocument()
  })

  it('shows the load failure', () => {
    setup({ keyPhase: 'error', vks: [] })
    expect(screen.getByText('Couldn’t load the spending limit')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Retry/ })).toBeInTheDocument()
  })

  it('shows the server message when the quota is unavailable', () => {
    setup({ quota: { configured: true, available: false, message: 'Bifrost rejected the key.' }, vks: [] })
    expect(screen.getByText('Bifrost rejected the key.')).toBeInTheDocument()
  })

  it('offers to create a key when none is configured, and points Vigil at the new one', async () => {
    setup({ settings: { default_vk: '' }, quota: { configured: false, message: 'No key configured.' }, vks: [] })
    expect(screen.getByText('No spending limit yet')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /New key/ }))
    fireEvent.click(await screen.findByRole('button', { name: /Create and use/ }))
    await waitFor(() =>
      expect(m.saveVk).toHaveBeenCalledWith(null, {
        name: 'vigil-soc',
        is_active: true,
        budget: { max_limit: 100, reset_duration: 'monthly' },
        rate_limit: null,
      }),
    )
    await waitFor(() =>
      expect(m.save).toHaveBeenCalledWith({ default_vk: 'sk-bf-new', budget_limit_usd: 500, enforcement_mode: 'hard_stop' }),
    )
  })

  it('says a key without a budget is unmetered and lets a ceiling be set', async () => {
    setup({ vks: [{ ...VK, budget: null, rate_limit: null }], quota: { ...QUOTA, quota: { ...QUOTA.quota, budgets: [] } } })
    expect(screen.getByText(/This key is unmetered/)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Spend ceiling (USD)'), { target: { value: '50' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(m.saveVk).toHaveBeenCalledWith('vk1', expect.objectContaining({ budget: { max_limit: 50, reset_duration: 'monthly' }, rate_limit: null })),
    )
  })

  it('confirms before raising the ceiling, and saves what the card does not draw as it was', async () => {
    setup()
    fireEvent.change(screen.getByLabelText('Spend ceiling (USD)'), { target: { value: '200' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(m.saveVk).not.toHaveBeenCalled()
    fireEvent.click(await screen.findByRole('button', { name: 'Raise ceiling' }))
    await waitFor(() =>
      expect(m.saveVk).toHaveBeenCalledWith('vk1', {
        name: 'vigil-soc',
        description: 'Vigil',
        is_active: true,
        allowed_models: undefined,
        allowed_providers: undefined,
        budget: { max_limit: 200, reset_duration: 'monthly' },
        rate_limit: { request_max_limit: 60, request_reset_duration: 'minute', token_max_limit: 5000, token_reset_duration: 'minute' },
      }),
    )
  })

  it('confirms before stretching the same ceiling over a shorter period', async () => {
    setup()
    fireEvent.click(screen.getByText('Monthly'))
    fireEvent.click(await screen.findByText('Daily'))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('button', { name: 'Raise ceiling' })).toBeInTheDocument()
    expect(m.saveVk).not.toHaveBeenCalled()
  })

  it('keeps the window a stored rate limit was saved with when it is not edited', async () => {
    setup({ vks: [{ ...VK, rate_limit: { request_max_limit: 1000, request_reset_duration: '1h' } }] })
    fireEvent.change(screen.getByLabelText('Spend ceiling (USD)'), { target: { value: '80' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(m.saveVk).toHaveBeenCalledWith('vk1', expect.objectContaining({ rate_limit: { request_max_limit: 1000, request_reset_duration: '1h' } })),
    )
  })

  it('confirms before removing the ceiling', async () => {
    setup()
    fireEvent.change(screen.getByLabelText('Spend ceiling (USD)'), { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Remove ceiling' }))
    await waitFor(() => expect(m.saveVk).toHaveBeenCalledWith('vk1', expect.objectContaining({ budget: null })))
  })

  it('lowers the ceiling and changes the rate limit without asking', async () => {
    setup()
    fireEvent.change(screen.getByLabelText('Spend ceiling (USD)'), { target: { value: '80' } })
    fireEvent.change(screen.getByLabelText('Requests per minute'), { target: { value: '30' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(m.saveVk).toHaveBeenCalledWith(
        'vk1',
        expect.objectContaining({
          budget: { max_limit: 80, reset_duration: 'monthly' },
          rate_limit: expect.objectContaining({ request_max_limit: 30, request_reset_duration: 'minute', token_max_limit: 5000 }),
        }),
      ),
    )
  })

  it('still finds the key by name when the listed secret is masked, and can paste a secret', async () => {
    setup({ vks: [{ ...VK, value: 'sk-bf-****' }] })
    expect(screen.getByLabelText('Spend ceiling (USD)')).toHaveValue(100)
    fireEvent.click(screen.getByRole('button', { name: /Use a different key/ }))
    fireEvent.change(screen.getByPlaceholderText('sk-bf-…'), { target: { value: 'sk-bf-pasted' } })
    fireEvent.click(screen.getByRole('button', { name: /Use this key/ }))
    await waitFor(() =>
      expect(m.save).toHaveBeenCalledWith({ default_vk: 'sk-bf-pasted', budget_limit_usd: 500, enforcement_mode: 'hard_stop' }),
    )
  })

  it('offers the paste field when the configured key cannot be matched', () => {
    setup({ vks: [{ ...VK, name: 'other', value: 'sk-bf-****' }] })
    expect(screen.getByText('Vigil’s key can’t be edited here')).toBeInTheDocument()
    expect(screen.getByPlaceholderText('sk-bf-…')).toBeInTheDocument()
  })
})
