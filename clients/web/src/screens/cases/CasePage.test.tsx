import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import CasesScreen from './CasesScreen'
import { ToastProvider } from '../../shell/toast'
import { casesApi, workflowApi } from '../../services/api'

const testState = vi.hoisted(() => ({
  cases: [] as Array<Record<string, unknown>>,
  runs: {} as Record<string, unknown>,
  recordError: null as string | null,
}))

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ hasPermission: () => true }),
}))

vi.mock('../../services/api', () => ({
  casesApi: {
    getAll: vi.fn(() => Promise.resolve({ data: { cases: testState.cases } })),
    getById: vi.fn((id: string) =>
      Promise.resolve({ data: testState.cases.find((item) => item.case_id === id) }),
    ),
    delete: vi.fn(),
    update: vi.fn(() => Promise.resolve({ data: { success: true } })),
    getSLA: vi.fn(() => Promise.resolve({ data: { resolution_due: '2026-06-20T12:00:00Z', health_status: 'warning' } })),
    getRecord: vi.fn(() => {
      if (testState.recordError) return Promise.reject({ response: { data: { detail: testState.recordError } } })
      return Promise.resolve({ data: { rows: [], run_id: null, investigation_id: null } })
    }),
    getComments: vi.fn(() => Promise.resolve({ data: { comments: [] } })),
    getTasks: vi.fn(() => Promise.resolve({ data: { tasks: [] } })),
    getEvidence: vi.fn(() => Promise.resolve({ data: { evidence: [] } })),
    getIOCs: vi.fn(() => Promise.resolve({ data: { iocs: [] } })),
    getEscalations: vi.fn(() => Promise.resolve({ data: { escalations: [] } })),
  },
  workflowApi: {
    getRun: vi.fn((id: string) => Promise.resolve({ data: testState.runs[id] ?? {} })),
    replayRun: vi.fn(),
    verifyRun: vi.fn(),
  },
  orchestratorApi: { exportInvestigation: vi.fn() },
  findingsApi: { getById: vi.fn() },
  caseSearchApi: { search: vi.fn() },
  timelineApi: { getCaseTimeline: vi.fn() },
}))

const HUNT = {
  iteration: 3,
  moves: [{ query_intent: 'who logged in', action: 'QUERY', worker_agent_id: 'threat_hunter' }],
  hypotheses: [
    { hypothesis_id: 'h1', statement: 'The host is owned', status: 'disproven', supports: 0, weakens: 2, resolution_reason: null },
    { hypothesis_id: 'h2', statement: 'Still forming', status: 'active', supports: 0, weakens: 0, resolution_reason: null },
  ],
  evidence: [
    { evidence_id: 'e1', iteration: 2, source_system: 'elastic', summary: 'no login', is_gap: false, gap_detail: null, bears_on: [{ hypothesis_id: 'h1', relation: 'weakens' }] },
  ],
  evidence_count: 1,
  calls: [{ question: 'who logged in', tool: 'search', result_length: 12, cost_usd: 0.01 }],
  recall: null,
  outcome: null,
  reason: '',
  cost_usd: 0.2,
}

function investigation(status: string, live: boolean, runId: string) {
  return {
    investigation_id: 'inv-1',
    status,
    workflow_id: 'incident-response',
    run_id: runId,
    live,
    cost_usd: 1,
    max_cost_usd: 5,
    budget_health: 'healthy',
    iteration_count: 1,
  }
}

function renderCase(id: string) {
  return render(
    <MemoryRouter initialEntries={[`/cases?case=${id}`]}>
      <ToastProvider>
        <Routes>
          <Route path="/cases" element={<CasesScreen openChat={vi.fn()} go={vi.fn()} goSettings={vi.fn()} setViewFull={vi.fn()} />} />
        </Routes>
      </ToastProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  testState.cases = []
  testState.runs = {}
  testState.recordError = null
  vi.mocked(workflowApi.getRun).mockClear()
})

