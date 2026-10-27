import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { overviewApi, type AgentRecord, type OutcomeList, type OverviewAgent } from '../../services/api'
import { AgentPanel, OutcomePanel } from './OverviewDrill'

vi.mock('../../services/api', () => ({ overviewApi: { outcome: vi.fn(), agent: vi.fn() } }))

// both reads answer the same, so each test sets the one body it draws
const resolve = (body: unknown) => {
  vi.mocked(overviewApi.outcome).mockResolvedValue(body as never)
  vi.mocked(overviewApi.agent).mockResolvedValue(body as never)
}

const list = (over: Partial<OutcomeList> = {}): OutcomeList => ({
  state: 'working',
  label: 'Still working',
  explain: 'Today’s alerts in a case that is still open.',
  total: 3,
  offset: 0,
  items: [
    { finding_id: 'f-1', title: 'Beacon to cdn-sync-update.net', description: null, severity: 'High', data_source: 'zeek', created_at: '2026-10-27T14:02:11', case_id: 'SOC-4417' },
    { finding_id: 'f-2', title: 'Odd sign-in', description: null, severity: 'Medium', data_source: 'entra', created_at: '2026-10-27T13:00:00', case_id: null },
  ],
  cases: [{ case_id: 'SOC-4417', title: 'Possible exfiltration from cr-14', status: 'open', priority: 'high', updated_at: null, alerts: 2 }],
  by_source: [{ data_source: 'zeek', count: 2 }, { data_source: 'entra', count: 1 }],
  ...over,
})

const agent: OverviewAgent = { workflow_id: 'cloud-incident', name: 'cloud-incident', running: 1, sample_size: 4, rate: 0.75, level: 'fair', current_step: 'investigate' }
const record: AgentRecord = {
  workflow: { id: 'cloud-incident', name: 'cloud-incident', source: 'file', description: 'Investigate cloud incidents.', objectives: ['Scope the identity blast radius'], tools_used: [] },
  live: [{ run_id: 'run-live-1', status: 'running', started_at: '2026-10-27T18:00:00', finished_at: null, duration_ms: null, cost_usd: 0, outcome: null, summary: null, triggered_by: 'triage', case_id: 'SOC-4417', finding_id: 'f-1' }],
  recent: [{ run_id: 'run-done-1', status: 'completed', started_at: '2026-10-27T10:00:00', finished_at: null, duration_ms: 252000, cost_usd: 0.62, outcome: 'benign', summary: 'Closed as benign: known backup job', triggered_by: 'triage', case_id: null, finding_id: 'f-9' }],
  record: { days: 30, runs: 4, completed: 3, failed: 1, cancelled: 0, rate: 0.75, median_ms: 252000, mean_cost_usd: 0.62 },
}

afterEach(() => {
  vi.mocked(overviewApi.outcome).mockReset()
  vi.mocked(overviewApi.agent).mockReset()
})

describe('OutcomePanel', () => {
  it('lists the cases and alerts behind an outcome, and opens each', async () => {
    resolve({ data: list() })
    const onOpenAlert = vi.fn()
    const openCase = vi.fn()
    render(<MemoryRouter><OutcomePanel state="working" onClose={vi.fn()} onOpenAlert={onOpenAlert} openCase={openCase} goTriage={vi.fn()} /></MemoryRouter>)
    expect(await screen.findByText('Beacon to cdn-sync-update.net')).toBeInTheDocument()
    expect(overviewApi.outcome).toHaveBeenCalledWith('working', 50)
    expect(screen.getByText('In 1 case')).toBeInTheDocument()
    expect(screen.getByText(/showing 2 of 3/)).toBeInTheDocument()
    fireEvent.click(screen.getByText('Possible exfiltration from cr-14'))
    expect(openCase).toHaveBeenCalledWith('SOC-4417')
    fireEvent.click(screen.getByText('Odd sign-in'))
    expect(onOpenAlert).toHaveBeenCalledWith('f-2')
    resolve({ data: list({ items: [{ ...list().items[1], finding_id: 'f-3', title: 'Third' }] }) })
    fireEvent.click(screen.getByRole('button', { name: 'Show 1 more' }))
    expect(await screen.findByText('Third')).toBeInTheDocument()
    expect(overviewApi.outcome).toHaveBeenLastCalledWith('working', 50, 2)
  })

  it('explains an empty outcome instead of showing a bare zero', async () => {
    resolve({ data: list({ state: 'scoring', label: 'Scoring', total: 0, items: [], cases: [], by_source: [] }) })
    render(<MemoryRouter><OutcomePanel state="scoring" onClose={vi.fn()} onOpenAlert={vi.fn()} openCase={vi.fn()} goTriage={vi.fn()} /></MemoryRouter>)
    expect(await screen.findByText(/Nothing is waiting to be scored/)).toBeInTheDocument()
  })
})

describe('AgentPanel', () => {
  it('shows what the agent does, its record and its runs', async () => {
    resolve({ data: record })
    const openRun = vi.fn()
    const openWorkflow = vi.fn()
    const openCase = vi.fn()
    render(<MemoryRouter><AgentPanel agent={agent} onClose={vi.fn()} openCase={openCase} openWorkflow={openWorkflow} openRun={openRun} /></MemoryRouter>)
    expect(await screen.findByText('Investigate cloud incidents.')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Cloud incident' })).toBeInTheDocument()
    expect(screen.getByText('75.0%')).toBeInTheDocument()
    expect(screen.getByText('Scope the identity blast radius')).toBeInTheDocument()
    const done = screen.getByText('Closed as benign: known backup job').closest('[role="button"]') as HTMLElement
    expect(within(done).getByText('4m 12s')).toBeInTheDocument()
    expect(within(done).getByText('$0.62')).toBeInTheDocument()
    fireEvent.click(done)
    expect(openRun).toHaveBeenCalledWith('run-done-1')
    fireEvent.click(screen.getByRole('button', { name: 'Case SOC-4417' }))
    expect(openCase).toHaveBeenCalledWith('SOC-4417')
    expect(openRun).toHaveBeenCalledTimes(1) // the case pill does not open the run
    fireEvent.click(screen.getByRole('button', { name: /Open in Agents & workflows/ }))
    expect(openWorkflow).toHaveBeenCalledWith('cloud-incident')
  })

  it('reads a markdown run summary as one plain line', async () => {
    const md = { ...record, live: [], recent: [{ ...record.recent[0], summary: '### Triage\nTriaged **f-1**.\n### Response\nContained.' }] }
    resolve({ data: md })
    render(<MemoryRouter><AgentPanel agent={agent} onClose={vi.fn()} openCase={vi.fn()} openWorkflow={vi.fn()} openRun={vi.fn()} /></MemoryRouter>)
    expect(await screen.findByText('Triage Triaged f-1. Response Contained.')).toBeInTheDocument()
  })
})
