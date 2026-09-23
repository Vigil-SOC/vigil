/* The spend table is where an operator learns whether a model is free or merely
   unmeasured. Both used to read as $0.00, and `zero` was labelled "free" (#989). */
import { describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import HealthScreen from './HealthScreen'

const row = (over: Record<string, unknown>) => ({
  provider_type: 'bifrost', calls: 10, input_tokens: 1000, output_tokens: 500, cache_hit_rate: 0, ...over,
})

vi.mock('../settings/useSettings', () => ({
  useCostAnalytics: () => ({
    phase: 'ready',
    error: null,
    reload: vi.fn(),
    data: {
      totals: { cost_usd: 1.5, calls: 40, input_tokens: 4000, output_tokens: 2000, cache_hit_rate: 0.25 },
      by_model: [
        row({ model: 'claude-priced', pricing_source: 'exact', cost_usd: 1.5 }),
        row({ model: 'claude-free-tier', pricing_source: 'exact', cost_usd: 0 }),
        row({ model: 'ollama/llama3', pricing_source: 'zero', cost_usd: 0 }),
        row({ model: 'mystery-model', pricing_source: 'unknown', cost_usd: 0 }),
      ],
    },
  }),
}))
vi.mock('../decisions/useDecisions', () => ({
  usePendingApprovals: () => ({ phase: 'ready', error: null, actions: [], reload: vi.fn() }),
}))
vi.mock('./useHealth', () => ({
  RUNS_PER_WORKFLOW: 20,
  RUN_STATUSES: ['completed', 'failed'],
  useRunOutcomes: () => ({ phase: 'ready', error: null, rows: [], unread: [], reload: vi.fn() }),
  PROBE_OUTCOMES: ['hit', 'miss', 'silent'],
  PROBE_TALLY_DAYS: 7,
  useProbeScores: () => ({ phase: 'ready', error: null, probes: [], tally: { hit: 0, miss: 0, silent: 0 }, reload: vi.fn() }),
}))

const modelRow = (model: string) => screen.getByText(model).closest('tr') as HTMLElement

describe('health spend table', () => {
  it('shows a priced model and a genuine zero as dollar amounts', () => {
    render(<HealthScreen openChat={vi.fn()} go={vi.fn()} goSettings={vi.fn()} setViewFull={vi.fn()} />)

    expect(within(modelRow('claude-priced')).getByText('$1.50')).toBeInTheDocument()
    expect(within(modelRow('claude-free-tier')).getByText('$0.00')).toBeInTheDocument()
  })

  it('never renders a not-billed or unpriced model as a dollar amount, and never says "free"', () => {
    render(<HealthScreen openChat={vi.fn()} go={vi.fn()} goSettings={vi.fn()} setViewFull={vi.fn()} />)

    const selfHosted = modelRow('ollama/llama3')
    expect(within(selfHosted).getAllByText('not billed')).toHaveLength(2)
    expect(selfHosted.textContent).not.toMatch(/\$/)

    const unknown = modelRow('mystery-model')
    expect(within(unknown).getAllByText('not priced')).toHaveLength(2)
    expect(unknown.textContent).not.toMatch(/\$/)

    expect(screen.queryByText('free')).not.toBeInTheDocument()
    expect(screen.queryByText('unknown')).not.toBeInTheDocument()
  })
})
