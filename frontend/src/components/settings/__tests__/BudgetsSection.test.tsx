import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

vi.mock('../../../services/api', () => ({
  budgetsApi: {
    get: vi.fn(),
    set: vi.fn(),
    getQuota: vi.fn(),
  },
}))

import BudgetsSection from '../BudgetsSection'
import { budgetsApi } from '../../../services/api'

const baseSettings = {
  default_vk: 'sk-bf-abcdef-12345678',
  budget_limit_usd: 100,
  enforcement_mode: 'warning' as const,
}

const baseQuota = {
  configured: true,
  available: true,
  virtual_key_id: 'sk-bf-abcdef-12345678',
  message: null,
  quota: {
    virtual_key_name: 'default',
    is_active: true,
    budgets: [
      {
        id: 'b1',
        max_limit: 100,
        current_usage: 42,
        reset_duration: '1mo',
        calendar_aligned: true,
        last_reset: '2026-04-01T00:00:00Z',
      },
    ],
    rate_limit: null,
  },
}

describe('BudgetsSection (#186)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(budgetsApi.get as any).mockResolvedValue({ data: baseSettings })
    ;(budgetsApi.getQuota as any).mockResolvedValue({ data: baseQuota })
    ;(budgetsApi.set as any).mockResolvedValue({ data: baseSettings })
  })

  it('renders the live quota line when Bifrost returns budget data', async () => {
    render(<BudgetsSection setMessage={() => {}} />)

    // The "$X spent of $Y" line is the visual signal that Bifrost is
    // reachable and the VK is provisioned. Catching its absence in CI
    // is the whole point of this test — silent quota loss = no UI signal.
    await waitFor(() =>
      expect(budgetsApi.getQuota).toHaveBeenCalled(),
    )
    expect(await screen.findByText(/spent of/i)).toBeInTheDocument()
    expect(screen.getByText(/\$42\.00/)).toBeInTheDocument()
    expect(screen.getByText(/\$100\.00/)).toBeInTheDocument()
  })

  it('shows a warning alert when configured VK is unreachable', async () => {
    ;(budgetsApi.getQuota as any).mockResolvedValue({
      data: {
        ...baseQuota,
        available: false,
        message: 'Bifrost: VK not found',
        quota: undefined,
      },
    })
    render(<BudgetsSection setMessage={() => {}} />)
    expect(await screen.findByText(/VK not found/i)).toBeInTheDocument()
  })

  it('shows an info alert when no VK is configured (bootstrap mode)', async () => {
    ;(budgetsApi.get as any).mockResolvedValue({
      data: { ...baseSettings, default_vk: '' },
    })
    ;(budgetsApi.getQuota as any).mockResolvedValue({
      data: { configured: false, message: 'No VK configured' },
    })
    render(<BudgetsSection setMessage={() => {}} />)
    expect(await screen.findByText(/No VK configured/i)).toBeInTheDocument()
  })

  it('saves the form payload on Save', async () => {
    const setMessage = vi.fn()
    render(<BudgetsSection setMessage={setMessage} />)

    // Wait for initial load.
    await screen.findByText(/spent of/i)

    // Bump the budget ceiling and save.
    const ceilingInput = screen.getByLabelText(/Monthly budget ceiling/i)
    fireEvent.change(ceilingInput, { target: { value: '250' } })

    const saveButton = screen.getByRole('button', { name: /^Save$/i })
    fireEvent.click(saveButton)

    await waitFor(() => expect(budgetsApi.set).toHaveBeenCalled())
    const payload = (budgetsApi.set as any).mock.calls[0][0]
    expect(payload.budget_limit_usd).toBe(250)
    expect(payload.default_vk).toBe(baseSettings.default_vk)
    expect(payload.enforcement_mode).toBe('warning')
  })
})
