/* The investigation queue used to print `$undefined` for a missing cost and
   `$0.000` for one nobody could compute (#989). */
import { describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import AutoOpsScreen from './AutoOpsScreen'

const inv = (over: Record<string, unknown>) => ({
  case_id: null, skill_id: 'triage', trigger_type: 'manual', status: 'completed', current_step: 1, total_steps: 1,
  iteration_count: 3, priority: 'medium', created_at: '2026-09-01T00:00:00Z', last_activity_at: null, summary: null, current_activity: null,
  ...over,
})

vi.mock('./useAutoOps', () => ({
  useAutoOps: () => ({
    status: {
      enabled: false, active_agents: 0, max_concurrent_agents: 2, queued: 0, completed: 3, failed: 0, pending_review: 0, total_investigations: 3,
      cost: { total_cost_usd: 1.25, active_cost_usd: 0, hourly_cost_usd: 0, hourly_budget_remaining: 5, per_investigation_limit: 1 },
      stats: {},
    },
    investigations: [
      inv({ investigation_id: 'inv-priced', cost_usd: 1.2346 }),
      inv({ investigation_id: 'inv-zero', cost_usd: 0 }),
      inv({ investigation_id: 'inv-absent', cost_usd: undefined }),
    ],
    phase: 'ready', error: null, notice: null, busy: null,
    reload: vi.fn(), clearError: vi.fn(), clearNotice: vi.fn(), toggleEnabled: vi.fn(), killAll: vi.fn(),
    setMaxAgents: vi.fn(), scanFindings: vi.fn(), wake: vi.fn(), killInvestigation: vi.fn(), review: vi.fn(),
  }),
}))

const rowFor = (id: string) => screen.getByText(id).closest('tr') as HTMLElement

describe('autoops investigation rows', () => {
  it('renders a priced cost and a real zero as dollars, and an absent cost as not priced', () => {
    render(<AutoOpsScreen openChat={vi.fn()} go={vi.fn()} goSettings={vi.fn()} setViewFull={vi.fn()} />)

    expect(within(rowFor('inv-priced')).getByText('$1.235')).toBeInTheDocument()
    expect(within(rowFor('inv-zero')).getByText('$0.000')).toBeInTheDocument()
    expect(within(rowFor('inv-absent')).getByText('not priced')).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('$undefined')
  })
})
