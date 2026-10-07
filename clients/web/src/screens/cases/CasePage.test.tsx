import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { format } from 'date-fns'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import CasesScreen, { CasesDetail } from './CasesScreen'
import { CASES_CHANGED } from './useCases'
import { ToastProvider } from '../../shell/toast'
import { approvalsApi, casesApi, streamFetch, workflowApi, type NeedsYouItem } from '../../services/api'

const testState = vi.hoisted(() => ({
  canDelete: true,
  cases: [] as Array<Record<string, unknown>>,
  getByIdError: false,
  runs: {} as Record<string, unknown>,
  recordError: null as string | null,
  recordRows: [] as Array<Record<string, unknown>>,
  convos: [] as Array<{ id: string; case_id: string; messages: Array<{ role: string; content: string }> }>,
}))

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ hasPermission: (p: string) => p !== 'cases.delete' || testState.canDelete }),
}))

vi.mock('../../services/api', () => ({
  casesApi: {
    getAll: vi.fn(() => Promise.resolve({ data: { cases: testState.cases } })),
    getById: vi.fn((id: string) =>
      testState.getByIdError ? Promise.reject(new Error('backend down')) : Promise.resolve({ data: testState.cases.find((item) => item.case_id === id) }),
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
    listAll: vi.fn(() => Promise.resolve({ data: { workflows: [{ id: 'incident-response', name: 'Incident response' }] } })),
    getRun: vi.fn((id: string) => Promise.resolve({ data: { run_id: id, ...(testState.runs[id] as object) } })),
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
  moves: [
    { query_intent: 'who logged in', action: 'QUERY', worker_agent_id: 'threat_hunter', iteration: 3, created_at: '2026-06-15T09:14:00Z' },
    { query_intent: 'rank the hosts', action: 'QUERY', worker_agent_id: 'threat_hunter', iteration: 2, created_at: '2026-06-15T09:10:00Z' },
    { query_intent: 'read the proxy log', action: 'QUERY', worker_agent_id: 'network_analyst', iteration: 1, created_at: '2026-06-15T09:05:00Z' },
  ],
  hypotheses: [
    { hypothesis_id: 'h1', statement: 'The host is owned', status: 'disproven', supports: 0, weakens: 2, resolution_reason: 'contradicted by telemetry', provenance: 'operator' },
    { hypothesis_id: 'h2', statement: 'Still forming', status: 'active', supports: 0, weakens: 0, resolution_reason: null, provenance: '' },
  ],
  evidence: [
    { evidence_id: 'e1', iteration: 2, source_system: 'elastic', summary: 'no login', is_gap: false, gap_detail: null, bears_on: [{ hypothesis_id: 'h1', relation: 'weakens' }] },
  ],
  evidence_count: 1,
  calls: [
    { question: 'who logged in', tool: 'search', result_length: 12, cost_usd: 0.01, iteration: 3 },
    { question: 'read the proxy log', tool: 'proxy_query', result_length: 4, cost_usd: 0.01, duration_ms: 40, iteration: 1 },
  ],
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
          <Route path="/cases" element={<CasesScreen openChat={vi.fn()} go={vi.fn()} goSettings={vi.fn()} openCase={vi.fn()} setViewFull={vi.fn()} />} />
        </Routes>
      </ToastProvider>
    </MemoryRouter>,
  )
}

/** The case page alone, as the drawer renders it. */
function renderDetail(id: string, props: { onBack?: () => void; onExpand?: () => void } = {}) {
  const ui = (caseId: string) => (
    <MemoryRouter>
      <ToastProvider>
        <CasesDetail id={caseId} onBack={props.onBack ?? vi.fn()} onExpand={props.onExpand} pageKey="cases" />
      </ToastProvider>
    </MemoryRouter>
  )
  const view = render(ui(id))
  return { ...view, showCase: (caseId: string) => view.rerender(ui(caseId)) }
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

function openCase(id: string) {
  return {
    case_id: id,
    title: 'Open case',
    status: 'open',
    priority: 'high',
    finding_ids: [],
    created_at: '2026-06-15T09:14:00Z',
    combined_state: 'open',
    investigations: [],
  }
}

beforeEach(() => {
  testState.canDelete = true
  testState.getByIdError = false
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
    expect(within(header).getByText('Executing')).toBeInTheDocument()
    expect(screen.getByText('2 alerts combined')).toBeInTheDocument()
    const now = await screen.findByRole('region', { name: 'Now' })
    expect(within(now).getByText('Now · step 3')).toBeInTheDocument()
    expect(within(now).getByText('who logged in')).toBeInTheDocument()
    const clock = format(new Date('2026-06-15T09:14:00Z'), 'HH:mm')
    expect(within(now).getByText(`threat_hunter · search · since ${clock}`)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: /Explanations/ }))
    expect(await screen.findByText('Ruled out')).toBeInTheDocument()
    expect(screen.getByText('Forming')).toBeInTheDocument()
    expect(screen.getByText('contradicted by telemetry')).toBeInTheDocument()
    expect(screen.getByText('Added by you')).toBeInTheDocument()
    expect(screen.getAllByText(/^Added by/)).toHaveLength(1)
    expect(screen.getAllByText('0 for')).toHaveLength(2)
    expect(screen.getByText('2 against')).toBeInTheDocument()
    for (const name of ['+ Add an explanation', 'Rule one out']) {
      expect(screen.getByRole('button', { name })).toBeDisabled()
    }

    fireEvent.click(screen.getByRole('tab', { name: /^Evidence/ }))
    expect(await screen.findByText('no login')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: /Checked/ }))
    const latency = await screen.findByRole('columnheader', { name: 'Latency' })
    expect(within(latency.closest('table') as HTMLElement).getByText('—')).toBeInTheDocument()
  })

  const huntCase = (id: string) => {
    testState.cases = [{
      case_id: id,
      title: 'Explanations case',
      status: 'open',
      priority: 'high',
      finding_ids: [],
      created_at: '2026-06-15T09:14:00Z',
      combined_state: 'executing',
      investigations: [investigation('executing', true, `run-${id}`)],
    }]
  }

  it('shows every explanation word and provenance on the Explanations tab', async () => {
    huntCase('case-words')
    const hyp = (hypothesis_id: string, status: string, supports: number, weakens: number, provenance: string, resolution_reason: string | null = null) =>
      ({ hypothesis_id, statement: `claim ${hypothesis_id}`, status, supports, weakens, resolution_reason, provenance })
    testState.runs['run-case-words'] = {
      hunt: {
        ...HUNT,
        hypotheses: [
          hyp('a', 'proven', 3, 0, 'hunt_spec', 'confirmed by the hash'),
          hyp('b', 'active', 2, 0, 'operator'),
          hyp('c', 'active', 0, 0, 'base_rate'),
          hyp('d', 'active', 1, 1, 'deployment_gap'),
          hyp('e', 'disproven', 0, 2, 'mystery_token'),
          hyp('f', 'inconclusive', 0, 0, ''),
          hyp('g', 'parked', 0, 0, '', 'waiting on logs'),
          hyp('h', 'handed_off', 0, 0, '', 'sent to the reviewer'),
        ],
      },
    }
    renderCase('case-words')
    fireEvent.click(await screen.findByRole('tab', { name: /Explanations/ }))
    expect(await screen.findByText('claim a')).toBeInTheDocument()
    for (const word of ['Proven', 'Standing', 'Forming', 'Weakened', 'Ruled out', 'Inconclusive', 'Parked', 'Handed off']) {
      expect(screen.getByText(word)).toBeInTheDocument()
    }
    expect(screen.getByText('claim e')).toHaveClass('struck')
    expect(screen.getByText('claim a')).not.toHaveClass('struck')
    for (const by of ['the hunt definition', 'you', 'the base rate', 'the deployment-gap check', 'mystery_token']) {
      expect(screen.getByText(`Added by ${by}`)).toBeInTheDocument()
    }
    expect(screen.getAllByText(/^Added by/)).toHaveLength(5)
    expect(screen.getByText('confirmed by the hash')).toBeInTheDocument()
    expect(screen.getByText('waiting on logs')).toBeInTheDocument()
    expect(screen.getByText('sent to the reviewer')).toBeInTheDocument()
  })

  it('shows an empty, loading and failed Explanations tab', async () => {
    huntCase('case-empty-expl')
    testState.runs['run-case-empty-expl'] = { hunt: { ...HUNT, hypotheses: [] } }
    const view = renderCase('case-empty-expl')
    fireEvent.click(await screen.findByRole('tab', { name: /Explanations/ }))
    expect(await screen.findByText('No explanations yet')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Rule one out' })).toBeDisabled()
    view.unmount()

    huntCase('case-loading')
    vi.mocked(workflowApi.getRun).mockImplementationOnce(() => new Promise(() => {}))
    const loading = renderCase('case-loading')
    fireEvent.click(await screen.findByRole('tab', { name: /Explanations/ }))
    expect(await screen.findByText('Loading the run…')).toBeInTheDocument()
    expect(screen.queryByText('This workflow does not test explanations yet.')).not.toBeInTheDocument()
    loading.unmount()

    huntCase('case-failed')
    vi.mocked(workflowApi.getRun).mockRejectedValueOnce(new Error('down'))
    renderCase('case-failed')
    fireEvent.click(await screen.findByRole('tab', { name: /Explanations/ }))
    expect((await screen.findAllByText('The run could not be read.')).length).toBeGreaterThan(0)
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

    const now = await screen.findByRole('region', { name: 'Now' })
    expect(within(now).getByText('Now · step 2')).toBeInTheDocument()
    expect(within(now).getByText('EXAMINE')).toBeInTheDocument()
    expect(within(now).getByText('lead · since —')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: /Explanations/ }))
    expect(await screen.findByText('This workflow does not test explanations yet.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'About explanations' })).toBeInTheDocument()
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

  it('lists one agent per worker with its tool, and shows the door tiles with their sub-lines', async () => {
    testState.cases = [{ ...openCase('case-agents'), combined_state: 'executing', investigations: [investigation('executing', true, 'run-hunt')] }]
    testState.runs['run-hunt'] = { hunt: HUNT }
    testState.recordRows = [
      { id: 'r1', kind: 'run', text: 'started', at: '2026-06-15T09:00:00Z', chained: true },
      { id: 'r2', kind: 'directive', text: 'note', at: '2026-06-15T09:01:00Z', chained: false },
    ]
    renderCase('case-agents')

    const table = await screen.findByRole('table', { name: 'Agents' })
    const rows = within(table).getAllByRole('row')
    expect(rows).toHaveLength(2)
    const clock = format(new Date('2026-06-15T09:14:00Z'), 'HH:mm')
    for (const text of ['threat_hunter', 'who logged in', 'search', clock, 'executing']) {
      expect(within(rows[0]).getByText(text)).toBeInTheDocument()
    }
    // The older move of the same worker is not a second row; the state sits on the latest row only.
    expect(within(table).queryByText('rank the hosts')).not.toBeInTheDocument()
    expect(within(rows[1]).getByText('network_analyst')).toBeInTheDocument()
    expect(within(rows[1]).getByText('proxy_query')).toBeInTheDocument()
    expect(within(rows[1]).queryByText('executing')).not.toBeInTheDocument()
    expect(screen.queryByText(/incident-response · executing/)).not.toBeInTheDocument()

    const doors = screen.getByRole('region', { name: 'Audit doors' })
    expect(within(doors).getAllByRole('button')).toHaveLength(5)
    expect(within(doors).getByText('1 forming · 1 ruled out')).toBeInTheDocument()
    expect(within(doors).getByText('0 for · 1 against · shown')).toBeInTheDocument()
    expect(within(doors).getByText('$0.2000 · 0 gaps')).toBeInTheDocument()
    expect(within(doors).getByText('Nothing recalled')).toBeInTheDocument()
    await waitFor(() => expect(within(doors).getByText('2 rows · 1 chained')).toBeInTheDocument())
    fireEvent.click(within(doors).getByRole('button', { name: /^Checked/ }))
    expect(await screen.findByRole('columnheader', { name: 'Latency' })).toBeInTheDocument()
  })

  it('shows a run started on the case with no investigation row, before it has reported', async () => {
    testState.cases = [{
      ...openCase('case-run-only'),
      combined_state: 'executing',
      investigations: [{ ...investigation('running', true, 'run-only'), investigation_id: null }],
    }]
    testState.runs['run-only'] = { hunt: null }
    renderCase('case-run-only')

    const now = await screen.findByRole('region', { name: 'Now' })
    expect(within(now).getByText('The run has started and has not reported yet.')).toBeInTheDocument()
    expect(screen.queryByText('No run on this case yet.')).not.toBeInTheDocument()
    expect(workflowApi.getRun).toHaveBeenCalledWith('run-only')
    expect(screen.queryByRole('button', { name: 'Export' })).not.toBeInTheDocument()
  })

  it('shows a dash for a lead decision time and keeps the honest explanations line', async () => {
    testState.cases = [{ ...openCase('case-lead'), combined_state: 'executing', investigations: [investigation('executing', true, 'run-lead')] }]
    testState.runs['run-lead'] = {
      projection: {
        iterations: 4,
        decisions: [
          { action: 'DISPATCH', rationale: 'first', worker: 'worker_a' },
          { action: 'EXAMINE', rationale: 'second', worker: 'worker_b' },
        ],
        findings: [],
        calls: [{ question: 'q', tool: 'search', result_length: 1, cost_usd: 0, iteration: 1 }],
        gaps: [],
        recall: null,
        outcome: null,
        reason: '',
        cost_usd: 0.5,
      },
    }
    renderCase('case-lead')

    const table = await screen.findByRole('table', { name: 'Agents' })
    const rows = within(table).getAllByRole('row')
    expect(rows.map((row) => row.textContent)).toEqual([expect.stringContaining('worker_bEXAMINE——'), expect.stringContaining('worker_aDISPATCH——')])
    expect(within(await screen.findByRole('region', { name: 'Now' })).getByText('worker_b · since —')).toBeInTheDocument()
    const doors = screen.getByRole('region', { name: 'Audit doors' })
    expect(within(doors).getByText('Does not test explanations yet')).toBeInTheDocument()
    expect(within(doors).getByText('Findings, no for or against')).toBeInTheDocument()
  })

  it('keeps the Now card copy for no run, a loading run and an unreadable run', async () => {
    testState.cases = [
      { ...openCase('case-none'), investigations: [] },
      { ...openCase('case-slow'), combined_state: 'executing', investigations: [investigation('executing', true, 'run-slow')] },
      { ...openCase('case-bad'), combined_state: 'executing', investigations: [investigation('executing', true, 'run-bad')] },
    ]
    renderCase('case-none')
    expect(await screen.findByText('No run on this case yet.')).toBeInTheDocument()
    expect(screen.getByText('No live investigation.')).toBeInTheDocument()
    expect(within(screen.getByRole('region', { name: 'Audit doors' })).getAllByText('No run yet')).toHaveLength(3)
    cleanup()

    vi.mocked(workflowApi.getRun).mockReturnValueOnce(new Promise(() => undefined) as never)
    renderCase('case-slow')
    expect(await screen.findAllByText('Loading the run…')).not.toHaveLength(0)
    expect(within(screen.getByRole('region', { name: 'Audit doors' })).getAllByText('Loading…')).toHaveLength(3)
    cleanup()

    vi.mocked(workflowApi.getRun).mockRejectedValueOnce(new Error('down'))
    renderCase('case-bad')
    expect(await screen.findAllByText('The run could not be read.')).not.toHaveLength(0)
    expect(within(screen.getByRole('region', { name: 'Audit doors' })).getAllByText('Couldn’t read the run')).toHaveLength(3)
  })

  it('shows the door tiles on a closed case, without a Now card', async () => {
    testState.cases = [{
      ...openCase('case-done'),
      status: 'closed',
      combined_state: 'closed',
      closure: { closure_category: 'false_positive', closed_by: 'ada', closed_by_kind: 'analyst', verdict: 'the scanner' },
      investigations: [],
    }]
    renderCase('case-done')
    const doors = await screen.findByRole('region', { name: 'Audit doors' })
    expect(within(doors).getAllByRole('button')).toHaveLength(5)
    expect(screen.queryByRole('region', { name: 'Now' })).not.toBeInTheDocument()
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
    expect(screen.getByText('Closed by ada')).toBeInTheDocument()
    expect(document.querySelector('.case-sla')).toBeNull()
    expect(screen.getByText(/closed by ada \(analyst\)/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Reopen' }))
    await waitFor(() => expect(casesApi.update).toHaveBeenCalledWith('case-closed', { status: 'open' }))
  })

  it('lists linked findings in the Alerts fold on every tab', async () => {
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
        { finding_id: 'f1', title: 'Beacon to rare host', description: 'console alert', source_link: 'https://example.test/alert/1' },
        { finding_id: 'f2', description: 'no door', source_link: null },
      ],
    }]
    testState.runs['run-links'] = { hunt: HUNT }
    renderCase('case-links')

    const header = (await screen.findByRole('heading', { name: 'Linked case' })).closest('.detail-head') as HTMLElement
    expect(within(header).getByText('3 alerts combined')).toBeInTheDocument()
    expect(within(header).queryByText('console alert')).not.toBeInTheDocument()
    const side = screen.getByRole('complementary', { name: 'Case details' })
    expect(within(side).getByText('Alerts (2)')).toBeInTheDocument()
    // The title names the alert; a row without one keeps its description.
    expect(within(side).getByText('Beacon to rare host')).toBeInTheDocument()
    expect(within(side).queryByText('console alert')).not.toBeInTheDocument()
    expect(within(side).getByText('no door')).toBeInTheDocument()
    expect(within(side).queryByText('gone')).not.toBeInTheDocument()
    const link = within(side).getByRole('link', { name: 'Open in source' })
    expect(link).toHaveAttribute('href', 'https://example.test/alert/1')
    expect(within(side).getAllByRole('link', { name: 'Open in source' })).toHaveLength(1)

    fireEvent.click(screen.getByRole('tab', { name: /^Evidence/ }))
    expect(await screen.findByText('no login')).toBeInTheDocument()
    expect(within(side).getByRole('link', { name: 'Open in source' })).toBeInTheDocument()
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
    expect(within(header).getByText('Executing')).toBeInTheDocument()
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
    const { showCase } = renderDetail('case-hunt')

    expect(await screen.findByRole('button', { name: 'e1' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'ghost-9' })).not.toBeInTheDocument()
    expect(screen.queryByText('no login')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'e1' }))
    expect(screen.getByRole('tab', { name: /^Evidence/ })).toHaveAttribute('aria-selected', 'true')
    const row = (await screen.findByText('no login')).closest('tr')
    expect(row).toHaveAttribute('data-evidence-id', 'e1')

    showCase('case-next')
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
    expect(body.textContent?.indexOf('Block 1.2.3.4')).toBeLessThan(body.textContent?.indexOf('Findings so far') ?? -1)
    expect(body.textContent?.indexOf('Block 1.2.3.4')).toBeLessThan(body.textContent?.indexOf('Isolate host') ?? -1)
    expect(screen.getByText('beacon')).toBeInTheDocument()
    expect(screen.getByText('Reversible')).toBeInTheDocument()
    expect(screen.getByText('Cannot be undone')).toBeInTheDocument()
    // Reversible asks never hold; irreversible ones have no plain Approve.
    expect(screen.getAllByRole('button', { name: 'Approve' })).toHaveLength(1)
    expect(screen.getAllByRole('button', { name: /Press and hold to confirm/ })).toHaveLength(1)
    expect(screen.getByText('Hold to approve')).toBeInTheDocument()
    expect(approvalsApi.needsYou).toHaveBeenCalledWith('case-dec')

    const before = vi.mocked(approvalsApi.needsYou).mock.calls.length
    const changed = vi.fn()
    window.addEventListener(CASES_CHANGED, changed)
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    expect(approvalsApi.approve).toHaveBeenCalledWith('act-1')
    await waitFor(() => expect(vi.mocked(approvalsApi.needsYou).mock.calls.length).toBeGreaterThan(before))
    // The list behind the drawer refreshes too.
    await waitFor(() => expect(changed).toHaveBeenCalledTimes(1))
    window.removeEventListener(CASES_CHANGED, changed)

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
    const submit = screen.getByRole('button', { name: 'Confirm reject' })
    expect(submit).toBeDisabled()
    fireEvent.submit(submit.closest('form') as HTMLFormElement)
    expect(approvalsApi.reject).not.toHaveBeenCalled()
    fireEvent.change(screen.getByRole('textbox', { name: 'Rejection reason' }), {
      target: { value: 'not our host' },
    })
    fireEvent.click(submit)
    expect(approvalsApi.reject).toHaveBeenCalledWith('act-no', 'not our host')
  })

  it('titles the block with how long it has waited and drops empty rows', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-06-15T09:12:00Z'))
    try {
      testState.cases = [{ case_id: 'case-dec', title: 'Decision case', status: 'open', priority: 'high', finding_ids: [], created_at: '2026-06-15T09:14:00Z', combined_state: 'open', investigations: [] }]
      vi.mocked(approvalsApi.needsYou).mockResolvedValue({
        data: { count: 3, items: [need({ reason: '' }), need({ source_id: 'a2', title: 'Two', created_at: '2026-06-15T05:00:00Z' }), need({ source_id: 'a3', title: 'Three', created_at: 'garbage' })] },
      } as never)
      renderCase('case-dec')
      expect(await screen.findByRole('heading', { name: 'Block 1.2.3.4' })).toBeInTheDocument()
      expect(screen.getByText('Needs your decision · waiting 12 min')).toBeInTheDocument()
      expect(screen.getByText('Needs your decision · waiting 4 h')).toBeInTheDocument()
      expect(screen.getByText('Needs your decision')).toBeInTheDocument()
      expect(screen.getAllByText('Why it stopped')).toHaveLength(2)
      expect(document.querySelector('.case-needs')?.textContent).not.toMatch(/left/)
    } finally {
      vi.useRealTimers()
    }
  })

  it('shows the error and renders nothing without items', async () => {
    testState.cases = [{ case_id: 'case-dec', title: 'Decision case', status: 'open', priority: 'high', finding_ids: [], created_at: '2026-06-15T09:14:00Z', combined_state: 'open', investigations: [] }]
    vi.mocked(approvalsApi.needsYou).mockResolvedValue({ data: { count: 0, items: [] } } as never)
    const { unmount } = renderCase('case-dec')
    expect(await screen.findByRole('heading', { name: 'Decision case' })).toBeInTheDocument()
    expect(document.querySelector('.case-needs')).toBeNull()
    unmount()

    vi.mocked(approvalsApi.needsYou).mockResolvedValue({ data: { count: 1, items: [need()] } } as never)
    vi.mocked(approvalsApi.approve).mockRejectedValueOnce({ response: { data: { detail: 'Already decided' } } })
    renderCase('case-dec')
    fireEvent.click(await screen.findByRole('button', { name: 'Approve' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Already decided')
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
    expect(screen.queryByText(/Now · step/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Decide on Summary' })).not.toBeInTheDocument()
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
    expect(screen.queryByRole('button', { name: 'Decide on Summary' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: /^Evidence/ }))
    fireEvent.click(await screen.findByRole('button', { name: 'Decide on Summary' }))
    expect(screen.getByRole('tab', { name: /^Summary/ })).toHaveAttribute('aria-selected', 'true')
    expect(screen.queryByRole('button', { name: 'Decide on Summary' })).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Block 1.2.3.4' })).toBeInTheDocument()
  })

  describe('head row', () => {
    const open = (id: string) => ({
      case_id: id,
      title: 'Frame case',
      status: 'open',
      priority: 'high',
      finding_ids: [],
      created_at: '2026-06-15T09:14:00Z',
      combined_state: 'open',
      investigations: [],
    })

    it('shows the loading state, then the breadcrumb and title', async () => {
      testState.cases = [open('case-9')]
      renderDetail('case-9')
      expect(screen.getByText('Loading case…')).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Case actions' })).not.toBeInTheDocument()
      expect(await screen.findByRole('heading', { name: 'Frame case' })).toBeInTheDocument()
      expect(screen.getByText('Case case-9')).toBeInTheDocument()
      expect(screen.queryByText('All cases')).not.toBeInTheDocument()
    })

    it('shows a failed read with the crumb and no menu', async () => {
      testState.getByIdError = true
      const onBack = vi.fn()
      renderDetail('case-9', { onBack })
      expect(await screen.findByText(/Couldn’t load this case: backend down/)).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Case actions' })).not.toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'Cases' }))
      expect(onBack).toHaveBeenCalled()
    })

    it('writes the populated header: severity, state, reason, workflow name, alerts, resolve-by', async () => {
      const due = new Date(Date.now() + 7 * 3600_000).toISOString()
      vi.mocked(casesApi.getSLA).mockResolvedValueOnce({ data: { resolution_due: due, health_status: 'healthy' } } as never)
      testState.cases = [{
        ...open('case-9'),
        combined_state: 'executing',
        investigations: [investigation('executing', true, 'run-hunt')],
      }]
      testState.runs['run-hunt'] = { hunt: HUNT }
      renderDetail('case-9')
      const head = ((await screen.findByRole('heading', { name: 'Frame case' })).closest('.detail-head')) as HTMLElement
      expect(within(head).getByText('High')).toBeInTheDocument()
      expect(within(head).getByText('Executing')).toHaveClass('state-pill', 'live')
      expect(await within(head).findByText('Incident response')).toBeInTheDocument()
      expect(within(head).getByText('who logged in')).toHaveClass('case-reason')
      expect(within(head).getByText('0 alerts combined')).toBeInTheDocument()
      const left = await within(head).findByText('7 h left')
      expect(left).toHaveClass('case-sla', 'good')
      expect(within(head).getByText(/Not measured yet/)).toBeInTheDocument()
      expect(within(head).getByRole('tab', { name: /^Summary/ })).toHaveAttribute('aria-selected', 'true')
    })

    it('falls back to the workflow id when the catalog does not answer', async () => {
      vi.mocked(workflowApi.listAll).mockRejectedValueOnce(new Error('down'))
      testState.cases = [{ ...open('case-9'), investigations: [investigation('open', false, 'run-x')] }]
      renderDetail('case-9')
      const head = ((await screen.findByRole('heading', { name: 'Frame case' })).closest('.detail-head')) as HTMLElement
      expect(await within(head).findByText('incident-response')).toBeInTheDocument()
    })

    it('writes the empty header for a case with no workflow or SLA', async () => {
      vi.mocked(casesApi.getSLA).mockRejectedValueOnce(new Error('no sla'))
      testState.cases = [open('case-9')]
      renderDetail('case-9')
      const head = ((await screen.findByRole('heading', { name: 'Frame case' })).closest('.detail-head')) as HTMLElement
      expect(within(head).getByText('No workflow')).toBeInTheDocument()
      expect(within(head).getByText(/Resolve by —/)).toBeInTheDocument()
      expect(within(head).getByText('Open')).toHaveClass('idle')
      expect(head.querySelector('.case-reason')).toBeNull()
    })

    it('shows Needs you with the ask on the pill, and the strip off Summary only', async () => {
      testState.cases = [{ ...open('case-9'), combined_state: 'executing' }]
      vi.mocked(approvalsApi.needsYou).mockResolvedValue({ data: { count: 1, items: [need({ case_id: 'case-9' })] } } as never)
      renderDetail('case-9')
      const head = ((await screen.findByRole('heading', { name: 'Frame case' })).closest('.detail-head')) as HTMLElement
      expect(await within(head).findByText('Needs you')).toHaveClass('state-pill', 'needs')
      expect(within(head).getByText('Block 1.2.3.4')).toHaveClass('case-reason')
      expect(document.querySelector('.case-needs-strip')).toBeNull()
      fireEvent.click(screen.getByRole('tab', { name: /^Checked/ }))
      const strip = (await screen.findByRole('button', { name: 'Decide on Summary' })).closest('.case-needs-strip') as HTMLElement
      expect(within(strip).getByText('Block 1.2.3.4')).toBeInTheDocument()
      expect(within(strip).queryByText(/pauses in/)).not.toBeInTheDocument()
      fireEvent.click(within(strip).getByRole('button', { name: 'Decide on Summary' }))
      expect(document.querySelector('.case-needs-strip')).toBeNull()
    })

    it('has Expand and Close only in the drawer', async () => {
      testState.cases = [open('case-9')]
      const onBack = vi.fn()
      const onExpand = vi.fn()
      const { unmount } = renderDetail('case-9', { onBack, onExpand })
      await screen.findByRole('heading', { name: 'Frame case' })
      fireEvent.click(screen.getByRole('button', { name: 'Expand' }))
      fireEvent.click(screen.getByRole('button', { name: 'Close' }))
      expect(onExpand).toHaveBeenCalledTimes(1)
      expect(onBack).toHaveBeenCalledTimes(1)
      unmount()

      renderDetail('case-9')
      await screen.findByRole('heading', { name: 'Frame case' })
      expect(screen.queryByRole('button', { name: 'Expand' })).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Close' })).not.toBeInTheDocument()
    })

    it('opens Edit and Merge from the ⋯ menu; Merge loads its own picker', async () => {
      testState.cases = [open('case-9'), { ...open('case-10'), title: 'Other case' }]
      renderDetail('case-9')
      await screen.findByRole('heading', { name: 'Frame case' })
      const menu = screen.getByRole('button', { name: 'Case actions' })
      expect(menu).toHaveAttribute('aria-haspopup', 'menu')

      fireEvent.click(menu)
      fireEvent.click(screen.getByRole('menuitem', { name: 'Edit' }))
      expect(screen.getByRole('dialog', { name: 'Edit case' })).toBeInTheDocument()
      expect(screen.queryByRole('menu')).not.toBeInTheDocument()
      fireEvent.click(within(screen.getByRole('dialog', { name: 'Edit case' })).getByRole('button', { name: 'Cancel' }))

      vi.mocked(casesApi.getAll).mockClear()
      fireEvent.click(screen.getByRole('button', { name: 'Case actions' }))
      fireEvent.click(screen.getByRole('menuitem', { name: 'Merge' }))
      const dialog = screen.getByRole('dialog', { name: 'Merge case' })
      expect(within(dialog).getByRole('button', { name: 'Merge case' })).toBeDisabled()
      await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Merge case' })).toBeEnabled())
      expect(casesApi.getAll).toHaveBeenCalledTimes(1)
    })

    it('shows a failed merge picker with Retry', async () => {
      testState.cases = [open('case-9')]
      renderDetail('case-9')
      await screen.findByRole('heading', { name: 'Frame case' })
      vi.mocked(casesApi.getAll).mockRejectedValueOnce(new Error('list down'))
      fireEvent.click(screen.getByRole('button', { name: 'Case actions' }))
      fireEvent.click(screen.getByRole('menuitem', { name: 'Merge' }))
      expect(await screen.findByText(/Couldn’t load cases: list down/)).toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
      await waitFor(() => expect(screen.queryByText(/Couldn’t load cases/)).not.toBeInTheDocument())
    })

    it('gates Delete on cases.delete and confirms before deleting', async () => {
      testState.cases = [open('case-9')]
      testState.canDelete = false
      const { unmount } = renderDetail('case-9')
      await screen.findByRole('heading', { name: 'Frame case' })
      fireEvent.click(screen.getByRole('button', { name: 'Case actions' }))
      expect(screen.getByRole('menuitem', { name: 'Edit' })).toBeInTheDocument()
      expect(screen.queryByRole('menuitem', { name: /Delete/ })).not.toBeInTheDocument()
      unmount()

      testState.canDelete = true
      const onBack = vi.fn()
      renderDetail('case-9', { onBack })
      await screen.findByRole('heading', { name: 'Frame case' })
      fireEvent.click(screen.getByRole('button', { name: 'Case actions' }))
      fireEvent.click(screen.getByRole('menuitem', { name: /Delete/ }))
      expect(screen.getByRole('dialog', { name: 'Delete case?' })).toBeInTheDocument()
      expect(casesApi.delete).not.toHaveBeenCalled()
      vi.mocked(casesApi.delete).mockResolvedValueOnce({ data: { success: true } } as never)
      fireEvent.click(within(screen.getByRole('dialog', { name: 'Delete case?' })).getByRole('button', { name: 'Delete case' }))
      await waitFor(() => expect(casesApi.delete).toHaveBeenCalledWith('case-9'))
      await waitFor(() => expect(onBack).toHaveBeenCalled())
    })

    it('closes the menu on outside click and on Escape', async () => {
      testState.cases = [open('case-9')]
      renderDetail('case-9')
      await screen.findByRole('heading', { name: 'Frame case' })
      fireEvent.click(screen.getByRole('button', { name: 'Case actions' }))
      expect(screen.getByRole('menu')).toBeInTheDocument()
      // the drawer stops mousedown from bubbling; the menu must still close
      const stop = (e: Event) => e.stopPropagation()
      document.body.addEventListener('mousedown', stop)
      fireEvent.mouseDown(document.body)
      document.body.removeEventListener('mousedown', stop)
      expect(screen.queryByRole('menu')).not.toBeInTheDocument()

      fireEvent.click(screen.getByRole('button', { name: 'Case actions' }))
      fireEvent.keyDown(document, { key: 'Escape' })
      expect(screen.queryByRole('menu')).not.toBeInTheDocument()
    })
  })
})

describe('Memory and blind spots tab', () => {
  const prov = (kind: string, id: string) => ({ investigation_kind: kind, investigation_id: id, concluded_at: '2026-05-02T10:00:00Z' })
  const recall = {
    keys: ['10.0.0.7', 'm.kaur'],
    sightings: [{ ...prov('hunt', 'hunt-9'), entity_key: '10.0.0.7', source_system: 'splunk', hit_count: 4 }],
    verdicts: [{ ...prov('case', 'case-4302'), hypothesis_id: 'h9', statement: 'Loader family, finance laptops', outcome: 'proven' }],
    gaps: [{ ...prov('hunt', 'hunt-3'), hypothesis_id: 'h3', statement: 'MFA push logs never checked', disposition: 'no_evidence_gathered' }],
  }

  function open(id: string, run: unknown) {
    testState.cases = [{
      case_id: id,
      title: 'Memory case',
      status: 'open',
      priority: 'high',
      finding_ids: [],
      created_at: '2026-06-15T09:14:00Z',
      combined_state: 'open',
      investigations: [investigation('executing', true, `run-${id}`)],
    }]
    testState.runs[`run-${id}`] = run
    renderCase(id)
  }

  const memoryTab = () => screen.findByRole('tab', { name: /Memory/ })

  it('shows recalled rows with provenance, merged blind spots, and counts the rows', async () => {
    open('m-hunt', { hunt: { ...HUNT, recall, calls: [...HUNT.calls, { question: 'what is 10.0.0.7', tool: 'recall_entity', result_length: 340, cost_usd: 0 }], evidence: [{ ...HUNT.evidence[0], is_gap: true, gap_detail: 'no proxy logs' }] } })
    fireEvent.click(await memoryTab())
    // sighting + verdict + recall_entity call + declared gap + visibility gap
    expect(screen.getByRole('tab', { name: /Memory/ })).toHaveTextContent('5')

    const recalled = within(await screen.findByRole('region', { name: 'Recalled' }))
    expect(recalled.getByText('Asked about 10.0.0.7, m.kaur')).toBeInTheDocument()
    expect(recalled.getByText('10.0.0.7 · splunk · 4 hits')).toBeInTheDocument()
    expect(recalled.getByText('Hunt hunt-9 · May 2, 2026')).toBeInTheDocument()
    expect(recalled.getByText('proven — Loader family, finance laptops')).toBeInTheDocument()
    expect(recalled.getByText('Case case-4302 · May 2, 2026')).toBeInTheDocument()
    expect(recalled.getByText('recall_entity · what is 10.0.0.7')).toBeInTheDocument()
    expect(recalled.getByText('340 bytes')).toBeInTheDocument()
    // declared gaps live in Blind spots, not here
    expect(recalled.queryByText(/MFA push logs/)).toBeNull()

    const blind = within(screen.getByRole('region', { name: 'Blind spots' }))
    expect(blind.getByText('Declared')).toBeInTheDocument()
    expect(blind.getByText('no evidence gathered — MFA push logs never checked')).toBeInTheDocument()
    expect(blind.getByText('Visibility')).toBeInTheDocument()
    expect(blind.getByText('no proxy logs')).toBeInTheDocument()
  })

  it('reads a lead run: recall and visibility gaps', async () => {
    open('m-lead', {
      projection: {
        iterations: 1, decisions: [], findings: [], calls: [], recall,
        gaps: [{ dispatch_id: 'dsp-1', agent_id: 'worker', failure_reason: 'tool down', query_intent: 'list users' }],
      },
    })
    fireEvent.click(await memoryTab())
    expect(await screen.findByText('list users — tool down')).toBeInTheDocument()
    expect(screen.getByText('10.0.0.7 · splunk · 4 hits')).toBeInTheDocument()
  })

  it('keeps Withdraw and Record a blind spot disabled as Later', async () => {
    open('m-later', { hunt: { ...HUNT, recall } })
    fireEvent.click(await memoryTab())
    const withdraw = await screen.findByRole('button', { name: 'Withdraw' })
    const record = screen.getByRole('button', { name: 'Record a blind spot' })
    for (const button of [withdraw, record]) {
      expect(button).toBeDisabled()
      expect(button).toHaveAttribute('title', 'Coming in a later release')
    }
    expect(within(screen.getByRole('region', { name: 'Recalled' })).getAllByRole('button', { name: 'Withdraw' })).toHaveLength(1)
  })

  it('tells an unavailable recall from an empty one and from none journaled', async () => {
    open('m-un', { hunt: { ...HUNT, recall: { unavailable: 'memory store down', keys: ['m.kaur'] } } })
    fireEvent.click(await memoryTab())
    expect(await screen.findByText('Recall did not happen: memory store down (m.kaur)')).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /Memory/ })).toHaveTextContent('0')
  })

  it('says so when the recall found no entities', async () => {
    open('m-none', { hunt: { ...HUNT, recall: { keys: [], sightings: [], verdicts: [], gaps: [] } } })
    fireEvent.click(await memoryTab())
    expect(await screen.findByText('No entities recalled.')).toBeInTheDocument()
    expect(screen.getByText('None recorded.')).toBeInTheDocument()
  })

  it('says when the run journaled no opening recall but asked mid-run', async () => {
    open('m-mid', { hunt: { ...HUNT, calls: [{ question: 'who is m.kaur', tool: 'recall_entity', result_length: 9, cost_usd: 0 }] } })
    fireEvent.click(await memoryTab())
    expect(await screen.findByText('The run did not journal an opening recall.')).toBeInTheDocument()
    expect(screen.getByText('recall_entity · who is m.kaur')).toBeInTheDocument()
  })

  it('is empty with no recall and no gaps, and keeps the tab', async () => {
    open('m-empty', { hunt: { ...HUNT, evidence: [] } })
    fireEvent.click(await memoryTab())
    expect(await screen.findByText('No memory recorded')).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /Memory/ })).toHaveTextContent('0')
  })

  it('shows loading, then an error when the run cannot be read', async () => {
    vi.mocked(workflowApi.getRun).mockImplementationOnce(() => new Promise(() => {}))
    open('m-load', { hunt: HUNT })
    fireEvent.click(await memoryTab())
    expect(await screen.findByText('Loading the run…')).toBeInTheDocument()
  })

  it('reports an unreadable run', async () => {
    vi.mocked(workflowApi.getRun).mockRejectedValueOnce(new Error('boom'))
    open('m-err', { hunt: HUNT })
    fireEvent.click(await memoryTab())
    expect(await screen.findByText('The run could not be read')).toBeInTheDocument()
  })
})