describe('case page', () => {
  it('reads a hunt fold: standings, evidence, and a missing latency as a dash', async () => {
    testState.cases = [{
      case_id: 'case-hunt',
      title: 'Hunt case',
      status: 'open',
      priority: 'high',
      assignee: 'ada',
      finding_ids: ['f1', 'f2'],
      created_at: '2026-06-15T09:14:00Z',
      combined_state: 'executing',
      investigations: [investigation('executing', true, 'run-hunt')],
    }]
    testState.runs['run-hunt'] = { hunt: HUNT }
    renderCase('case-hunt')

    expect(await screen.findByRole('heading', { name: 'Hunt case' })).toBeInTheDocument()
    const header = document.querySelector('.detail-head') as HTMLElement
    expect(within(header).getByText('executing')).toBeInTheDocument()
    expect(screen.getByText('2 alerts combined')).toBeInTheDocument()
    expect(await screen.findByText(/who logged in · threat_hunter/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: /Explanations/ }))
    expect(await screen.findByText('ruled out')).toBeInTheDocument()
    expect(screen.getByText('forming')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: /^Evidence/ }))
    expect(await screen.findByText('no login')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: /Checked/ }))
    const latency = await screen.findByRole('columnheader', { name: 'Latency' })
    expect(within(latency.closest('table') as HTMLElement).getByText('—')).toBeInTheDocument()
  })

  it('gives an investigate run the honest line and puts findings in the evidence table', async () => {
    testState.cases = [{
      case_id: 'case-inv',
      title: 'Investigate case',
      status: 'open',
      priority: 'medium',
      finding_ids: [],
      created_at: '2026-06-15T09:14:00Z',
      combined_state: 'open',
      investigations: [investigation('completed', false, 'run-inv')],
    }]
    testState.runs['run-inv'] = {
      projection: {
        iterations: 2,
        decisions: [{ action: 'EXAMINE', rationale: 'look', worker: 'lead' }],
        findings: [{ agent_id: 'lead', answer: 'benign traffic' }],
        calls: [],
        gaps: [{ dispatch_id: 'dsp-1', agent_id: 'worker', failure_reason: 'tool down', query_intent: 'list users' }],
        recall: null,
        outcome: 'completed',
        reason: 'done',
        cost_usd: 0.1,
      },
    }
    renderCase('case-inv')

    expect(await screen.findByText(/EXAMINE · lead/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: /Explanations/ }))
    expect(await screen.findByText('This workflow does not test explanations yet.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: /^Evidence/ }))
    expect(await screen.findByText('benign traffic')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: /Memory/ }))
    expect(await screen.findByText(/list users/)).toBeInTheDocument()
    expect(screen.getByText(/tool down/)).toBeInTheDocument()
  })

  it('keeps empty tabs in the strip', async () => {
    testState.cases = [{
      case_id: 'case-empty',
      title: 'Empty case',
      status: 'open',
      priority: 'low',
      finding_ids: [],
      created_at: '2026-06-15T09:14:00Z',
      combined_state: 'open',
      investigations: [],
    }]
    renderCase('case-empty')
    expect(await screen.findByRole('heading', { name: 'Empty case' })).toBeInTheDocument()
    for (const name of ['Summary', 'Explanations', 'Evidence', 'Checked', 'Memory and blind spots', 'Record']) {
      expect(screen.getByRole('tab', { name: new RegExp(name) })).toBeInTheDocument()
    }
    fireEvent.click(screen.getByRole('tab', { name: /Evidence/ }))
    expect(await screen.findByText('No evidence yet')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: /Checked/ }))
    expect(await screen.findByText('No questions asked yet')).toBeInTheDocument()
    expect(workflowApi.getRun).not.toHaveBeenCalled()
  })

  it('shows a closed verdict and reopens through the status update', async () => {
    testState.cases = [{
      case_id: 'case-closed',
      title: 'Closed case',
      status: 'closed',
      priority: 'high',
      finding_ids: [],
      created_at: '2026-06-15T09:14:00Z',
      combined_state: 'closed',
      closure: { closure_category: 'false_positive', closed_by: 'ada', closed_by_kind: 'analyst', verdict: 'the scanner' },
      investigations: [investigation('completed', false, 'run-closed')],
    }]
    testState.runs['run-closed'] = { projection: { iterations: 1, decisions: [], findings: [], calls: [], gaps: [], recall: null } }
    renderCase('case-closed')

    expect(await screen.findByText('the scanner')).toBeInTheDocument()
    expect(screen.getByText(/false_positive/)).toBeInTheDocument()
    expect(screen.getByText(/ada/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Reopen' }))
    await waitFor(() => expect(casesApi.update).toHaveBeenCalledWith('case-closed', { status: 'open' }))
  })

  it('shows a failed record read', async () => {
    testState.recordError = 'the agent layer answered 502'
    testState.cases = [{
      case_id: 'case-record',
      title: 'Record case',
      status: 'open',
      priority: 'low',
      finding_ids: [],
      created_at: '2026-06-15T09:14:00Z',
      combined_state: 'open',
      investigations: [],
    }]
    renderCase('case-record')
    fireEvent.click(await screen.findByRole('tab', { name: /Record/ }))
    expect(await screen.findByText('the agent layer answered 502')).toBeInTheDocument()
  })
})
