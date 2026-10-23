import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

let summary: unknown = { total_cases: 3, open_cases: 1, critical_cases: 0 }
const ok = (data: unknown) => Promise.resolve({ data })
vi.mock('../../services/api', () => ({
  default: {},
  caseMetricsApi: {
    getSummary: () => ok(summary),
    getMTTD: () => ok({}),
    getMTTR: () => ok({}),
    getByPriority: () => ok({ priority_breakdown: [{ priority: 'high', count: 1, closed_count: 0 }] }),
    getByStatus: () => ok({ status_breakdown: [{ status: 'open', count: 1 }] }),
    getAnalystPerformance: () => ok({ analyst_performance: [] }),
  },
}))
import MetricsScreen from './MetricsScreen'

describe('probe', () => {
  it('total>0 renders the body and no Export button', async () => {
    summary = { total_cases: 3, open_cases: 1, critical_cases: 0 }
    render(<MetricsScreen />)
    expect(await screen.findByText('Total Cases')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Export report/ })).toBeNull()
  })
  it('race: total 0 with by-priority>0 falls to the outer empty state', async () => {
    summary = { total_cases: 0 }
    render(<MetricsScreen />)
    expect(await screen.findByText('No case metrics yet')).toBeTruthy()
    expect(screen.queryByText('Total Cases')).toBeNull()
  })
})
