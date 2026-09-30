/* Setup used to ask for a monthly cap and an enforcement mode. Neither is read
   at dispatch; the ceiling is the virtual key's budget. */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import BudgetDialog from './BudgetDialog'

const get = vi.fn()
const set = vi.fn((_payload: unknown) => Promise.resolve({ data: {} }))

vi.mock('../../services/api', () => ({
  budgetsApi: {
    get: () => get(),
    set: (payload: unknown) => set(payload),
  },
}))

describe('BudgetDialog', () => {
  it('asks only for the virtual key and round-trips the ignored fields', async () => {
    get.mockResolvedValue({
      data: { default_vk: 'sk-bf-abc', budget_limit_usd: 500, enforcement_mode: 'hard_stop' },
    })
    render(<BudgetDialog onClose={() => {}} onSaved={() => {}} />)

    expect(await screen.findByDisplayValue('sk-bf-abc')).toBeInTheDocument()
    expect(screen.queryByText('Monthly spend cap (USD)')).not.toBeInTheDocument()
    expect(screen.queryByText('Enforcement')).not.toBeInTheDocument()
    expect(screen.getByText(/Settings → AI Config → Virtual Keys/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(set).toHaveBeenCalledWith({
        default_vk: 'sk-bf-abc',
        budget_limit_usd: 500,
        enforcement_mode: 'hard_stop',
      }),
    )
  })
})
