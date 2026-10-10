/* Kernel runs, rendered by the console's own run-detail components. The fixtures
   are what the agent service answers for real kernel ledgers -- a recorded hunt and
   an investigate run from the eval -- written by services/agent/scripts/
   kernel-view-fixtures.ts. A change on either side that the other cannot read
   fails here rather than in front of an operator. */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { RunDetail } from './WorkflowsScreen'
import { readFold } from '../cases/caseFold'
import huntProjection from './__fixtures__/kernel-hunt-projection.json'
import huntReplay from './__fixtures__/kernel-hunt-replay.json'
import investigateProjection from './__fixtures__/kernel-investigate-projection.json'
import investigateReplay from './__fixtures__/kernel-investigate-replay.json'

vi.mock('../../services/api', () => ({
  workflowApi: {
    steer: vi.fn(() => Promise.resolve({ data: {} })),
    cancelRun: vi.fn(() => Promise.resolve({ data: {} })),
    // As serve.ts answers it: the report narrowed to the one move asked about.
    getReplay: vi.fn((_run: string, decisionId: string) =>
      Promise.resolve({ data: { ...huntReplay, decisions: huntReplay.decisions.filter((one) => one.decision_id === decisionId) } }),
    ),
    replayRun: vi.fn(() => Promise.resolve({ data: investigateReplay })),
  },
  approvalsApi: {
    list: vi.fn(() => Promise.resolve({ data: { actions: [] } })),
    approve: vi.fn(() => Promise.resolve({ data: {} })),
    reject: vi.fn(() => Promise.resolve({ data: {} })),
  },
  agentsApi: { listAgents: vi.fn(() => Promise.resolve({ data: { agents: [] } })) },
  findingsApi: { getAll: vi.fn(() => Promise.resolve({ data: { findings: [] } })) },
  casesApi: { getAll: vi.fn(() => Promise.resolve({ data: { cases: [] } })) },
}))
vi.mock('../../services/skillsApi', () => ({
  skillsApi: { list: vi.fn(() => Promise.resolve([])) },
}))

const tabTo = (name: RegExp) => fireEvent.click(screen.getByRole('tab', { name }))

describe('a hunt run on the kernel', () => {
  const renderHunt = () => render(<RunDetail d={{ run_id: 'run-1', status: 'completed', hunt: huntProjection } as never} onSteered={() => {}} />)

  it('offers every tab a controller hunt did, each with something in it', () => {
    renderHunt()
    const tabs = screen.getAllByRole('tab').map((tab) => tab.textContent ?? '')
    for (const name of ['Hypotheses', 'Evidence', 'Memory', 'Moves', 'Frontier', 'Gaps', 'Escalations & checkpoints', 'Report', 'Next steps']) {
      expect(tabs.some((tab) => tab.startsWith(name))).toBe(true)
    }
    expect(screen.getByText(/Why it ended/)).toBeInTheDocument()
  })

  it('shows its beliefs, the one the agent proposed included', () => {
    renderHunt()
    tabTo(/Hypotheses/)
    expect(screen.getAllByText(/192\.0\.2\.10 is beaconing to 198\.51\.100\.7/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/runs an unsanctioned sync client/).length).toBeGreaterThan(0)
  })

  it('lists its moves, and opens what a move was made against without breaking', async () => {
    renderHunt()
    tabTo(/Moves/)
    expect(screen.getAllByText('VALIDATE').length).toBeGreaterThan(0)
    fireEvent.click(screen.getByText(/the beacon is proven/))

    expect(await screen.findByText(/What the lead was shown at turn/)).toBeInTheDocument()
    // The call that chose the move reproduced from the ledger.
    expect(screen.getByText('rebuild matches the record')).toBeInTheDocument()
    expect(screen.getAllByText(/runs an unsanctioned sync client/).length).toBeGreaterThan(0)
  })

  it('reads on the case page', () => {
    const fold = readFold({ status: 'completed', hunt: huntProjection })
    expect(fold).not.toBeNull()
    const text = JSON.stringify(fold)
    expect(text).toContain('192.0.2.10 is beaconing')
    expect(text).toContain('splunk_search')
  })
})

describe('an investigate run on the kernel', () => {
  it('lists each decision and the calls that followed it', async () => {
    render(<RunDetail d={{ run_id: 'run-1', status: 'completed', projection: investigateProjection, result_summary: 'closed' } as never} onSteered={() => {}} />)

    expect((await screen.findAllByText('EXAMINE')).length).toBe(4)
    expect(screen.getByText('CONCLUDE')).toBeInTheDocument()
    expect(screen.getAllByText('get_finding').length).toBeGreaterThan(0)
    expect(screen.getAllByText('splunk_search').length).toBeGreaterThan(0)
  })

  it('reads on the case page, its answer and calls included', () => {
    const fold = readFold({ status: 'completed', projection: investigateProjection })
    expect(fold).not.toBeNull()
    const text = JSON.stringify(fold)
    expect(text).toContain('benign')
    expect(text).toContain('get_finding')
  })
})
