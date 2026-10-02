import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import CasesScreen from './CasesScreen'
import { ToastProvider } from '../../shell/toast'
import { approvalsApi, casesApi, streamFetch, workflowApi, type NeedsYouItem } from '../../services/api'

const testState = vi.hoisted(() => ({
  cases: [] as Array<Record<string, unknown>>,
  runs: {} as Record<string, unknown>,
  recordError: null as string | null,
  recordRows: [] as Array<Record<string, unknown>>,
  convos: [] as Array<{ id: string; case_id: string; messages: Array<{ role: string; content: string }> }>,
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
      return Promise.resolve({ data: { rows: testState.recordRows, run_id: null, investigation_id: null } })
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
  default: { get: vi.fn(() => Promise.resolve({ data: {} })) },
  agentsApi: { listAgents: vi.fn(() => Promise.resolve({ data: { agents: [] } })) },
  conversationsApi: {
    list: vi.fn((params?: { q?: string }) => {
      const q = params?.q
      const conversations = testState.convos.filter((row) => !q || row.case_id === q)
      return Promise.resolve({ data: { conversations } })
    }),
    get: vi.fn((id: string) => {
      const row = testState.convos.find((item) => item.id === id)
      return Promise.resolve({ data: row ?? { id, case_id: null, messages: [] } })
    }),
    update: vi.fn(() => Promise.resolve({ data: {} })),
    delete: vi.fn(),
    importHistory: vi.fn(() => Promise.resolve({ data: {} })),
  },
  reasoningApi: {
    getSessionSummary: vi.fn(() => Promise.resolve(null)),
    listInteractions: vi.fn(() => Promise.resolve({ interactions: [] })),
    getInteraction: vi.fn(),
  },
  streamFetch: vi.fn(() => Promise.resolve({
    ok: true,
    status: 200,
    body: { getReader: () => ({ read: () => Promise.resolve({ done: true, value: undefined }) }) },
  })),
  approvalsApi: {
    needsYou: vi.fn(() => Promise.resolve({ data: { count: 0, items: [] } })),
    approve: vi.fn(() => Promise.resolve({})),
    reject: vi.fn(() => Promise.resolve({})),
  },
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

function need(over: Partial<NeedsYouItem> = {}): NeedsYouItem {
  return {
    kind: 'approval',
    source_id: 'act-1',
    title: 'Block 1.2.3.4',
    reason: 'beacon',
    created_at: '2026-06-15T09:00:00Z',
    reversibility: 'reversible',
    case_id: 'case-dec',
    ...over,
  }
}

beforeEach(() => {
  testState.cases = []
  testState.runs = {}
  testState.recordError = null
  testState.recordRows = []
  testState.convos = []
  vi.mocked(workflowApi.getRun).mockClear()
  vi.mocked(streamFetch).mockClear()
  vi.mocked(approvalsApi.needsYou).mockResolvedValue({ data: { count: 0, items: [] } } as never)
  vi.mocked(approvalsApi.approve).mockReset()
  vi.mocked(approvalsApi.reject).mockReset()
  vi.mocked(approvalsApi.approve).mockResolvedValue({} as never)
  vi.mocked(approvalsApi.reject).mockResolvedValue({} as never)
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

  it('lists linked findings under the count on every tab', async () => {
    testState.cases = [{
      case_id: 'case-links',
      title: 'Linked case',
      status: 'open',
      priority: 'high',
      finding_ids: ['f1', 'gone', 'f2'],
      created_at: '2026-06-15T09:14:00Z',
      combined_state: 'executing',
      investigations: [investigation('executing', true, 'run-links')],
      linked_findings: [
        { finding_id: 'f1', description: 'console alert', source_link: 'https://example.test/alert/1' },
        { finding_id: 'f2', description: 'no door', source_link: null },
      ],
    }]
    testState.runs['run-links'] = { hunt: HUNT }
    renderCase('case-links')

    const header = (await screen.findByRole('heading', { name: 'Linked case' })).closest('.detail-head') as HTMLElement
    expect(within(header).getByText('3 alerts combined')).toBeInTheDocument()
    expect(within(header).getByText('console alert')).toBeInTheDocument()
    expect(within(header).getByText('no door')).toBeInTheDocument()
    expect(within(header).queryByText('gone')).not.toBeInTheDocument()
    const link = within(header).getByRole('link', { name: 'Open in source' })
    expect(link).toHaveAttribute('href', 'https://example.test/alert/1')
    expect(within(header).getAllByRole('link', { name: 'Open in source' })).toHaveLength(1)

    fireEvent.click(screen.getByRole('tab', { name: /^Evidence/ }))
    expect(await screen.findByText('no login')).toBeInTheDocument()
    expect(within(header).getByRole('link', { name: 'Open in source' })).toBeInTheDocument()
    const evidence = screen.getByText('no login').closest('table') ?? screen.getByText('no login').closest('section')
    expect(evidence).toBeTruthy()
    expect(within(evidence as HTMLElement).queryByRole('link', { name: 'Open in source' })).not.toBeInTheDocument()
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

  it('pins Ask on the case and keeps Tell and Do from posting', async () => {
    testState.cases = [{
      case_id: 'case-hunt',
      title: 'Hunt case',
      status: 'open',
      priority: 'high',
      finding_ids: ['f1'],
      created_at: '2026-06-15T09:14:00Z',
      combined_state: 'open',
      investigations: [],
    }]
    renderCase('case-hunt')
    expect(await screen.findByText('Private to you · Ask only')).toBeInTheDocument()
    expect(document.querySelector('.case-ask')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Tell' }))
    fireEvent.click(screen.getByRole('button', { name: 'Do' }))
    expect(screen.getByRole('button', { name: 'Tell' })).toHaveAttribute('type', 'button')
    expect(screen.getByRole('button', { name: 'Tell' })).toHaveAttribute('title', 'Coming in a later release')
    expect(screen.getByRole('button', { name: 'Do' })).toHaveAttribute('type', 'button')
    expect(screen.getByRole('button', { name: 'Do' })).toHaveAttribute('title', 'Coming in a later release')
    expect(streamFetch).not.toHaveBeenCalled()
  })

  it('sends Why? on this case with promptFor and no model', async () => {
    testState.cases = [{
      case_id: 'case-hunt',
      title: 'Hunt case',
      status: 'open',
      priority: 'high',
      finding_ids: ['f1', 'f2'],
      created_at: '2026-06-15T09:14:00Z',
      combined_state: 'executing',
      investigations: [investigation('executing', true, 'run-hunt')],
    }]
    testState.runs['run-hunt'] = { hunt: HUNT }
    testState.recordRows = [{
      id: 'row-1',
      at: '2026-06-15T10:00:00Z',
      kind: 'agent',
      source: 'run',
      chained: true,
      text: 'because the login failed',
    }]
    renderCase('case-hunt')
    const header = (await screen.findByRole('heading', { name: 'Hunt case' })).closest('.detail-head') as HTMLElement
    expect(within(header).getByText('executing')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: /Record/ }))
    fireEvent.click(await screen.findByRole('button', { name: 'Why?' }))
    await waitFor(() => expect(streamFetch).toHaveBeenCalled())
    const body = JSON.parse((vi.mocked(streamFetch).mock.calls[0][1] as { body: string }).body)
    expect(body.case_id).toBe('case-hunt')
    expect(body.model).toBeUndefined()
    expect(body.page_context).toBe('cases')
    expect(body.messages.at(-1)).toEqual({
      role: 'user',
      content: 'Investigate case case-hunt: "Hunt case" — high priority, status executing, 2 linked findings.\n\nWhy?\nbecause the login failed',
    })
  })

  it('chips a hunt evidence id and opens that row, and drops the thread when the case changes', async () => {
    testState.cases = [
      {
        case_id: 'case-hunt',
        title: 'Hunt case',
        status: 'open',
        priority: 'high',
        finding_ids: ['f1'],
        created_at: '2026-06-15T09:14:00Z',
        combined_state: 'executing',
        investigations: [investigation('executing', true, 'run-hunt')],
      },
      {
        case_id: 'case-next',
        title: 'Next case',
        status: 'open',
        priority: 'low',
        finding_ids: [],
        created_at: '2026-06-15T09:14:00Z',
        combined_state: 'open',
        investigations: [],
      },
    ]
    testState.runs['run-hunt'] = { hunt: HUNT }
    testState.convos = [{
      id: 'conv-hunt',
      case_id: 'case-hunt',
      messages: [{ role: 'assistant', content: 'The row e1 matters; ghost-9 does not.' }],
    }]
    renderCase('case-hunt')

    expect(await screen.findByRole('button', { name: 'e1' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'ghost-9' })).not.toBeInTheDocument()
    expect(screen.queryByText('no login')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'e1' }))
    expect(screen.getByRole('tab', { name: /^Evidence/ })).toHaveAttribute('aria-selected', 'true')
    const row = (await screen.findByText('no login')).closest('tr')
    expect(row).toHaveAttribute('data-evidence-id', 'e1')

    fireEvent.click(screen.getByText('Next case'))
    expect(await screen.findByRole('heading', { name: 'Next case' })).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByText(/The row e1 matters/)).not.toBeInTheDocument())
  })

  it('approves a reversible ask on one press and holds an irreversible ask for 1600ms', async () => {
    testState.cases = [{
      case_id: 'case-dec',
      title: 'Decision case',
      status: 'open',
      priority: 'high',
      finding_ids: [],
      created_at: '2026-06-15T09:14:00Z',
      combined_state: 'open',
      investigations: [],
    }]
    vi.mocked(approvalsApi.needsYou).mockResolvedValue({
      data: {
        count: 2,
        items: [
          need(),
          need({
            source_id: 'act-irr',
            title: 'Isolate host',
            reason: '',
            kind: 'checkpoint',
            reversibility: 'irreversible',
          }),
        ],
      },
    } as never)
    renderCase('case-dec')

    expect(await screen.findByRole('heading', { name: 'Block 1.2.3.4' })).toBeInTheDocument()
    const body = document.querySelector('.detail-body') as HTMLElement
    expect(body.textContent?.indexOf('Block 1.2.3.4')).toBeLessThan(body.textContent?.indexOf('Now · phase') ?? -1)
    expect(body.textContent?.indexOf('Block 1.2.3.4')).toBeLessThan(body.textContent?.indexOf('Isolate host') ?? -1)
    expect(screen.getByText('beacon')).toBeInTheDocument()
    expect(screen.getByText('approval · reversible')).toBeInTheDocument()
    expect(screen.getByText('checkpoint · irreversible')).toBeInTheDocument()
    expect(approvalsApi.needsYou).toHaveBeenCalledWith('case-dec')

    const before = vi.mocked(approvalsApi.needsYou).mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    expect(approvalsApi.approve).toHaveBeenCalledWith('act-1')
    await waitFor(() => expect(vi.mocked(approvalsApi.needsYou).mock.calls.length).toBeGreaterThan(before))

    const hold = screen.getByRole('button', { name: /Press and hold to confirm/ })
    vi.useFakeTimers()
    try {
      fireEvent.pointerDown(hold)
      act(() => {
        vi.advanceTimersByTime(200)
      })
      fireEvent.pointerUp(hold)
      act(() => {
        vi.advanceTimersByTime(1600)
      })
      expect(approvalsApi.approve).not.toHaveBeenCalledWith('act-irr')

      fireEvent.pointerDown(hold)
      act(() => {
        vi.advanceTimersByTime(1600)
      })
      expect(approvalsApi.approve).toHaveBeenCalledWith('act-irr')
    } finally {
      vi.useRealTimers()
    }
  })

  it('sends the typed reason when rejecting from the case', async () => {
    testState.cases = [{
      case_id: 'case-dec',
      title: 'Decision case',
      status: 'open',
      priority: 'high',
      finding_ids: [],
      created_at: '2026-06-15T09:14:00Z',
      combined_state: 'open',
      investigations: [],
    }]
    vi.mocked(approvalsApi.needsYou).mockResolvedValue({
      data: { count: 1, items: [need({ source_id: 'act-no', title: 'Disable account' })] },
    } as never)
    renderCase('case-dec')

    fireEvent.click(await screen.findByRole('button', { name: 'Reject' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Rejection reason' }), {
      target: { value: 'not our host' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }))
    expect(approvalsApi.reject).toHaveBeenCalledWith('act-no', 'not our host')
  })

  it('puts the decision block ahead of the verdict on a closed case', async () => {
    testState.cases = [{
      case_id: 'case-closed',
      title: 'Closed case',
      status: 'closed',
      priority: 'high',
      finding_ids: [],
      created_at: '2026-06-15T09:14:00Z',
      combined_state: 'closed',
      closure: { closure_category: 'false_positive', closed_by: 'ada', closed_by_kind: 'analyst', verdict: 'the scanner' },
      investigations: [],
    }]
    vi.mocked(approvalsApi.needsYou).mockResolvedValue({
      data: { count: 1, items: [need({ title: 'Quarantine mailbox', case_id: 'case-closed' })] },
    } as never)
    renderCase('case-closed')

    expect(await screen.findByRole('heading', { name: 'Quarantine mailbox' })).toBeInTheDocument()
    const body = document.querySelector('.detail-body') as HTMLElement
    expect(body.textContent?.indexOf('Quarantine mailbox')).toBeLessThan(body.textContent?.indexOf('Verdict') ?? -1)
    expect(screen.getByText('the scanner')).toBeInTheDocument()
    expect(screen.queryByText(/Now · phase/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Needs you' })).not.toBeInTheDocument()
  })

  it('selects Summary from the needs-you strip on another tab', async () => {
    testState.cases = [{
      case_id: 'case-dec',
      title: 'Decision case',
      status: 'open',
      priority: 'high',
      finding_ids: [],
      created_at: '2026-06-15T09:14:00Z',
      combined_state: 'open',
      investigations: [],
    }]
    vi.mocked(approvalsApi.needsYou).mockResolvedValue({
      data: { count: 1, items: [need()] },
    } as never)
    renderCase('case-dec')

    expect(await screen.findByRole('heading', { name: 'Block 1.2.3.4' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Needs you' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: /^Evidence/ }))
    fireEvent.click(await screen.findByRole('button', { name: 'Needs you' }))
    expect(screen.getByRole('tab', { name: /^Summary/ })).toHaveAttribute('aria-selected', 'true')
    expect(screen.queryByRole('button', { name: 'Needs you' })).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Block 1.2.3.4' })).toBeInTheDocument()
  })
})
