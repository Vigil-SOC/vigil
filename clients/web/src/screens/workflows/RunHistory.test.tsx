/* A run that cost nothing used to hide behind "—" because 0 is falsy (#989). */
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { HistoryModal } from './WorkflowsScreen'

vi.mock('../../services/api', () => ({
  workflowApi: {
    listRuns: vi.fn(() => Promise.resolve({
      data: {
        runs: [
          { run_id: 'run-priced', status: 'completed', triggered_by: 'priced', total_cost_usd: 0.5 },
          { run_id: 'run-zero', status: 'completed', triggered_by: 'zero', total_cost_usd: 0 },
          { run_id: 'run-absent', status: 'running', triggered_by: 'absent', total_cost_usd: null },
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
})
