/* A run that cost nothing used to hide behind "—" because 0 is falsy (#989). */
import { describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { HistoryModal } from './WorkflowsScreen'

vi.mock('../../services/api', () => ({
  workflowApi: {
    listRuns: vi.fn(() => Promise.resolve({
      data: {
        runs: [
          { run_id: 'run-priced', status: 'completed', triggered_by: 'priced', total_cost_usd: 0.5 },
          { run_id: 'run-zero', status: 'completed', triggered_by: 'zero', total_cost_usd: 0 },
          { run_id: 'run-absent', status: 'running', triggered_by: 'absent', total_cost_usd: null },
          { run_id: 'run-budget', status: 'completed', triggered_by: 'budget', outcome: 'budget_exhausted', reason: 'hit the cost ceiling' },
          { run_id: 'run-abandoned', status: 'cancelled', triggered_by: 'left', outcome: 'abandoned', reason: 'parked with no answer' },
          { run_id: 'run-aborted', status: 'cancelled', triggered_by: 'halted', outcome: 'aborted', reason: 'the operator stopped it' },
          { run_id: 'run-failed', status: 'failed', triggered_by: 'crashed', outcome: 'failed', reason: 'the worker crashed' },
          { run_id: 'run-cancel', status: 'cancelled', triggered_by: 'operator', error: 'Cancelled: from the console' },
        ],
      },
    })),
    getRun: vi.fn(() => new Promise(() => undefined)),
  },
  agentsApi: { listAgents: vi.fn(() => Promise.resolve({ data: { agents: [] } })) },
  findingsApi: { getAll: vi.fn(() => Promise.resolve({ data: { findings: [] } })) },
  casesApi: { getAll: vi.fn(() => Promise.resolve({ data: { cases: [] } })) },
}))
vi.mock('../../services/skillsApi', () => ({
  skillsApi: { list: vi.fn(() => Promise.resolve([])) },
}))

const rowFor = (trigger: string) => screen.getByText(trigger).closest('tr') as HTMLElement
// the Cost column, by header position: <caret> Status Started Duration Trigger Cost
const costCell = (trigger: string) => rowFor(trigger).querySelectorAll('td')[5].textContent

describe('workflow run history rows', () => {
  it('shows a real zero as a zero, an absent cost as not priced, and never a dash', async () => {
    render(<HistoryModal wf={{ id: 'wf-1', name: 'Beacon hunt' } as never} onClose={vi.fn()} />)

    await screen.findByText('priced')
    expect(costCell('priced')).toBe('$0.500')
    expect(costCell('zero')).toBe('$0.000')
    expect(costCell('absent')).toBe('not priced')
  })

  it('badges a budget stop, an abandon and an abort, and leaves a plain finish bare', async () => {
    render(<HistoryModal wf={{ id: 'wf-1', name: 'Beacon hunt' } as never} onClose={vi.fn()} />)

    await screen.findByText('budget')
    expect(within(rowFor('budget')).getByText('stopped at budget')).toHaveAttribute('title', 'hit the cost ceiling')
    expect(within(rowFor('left')).getByText('abandoned')).toHaveAttribute('title', 'parked with no answer')
    expect(within(rowFor('halted')).getByText('aborted')).toHaveAttribute('title', 'the operator stopped it')
    // completed and failed already say what they are; an operator cancel stores
    // its note on error and has no agent outcome.
    expect(rowFor('priced').querySelectorAll('.status')).toHaveLength(1)
    expect(rowFor('crashed').querySelectorAll('.status')).toHaveLength(1)
    expect(rowFor('operator').querySelectorAll('.status')).toHaveLength(1)
    expect(within(rowFor('operator')).getByText('⚠')).toHaveAttribute('title', 'Cancelled: from the console')
  })
})
