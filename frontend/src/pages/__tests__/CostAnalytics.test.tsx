import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

// Mock recharts before importing the component — recharts pulls in
// SVG/canvas APIs that jsdom doesn't provide and isn't what we're
// asserting on anyway. Just stub the bits CostAnalytics references.
vi.mock('recharts', () => {
  const Stub = ({ children }: { children?: any }) => <div>{children}</div>
  return {
    Bar: Stub,
    BarChart: Stub,
    CartesianGrid: Stub,
    Cell: Stub,
    Legend: Stub,
    ResponsiveContainer: Stub,
    Tooltip: Stub,
    XAxis: Stub,
    YAxis: Stub,
  }
})

// vi.mock factories are hoisted above all imports; vi.hoisted lets us
// share a mock fn between the factory and the test body without tripping
// the "cannot access before initialization" hoist-order trap.
const { mockGet } = vi.hoisted(() => ({ mockGet: vi.fn() }))

vi.mock('../../services/api', () => ({
  default: { get: mockGet },
  analyticsApi: {
    estimateCost: vi.fn(),
    recalculateCost: vi.fn(),
  },
}))

import CostAnalytics from '../CostAnalytics'

const samplePayload = {
  window: { start: '2026-04-28T00:00:00', end: '2026-05-05T00:00:00', seconds: 604800 },
  totals: {
    calls: 42,
    input_tokens: 100_000,
    output_tokens: 10_000,
    cache_read_tokens: 50_000,
    cache_creation_tokens: 5_000,
    cost_usd: 1.234,
    cache_hit_rate: 0.33,
  },
  by_agent: [],
  by_model: [
    {
      model: 'claude-sonnet-4-5-20250929',
      provider_type: 'anthropic',
      pricing_source: 'exact',
      calls: 30,
      input_tokens: 80_000,
      output_tokens: 8_000,
      cache_read_tokens: 40_000,
      cache_creation_tokens: 4_000,
      cost_usd: 1.0,
      cache_hit_rate: 0.33,
    },
    {
      model: 'some-future-model',
      provider_type: 'unknown',
      pricing_source: 'unknown',
      calls: 12,
      input_tokens: 20_000,
      output_tokens: 2_000,
      cache_read_tokens: 10_000,
      cache_creation_tokens: 1_000,
      cost_usd: 0.0,
      cache_hit_rate: 0.33,
    },
  ],
  top_investigations: [],
  time_series: null,
}

describe('CostAnalytics — pricing_source surfacing (#184)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockGet.mockResolvedValue({ data: samplePayload })
  })

  it('renders the per-model table with pricing badges and provider chips', async () => {
    render(<CostAnalytics />)

    await waitFor(() =>
      expect(mockGet).toHaveBeenCalledWith('/analytics/cost', expect.any(Object)),
    )

    // Both model rows present.
    expect(
      await screen.findByText('claude-sonnet-4-5-20250929'),
    ).toBeInTheDocument()
    expect(screen.getByText('some-future-model')).toBeInTheDocument()

    // Provider chip — anthropic appears exactly once. (The "unknown"
    // string appears in both the provider chip and the pricing badge,
    // so we use getAllByText for it and assert at least 2 matches.)
    expect(screen.getByText('anthropic')).toBeInTheDocument()
    expect(screen.getAllByText('unknown').length).toBeGreaterThanOrEqual(2)

    // Pricing badges — `exact` for catalogued model. The point of #184
    // Phase 3 surfacing: a $0 unknown row is visually distinguishable
    // from a $0 zero-pricing (Ollama) row.
    expect(screen.getByText('exact')).toBeInTheDocument()
  })

  it('shows the "free" label for zero-pricing rows instead of $0 ambiguity', async () => {
    mockGet.mockResolvedValue({
      data: {
        ...samplePayload,
        by_model: [
          {
            ...samplePayload.by_model[0],
            model: 'llama3.1',
            provider_type: 'ollama',
            pricing_source: 'zero',
            cost_usd: 0,
          },
        ],
      },
    })
    render(<CostAnalytics />)
    expect(await screen.findByText('llama3.1')).toBeInTheDocument()
    expect(screen.getByText('free')).toBeInTheDocument()
  })
})
