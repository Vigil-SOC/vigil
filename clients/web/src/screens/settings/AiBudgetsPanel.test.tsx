/* The enforcement-mode select used to live on "What Vigil sends". It was stored
   and never read, so the panel only edits which key Vigil presents. */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import AiBudgetsPanel from './AiBudgetsPanel'

const { save, settings } = vi.hoisted(() => ({
  save: vi.fn(() => Promise.resolve({})),
  settings: {
    default_vk: 'sk-bf-x',
    budget_limit_usd: 500,
    enforcement_mode: 'hard_stop' as const,
  },
}))

vi.mock('./useSettings', () => ({
  useBudgets: () => ({
    settings,
    quota: { configured: true, available: true },
    phase: 'ready' as const,
    save,
  }),
}))

vi.mock('./useBifrost', () => ({
  useVirtualKeys: () => ({
    vks: [],
    phase: 'ready' as const,
    error: null,
    reload: vi.fn(),
    save: vi.fn(),
    remove: vi.fn(),
  }),
  bifrostError: (_e: unknown, fallback: string) => fallback,
}))

describe('AiBudgetsPanel', () => {
  it('does not offer an enforcement mode, and a key save keeps the stored fields', async () => {
    render(<AiBudgetsPanel notify={() => {}} />)

    expect(screen.queryByText('Enforcement mode')).not.toBeInTheDocument()
    expect(screen.queryByText(/Warning only/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Hard stop/)).not.toBeInTheDocument()
    expect(screen.getByText(/The virtual key sent as x-bf-vk/)).toBeInTheDocument()
    expect(screen.queryByText(/what to do when the gateway refuses/i)).not.toBeInTheDocument()

    fireEvent.change(screen.getByDisplayValue('sk-bf-x'), { target: { value: 'sk-bf-new' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() =>
      expect(save).toHaveBeenCalledWith({
        default_vk: 'sk-bf-new',
        budget_limit_usd: 500,
        enforcement_mode: 'hard_stop',
      }),
    )
  })
})