describe('following a live run', () => {
  const live = (over: Record<string, unknown> = {}) => ({ status: 'running', hunt: { ...HUNT, status: 'running' }, ...over })
  const open = (id: string, run: unknown, state = 'executing') => {
    testState.cases = [{ ...openCase(id), combined_state: state, investigations: [investigation(state, true, `run-${id}`)] }]
    testState.runs[`run-${id}`] = run
    renderCase(id)
  }
  const tick = () => act(async () => { await vi.advanceTimersByTimeAsync(5_000) })

  beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }))
  afterEach(() => vi.useRealTimers())

  it('re-reads the run, case and record while live, and stops once it parks', async () => {
    open('case-live', live())
    expect(await screen.findByText('Now · step 3')).toBeInTheDocument()
    const reads = () => vi.mocked(workflowApi.getRun).mock.calls.length
    const first = reads()

    testState.runs['run-case-live'] = live({ hunt: { ...HUNT, status: 'running', iteration: 4 } })
    await tick() // the run re-read
    expect(await screen.findByText('Now · step 4')).toBeInTheDocument()
    await tick() // the case and record follow the run's change
    expect(vi.mocked(casesApi.getById).mock.calls.length).toBeGreaterThan(1)
    expect(vi.mocked(casesApi.getRecord).mock.calls.length).toBeGreaterThan(1)

    // The budget refuses the next iteration: the pill and the line replace the live card.
    testState.runs['run-case-live'] = live({ hunt: { ...HUNT, status: 'parked', reason: 'the budget refused another iteration: unpriced | extra' } })
    await tick()
    expect(await screen.findByRole('region', { name: 'Run state' })).toHaveTextContent('Paused')
    expect(document.querySelector('.state-pill')).toHaveTextContent('Paused')
    expect(screen.getByText('The budget refused another iteration: unpriced.')).toBeInTheDocument()
    expect(screen.queryByText(/Now · step/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'What the run reported' })).toBeInTheDocument()
    expect(within(screen.getByRole('table', { name: 'Agents' })).queryByText('executing')).not.toBeInTheDocument()

    // A paused run can wake, so it keeps being read; a failed one does not.
    const before = reads()
    expect(before).toBeGreaterThan(first)
    testState.runs['run-case-live'] = live({ status: 'failed', error: 'the lead emitted no decision: Connection error. | x | y', hunt: { ...HUNT, status: 'running' } })
    await tick()
    await waitFor(() => expect(document.querySelector('.state-pill')).toHaveTextContent('Stopped'))
    expect(screen.getByText('The lead emitted no decision: Connection error.')).toBeInTheDocument()
    const ended = reads()
    await tick()
    await tick()
    expect(reads()).toBe(ended)
  })

  it('lets Needs you win over a stopped run', async () => {
    vi.mocked(approvalsApi.needsYou).mockResolvedValue({ data: { count: 1, items: [need({ case_id: 'case-need' })] } } as never)
    open('case-need', live({ status: 'failed', error: 'boom' }), 'waiting_approval')
    expect((await screen.findAllByText('Needs you'))[0]).toBeInTheDocument()
    expect(document.querySelector('.state-pill')).toHaveTextContent('Needs you')
    expect(screen.queryByRole('region', { name: 'Run state' })).not.toBeInTheDocument()
  })

  it('says handed off in Findings so far', async () => {
    open('case-words', live({ hunt: { ...HUNT, hypotheses: [{ ...HUNT.hypotheses[1], status: 'handed_off' }] } }))
    expect(await screen.findByText('handed off — Still forming')).toBeInTheDocument()
  })
})
