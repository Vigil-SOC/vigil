import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import CasesScreen, { CasesDetail } from './CasesScreen'
import { CASES_CHANGED } from './useCases'
import { ToastProvider } from '../../shell/toast'
import { approvalsApi, casesApi, orchestratorApi, streamFetch, workflowApi, type NeedsYouItem } from '../../services/api'
import investigateProjection from '../workflows/__fixtures__/kernel-investigate-projection.json'
import heldProjection from '../workflows/__fixtures__/kernel-investigate-held-projection.json'

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
    addComment: vi.fn(),
    getTasks: vi.fn(() => Promise.resolve({ data: { tasks: [] } })),
    getEvidence: vi.fn(() => Promise.resolve({ data: { evidence: [] } })),
    getIOCs: vi.fn(() => Promise.resolve({ data: { iocs: [] } })),
    getEscalations: vi.fn(() => Promise.resolve({ data: { escalations: [] } })),
  },
  workflowApi: {
    listAll: vi.fn(() => Promise.resolve({ data: { workflows: [{ id: 'incident-response', name: 'Incident response' }] } })),
    getRun: vi.fn((id: string) => Promise.resolve({ data: { run_id: id, ...(testState.runs[id] as object) } })),
    verifyRun: vi.fn(),
    getCall: vi.fn(),
    steer: vi.fn(() => Promise.resolve({ data: {} })),
  },
  orchestratorApi: { exportInvestigation: vi.fn() },
  findingsApi: { getById: vi.fn() },
  caseSearchApi: { search: vi.fn() },
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
    { evidence_id: 'e1', iteration: 2, source_system: 'elastic', summary: 'no login', is_gap: false, gap_detail: null, source_tier: 'telemetry', bears_on: [{ hypothesis_id: 'h1', relation: 'weakens' }] },
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
    primary: true,
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
function renderDetail(id: string, props: { onBack?: () => void; onExpand?: () => void; seed?: string | null; onSeedConsumed?: () => void } = {}) {
  const ui = (caseId: string) => (
    <MemoryRouter>
      <ToastProvider>
        <CasesDetail id={caseId} onBack={props.onBack ?? vi.fn()} onExpand={props.onExpand} pageKey="cases" seed={props.seed} onSeedConsumed={props.onSeedConsumed} />
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
    expect(screen.getByText('Combined from 2 alerts')).toBeInTheDocument()
    const now = await screen.findByRole('region', { name: 'Now' })
    expect(await within(now).findByText('Now · step 3')).toBeInTheDocument()
    expect(within(now).getByText('who logged in')).toBeInTheDocument()
    const clock = '09:14' // UTC, whatever the machine's zone
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
    const card = (await screen.findByText(/What Vigil checked/)).closest('section') as HTMLElement
    expect(card).toHaveTextContent('What Vigil checked · 2 queries · $0.20')
    expect(card).not.toHaveTextContent('no source')
    // the run's cost in the header; each turn shows its words and its own cost once
    const turn = within(card).getByText('who logged in').closest('section') as HTMLElement
    expect(turn).toHaveTextContent('Turn 3')
    expect(turn).toHaveTextContent('$0.01')
    const row = turn.querySelector('li') as HTMLElement
    expect(row).toHaveTextContent('search')
    expect(row).toHaveTextContent('12 chars')
    // a latency the ledger never recorded is a dash, never a zero
    expect(row).toHaveTextContent('—')
    expect(row).not.toHaveTextContent('0.0 s')
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
          hyp('f', 'inconclusive', 0, 1, ''),
          hyp('g', 'parked', 0, 0, '', 'waiting on logs'),
          hyp('h', 'handed_off', 0, 0, '', 'sent to the reviewer'),
        ],
      },
    }
    renderCase('case-words')
    fireEvent.click(await screen.findByRole('tab', { name: /Explanations/ }))
    expect(await screen.findByText('claim a')).toBeInTheDocument()
    // the design's five words in the board's tones; a ledger status never shows as a word
    const pill = (id: string) => screen.getByText(`claim ${id}`).closest('li')!.querySelector('.case-expl-pill')!
    const words: [string, string, string][] = [
      ['a', 'Proven', 'good'], ['b', 'Standing', 'good'], ['c', 'Forming', 'ac'], ['d', 'Weakened', 'fair'],
      ['e', 'Ruled out', 'muted'], ['f', 'Weakened', 'fair'], ['g', 'Ruled out', 'muted'], ['h', 'Proven', 'good'],
    ]
    for (const [id, word, tone] of words) {
      expect(pill(id)).toHaveTextContent(word)
      expect(pill(id)).toHaveClass(tone)
    }
    expect(screen.queryByText(/^(Inconclusive|Parked)$/)).not.toBeInTheDocument()
    // a hand-off is what happened next, beside its word
    expect(screen.getByText('claim h').closest('li')).toHaveTextContent('Handed off')
    expect(screen.getByText('claim e')).toHaveClass('struck')
    expect(screen.getByText('claim g')).toHaveClass('struck')
    expect(screen.getByText('claim a')).not.toHaveClass('struck')
    for (const by of ['the hunt definition', 'you', 'the base rate', 'the deployment-gap check', 'mystery_token']) {
      expect(screen.getByText(`Added by ${by}`)).toBeInTheDocument()
    }
    expect(screen.getAllByText(/^Added by/)).toHaveLength(5)
    expect(screen.getByText('confirmed by the hash')).toBeInTheDocument()
    expect(screen.getByText('waiting on logs')).toBeInTheDocument()
    expect(screen.getByText('sent to the reviewer')).toBeInTheDocument()
  })

  // #2459: an abandoned explanation's note cited raw ev- ids.
  it('names the rows an explanation\'s note cites in words, and as chips that open them once the row is opened', async () => {
    huntCase('case-note')
    testState.runs['run-case-note'] = {
      hunt: {
        ...HUNT,
        hypotheses: [
          { hypothesis_id: 'hb', statement: 'The activity has a legitimate explanation', status: 'parked', supports: 0, weakens: 2, resolution_reason: 'abandoned: not supported by sensor-attested records. [ev-b6809427f57f, ev-6e0731e4c66b]', provenance: 'base_rate' },
        ],
        evidence: [
          { evidence_id: 'ev-b6809427f57f', iteration: 4, source_system: 'endpoint', summary: 'lsass dump', is_gap: false, gap_detail: null, source_tier: 'telemetry', origins: ['crowdstrike'], bears_on: [{ hypothesis_id: 'hb', relation: 'weakens' }] },
        ],
        evidence_count: 2,
      },
    }
    renderCase('case-note')
    fireEvent.click(await screen.findByRole('tab', { name: /Explanations/ }))
    const note = await screen.findByText(/abandoned: not supported by sensor-attested records\./)
    expect(note).toHaveTextContent('abandoned: not supported by sensor-attested records. · 2 evidence rows')
    expect(note.textContent).not.toMatch(/ev-/)

    fireEvent.click(note)
    const chip = within(note).getByRole('button', { name: 'step 4 · CrowdStrike' })
    expect(note).toHaveTextContent('and 1 older row')
    fireEvent.click(chip)
    expect(await screen.findByRole('tab', { name: /^Evidence/, selected: true })).toBeInTheDocument()
    await waitFor(() => expect(document.querySelector('[data-evidence-id="ev-b6809427f57f"]')).toHaveClass('cite-target'))
  })

  it('keeps reading the hunt run once a newer root-cause run is on the case, and links to it', async () => {
    huntCase('case-handoff')
    const run = (workflow_id: string, run_id: string) => ({ ...investigation('completed', false, run_id), workflow_id })
    testState.cases[0].investigations = [{ ...run('root-cause-analysis', 'run-rca'), primary: false }, run('threat-hunt', 'run-hunt')]
    testState.runs['run-hunt'] = {
      hunt: { ...HUNT, hypotheses: [{ hypothesis_id: 'h', statement: 'claim h', status: 'handed_off', supports: 2, weakens: 0, resolution_reason: null, provenance: '' }] },
    }
    renderCase('case-handoff')
    fireEvent.click(await screen.findByRole('tab', { name: /Explanations/ }))
    expect(await screen.findByText('claim h')).toBeInTheDocument()
    // the hunt is the case's run and comes first on the run filter
    const filter = screen.getByRole('group', { name: 'Run' })
    expect(within(filter).getAllByRole('button').map((b) => b.textContent)).toEqual(['All runs', 'threat-hunt', 'root-cause-analysis'])
    expect(screen.getByRole('link', { name: 'Open run run-rca' })).toHaveAttribute('href', '/workflows?run=run-rca')
    const row = screen.getByText('claim h').closest('li')!
    expect(row.querySelector('.case-expl-pill')).toHaveTextContent('Proven')
    expect(row).toHaveTextContent('Handed off · Root-cause run')
    expect(screen.queryByText(/handed_off/)).not.toBeInTheDocument()
  })

  it('shows the Handed off pill alone while no root-cause run exists', async () => {
    huntCase('case-no-rca')
    testState.cases[0].investigations = [{ ...investigation('executing', true, 'run-hunt'), workflow_id: 'threat-hunt' }]
    testState.runs['run-hunt'] = {
      hunt: { ...HUNT, hypotheses: [{ hypothesis_id: 'h', statement: 'claim h', status: 'handed_off', supports: 2, weakens: 0, resolution_reason: null, provenance: '' }] },
    }
    renderCase('case-no-rca')
    fireEvent.click(await screen.findByRole('tab', { name: /Explanations/ }))
    expect(await screen.findByText('claim h')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Open run/ })).not.toBeInTheDocument()
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

  it('lays a hunt evidence trail out: stance, tier, by, a disabled row action, header counts', async () => {
    testState.cases = [{
      case_id: 'case-trail',
      title: 'Trail case',
      status: 'open',
      priority: 'high',
      finding_ids: [],
      created_at: '2026-06-15T09:14:00Z',
      combined_state: 'executing',
      investigations: [investigation('executing', true, 'run-trail')],
    }]
    const base = { gap_detail: null, is_gap: false, source_tier: 'feed' }
    testState.runs['run-trail'] = {
      hunt: {
        ...HUNT,
        evidence_count: 7,
        evidence: [
          { ...HUNT.evidence[0] },
          // Supports one explanation and weakens another: its stance on the hunt's own explanation, each link named beneath.
          { ...base, evidence_id: 'e2', iteration: 3, source_system: 'okta', summary: 'new session', bears_on: [{ hypothesis_id: 'h2', relation: 'supports' }, { hypothesis_id: 'h1', relation: 'weakens' }] },
          { ...base, evidence_id: 'e3', iteration: 1, source_system: 'dns', summary: 'plain lookup', source_tier: 'not_evidence', bears_on: [] },
          // Gap row with no stamped tier, and an iteration the capped moves do not cover.
          { ...base, evidence_id: 'e4', iteration: 9, source_system: 'mfa', summary: 'no push logs', is_gap: true, gap_detail: 'no source', source_tier: undefined, bears_on: [{ hypothesis_id: 'h2', relation: 'supports' }] },
        ],
      },
    }
    renderCase('case-trail')

    fireEvent.click(await screen.findByRole('tab', { name: /^Evidence/ }))
    expect(await screen.findByRole('heading', { name: 'Evidence trail · 7 rows · showing 4 of 7 · 0 support · 2 go against · 5 neither' })).toBeInTheDocument()

    const row = (id: string) => document.querySelector(`[data-evidence-id="${id}"]`) as HTMLElement
    const first = row('e1')
    // Read against the hunt's own explanation (The host is owned), not whichever ranks first.
    expect(within(first).getByText('Goes against')).toBeInTheDocument()
    expect(within(first).getByText('Telemetry')).toBeInTheDocument()
    expect(within(first).getByText('threat_hunter')).toBeInTheDocument()
    expect(within(first).getByText('The host is owned')).toBeInTheDocument()
    const second = row('e2')
    expect(within(second).queryByText('Mixed')).not.toBeInTheDocument()
    expect(within(second).getByText('Feed')).toBeInTheDocument()
    expect(within(second).getByText('threat_hunter')).toBeInTheDocument()
    expect(within(second).getByText('Supports Still forming · Goes against The host is owned')).toBeInTheDocument()
    expect(within(row('e3')).getByText('Neither')).toBeInTheDocument()
    expect(within(row('e3')).getByText('Not counted')).toBeInTheDocument()
    expect(within(row('e3')).getByText('network_analyst')).toBeInTheDocument()
    const gap = row('e4')
    expect(within(gap).getByText('no push logs (gap)')).toBeInTheDocument()
    expect(within(gap).getAllByText('—')).toHaveLength(2) // no tier, move not in the capped list

    const act = within(first).getByRole('button', { name: 'Actions for this row' })
    expect(act).toBeDisabled()
    expect(act).toHaveAttribute('title', 'Coming in a later release')
  })

  // #2287: the system the rows came from, not the hunt's domain word; the domain only when no system is known.
  it('names the system each evidence row came from, with its tier beside it', async () => {
    testState.cases = [{
      case_id: 'case-origin',
      title: 'Origin case',
      status: 'open',
      priority: 'high',
      finding_ids: [],
      created_at: '2026-06-15T09:14:00Z',
      combined_state: 'executing',
      investigations: [investigation('executing', true, 'run-origin')],
    }]
    const base = { gap_detail: null, is_gap: false, bears_on: [] }
    testState.runs['run-origin'] = {
      hunt: {
        ...HUNT,
        evidence_count: 3,
        evidence: [
          { ...base, evidence_id: 'o1', iteration: 2, source_system: 'process_lineage', summary: 'rundll32 loaded msupdate64.dll', source_tier: 'telemetry', origins: ['crowdstrike'] },
          { ...base, evidence_id: 'o2', iteration: 3, source_system: 'proxy', summary: 'beacon and email', source_tier: 'telemetry', origins: ['zscaler', 'proofpoint'] },
          { ...base, evidence_id: 'o3', iteration: 4, source_system: 'dns', summary: 'one lookup', source_tier: 'telemetry' },
        ],
      },
    }
    renderCase('case-origin')

    fireEvent.click(await screen.findByRole('tab', { name: /^Evidence/ }))
    const row = (id: string) => document.querySelector(`[data-evidence-id="${id}"]`) as HTMLElement
    await waitFor(() => expect(row('o1')).not.toBeNull())
    expect(within(row('o1')).getByText('CrowdStrike')).toBeInTheDocument()
    expect(within(row('o1')).getByText('Telemetry')).toBeInTheDocument()
    expect(within(row('o1')).queryByText('process_lineage')).not.toBeInTheDocument()
    expect(within(row('o2')).getByText('Zscaler, Proofpoint')).toBeInTheDocument()
    expect(within(row('o3')).getByText('dns')).toBeInTheDocument()
  })

  // #2458: rows marked Neither name where they came from too, and say once that they bear on nothing.
  it('names every Neither row\'s source and says once that it bears on neither explanation', async () => {
    testState.cases = [{
      case_id: 'case-neither',
      title: 'Neither case',
      status: 'open',
      priority: 'high',
      finding_ids: [],
      created_at: '2026-06-15T09:14:00Z',
      combined_state: 'executing',
      investigations: [investigation('executing', true, 'run-neither')],
    }]
    const neither = [{ hypothesis_id: 'h1', relation: 'neither' }, { hypothesis_id: 'h2', relation: 'neither' }]
    const base = { gap_detail: null, is_gap: false, source_tier: 'telemetry', bears_on: neither }
    testState.runs['run-neither'] = {
      hunt: {
        ...HUNT,
        evidence_count: 6,
        evidence: [
          { ...base, evidence_id: 'n1', iteration: 15, source_system: 'dns', summary: 'findings-index lookup', origins: ['vigil_alerts'] },
          { ...base, evidence_id: 'n2', iteration: 10, source_system: 'endpoint', summary: 'recall and search', origins: ['vigil'] },
          { ...base, evidence_id: 'n3', iteration: 6, source_system: 'critic', summary: 'strongest benign explanation', source_tier: 'not_evidence', bears_on: [] },
          { ...base, evidence_id: 'n4', iteration: 0, source_system: 'deployment', summary: 'No SIEM is connected', is_gap: true, source_tier: undefined, bears_on: [neither[0]] },
          { ...base, evidence_id: 'n5', iteration: 10, source_system: 'proxy', summary: 'post-11-Oct check', is_gap: true, source_tier: undefined },
          { ...base, evidence_id: 'n6', iteration: 4, source_system: 'okta', summary: 'new session', bears_on: [{ hypothesis_id: 'h1', relation: 'supports' }, { hypothesis_id: 'h2', relation: 'neither' }] },
        ],
      },
    }
    renderCase('case-neither')

    fireEvent.click(await screen.findByRole('tab', { name: /^Evidence/ }))
    const row = (id: string) => document.querySelector(`[data-evidence-id="${id}"]`) as HTMLElement
    await waitFor(() => expect(row('n1')).not.toBeNull())
    const source = (id: string) => row(id).querySelector('.ev-src .ev-line')?.textContent
    expect(source('n1')).toBe('Vigil alerts')
    expect(source('n2')).toBe('Vigil')
    expect(source('n3')).toBe('Reviewer')
    expect(source('n4')).toBe('Not connected')
    expect(source('n5')).toBe('Not answered')
    for (const id of ['n1', 'n2', 'n4', 'n5']) {
      expect(within(row(id)).getByText('Bears on neither explanation')).toBeInTheDocument()
      expect(within(row(id)).queryByText(/The host is owned|Still forming/)).not.toBeInTheDocument()
    }
    // A link either way is named; the explanation it bears on neither way is not.
    expect(within(row('n6')).getByText('Supports The host is owned')).toBeInTheDocument()
  })

  it('totals the Evidence header over every row, against the hunt\'s own explanation, and pages older rows in', async () => {
    testState.cases = [{ ...openCase('case-157'), combined_state: 'executing', investigations: [investigation('executing', true, 'run-157')] }]
    const newest = Array.from({ length: 50 }, (_, i) => ({
      evidence_id: `n${i}`, iteration: 9, source_system: 'elastic', summary: `row ${i}`, is_gap: false, gap_detail: null, source_tier: 'telemetry', bears_on: [],
    }))
    // 157 rows; the newest 50 are all neither, but the hypothesis has 95 supports and 3 weakens across the run.
    const hunt157 = {
        ...HUNT,
        evidence_count: 157,
        evidence: newest,
        hypotheses: [
          { hypothesis_id: 'h1', statement: 'The host is owned', status: 'inconclusive', supports: 95, weakens: 3, neither: 59, resolution_reason: null, provenance: 'operator' },
          { hypothesis_id: 'hb', statement: 'The activity has a legitimate explanation', status: 'active', supports: 0, weakens: 24, neither: 133, resolution_reason: null, provenance: 'base_rate' },
        ],
    }
    testState.runs['run-157'] = { hunt: hunt157 }
    renderCase('case-157')

    fireEvent.click(await screen.findByRole('tab', { name: /^Evidence/ }))
    expect(await screen.findByRole('heading', { name: 'Evidence trail · 157 rows · showing 50 of 157 · 95 support · 3 go against · 59 neither' })).toBeInTheDocument()

    testState.runs['run-157'] = {
      hunt: { ...hunt157, evidence: [...newest, ...newest.map((row, i) => ({ ...row, evidence_id: `o${i}` }))] },
    }
    fireEvent.click(screen.getByRole('button', { name: 'Show older' }))
    await waitFor(() => expect(workflowApi.getRun).toHaveBeenCalledWith('run-157', 100))
    expect(await screen.findByRole('heading', { name: 'Evidence trail · 157 rows · showing 100 of 157 · 95 support · 3 go against · 59 neither' })).toBeInTheDocument()
  })

  it('offers Show older again when the larger read fails, and reads again on the next press', async () => {
    testState.cases = [{ ...openCase('case-older-fail'), combined_state: 'executing', investigations: [investigation('executing', true, 'run-one')] }]
    const rows = Array.from({ length: 50 }, (_, i) => ({
      evidence_id: `n${i}`, iteration: 9, source_system: 'elastic', summary: `row ${i}`, is_gap: false, gap_detail: null, source_tier: 'telemetry', bears_on: [],
    }))
    testState.runs['run-one'] = { hunt: { ...HUNT, evidence_count: 80, evidence: rows } }
    renderCase('case-older-fail')
    fireEvent.click(await screen.findByRole('tab', { name: /^Evidence/ }))
    await screen.findByText('row 0')
    const ok = (runId: string) => Promise.resolve({ data: { run_id: runId, ...(testState.runs[runId] as object) } } as never)
    vi.mocked(workflowApi.getRun).mockImplementation((runId: string, limit?: number) => (limit === 100 ? Promise.reject(new Error('down')) : ok(runId)))
    try {
      vi.mocked(workflowApi.getRun).mockClear()
      fireEvent.click(screen.getByRole('button', { name: 'Show older' }))
      await waitFor(() => expect(workflowApi.getRun).toHaveBeenCalledWith('run-one', 100))
      // the read failed and the run keeps what it showed, so the button comes back rather than spinning
      const button = await screen.findByRole('button', { name: 'Show older' })
      expect(button).toBeEnabled()
      expect(screen.getByText('row 49')).toBeInTheDocument()
      // the same ask again reads again, though its limit is already set
      vi.mocked(workflowApi.getRun).mockClear()
      fireEvent.click(button)
      await waitFor(() => expect(workflowApi.getRun).toHaveBeenCalledWith('run-one', 100))
      expect(await screen.findByRole('button', { name: 'Show older' })).toBeEnabled()
    } finally {
      vi.mocked(workflowApi.getRun).mockImplementation(ok)
    }
  })

  it('shows loading, then a read error, instead of an empty evidence trail', async () => {
    testState.cases = [{
      case_id: 'case-err',
      title: 'Error case',
      status: 'open',
      priority: 'high',
      finding_ids: [],
      created_at: '2026-06-15T09:14:00Z',
      combined_state: 'executing',
      investigations: [investigation('executing', true, 'run-err')],
    }]
    vi.mocked(workflowApi.getRun).mockRejectedValueOnce(new Error('boom'))
    renderCase('case-err')

    fireEvent.click(await screen.findByRole('tab', { name: /^Evidence/ }))
    expect(await screen.findByText('The run could not be read.')).toBeInTheDocument()
    expect(screen.queryByText('No evidence yet')).not.toBeInTheDocument()
  })

  it('says the run is loading while the fold is unread', async () => {
    testState.cases = [{
      case_id: 'case-wait',
      title: 'Waiting case',
      status: 'open',
      priority: 'high',
      finding_ids: [],
      created_at: '2026-06-15T09:14:00Z',
      combined_state: 'executing',
      investigations: [investigation('executing', true, 'run-wait')],
    }]
    vi.mocked(workflowApi.getRun).mockReturnValueOnce(new Promise(() => undefined) as never)
    renderCase('case-wait')

    fireEvent.click(await screen.findByRole('tab', { name: /^Evidence/ }))
    expect(screen.getByText('Loading the run…')).toBeInTheDocument()
    expect(screen.queryByText('No evidence yet')).not.toBeInTheDocument()
  })

  it('says a hunt with no evidence rows has none yet', async () => {
    testState.cases = [{
      case_id: 'case-none',
      title: 'Bare case',
      status: 'open',
      priority: 'high',
      finding_ids: [],
      created_at: '2026-06-15T09:14:00Z',
      combined_state: 'executing',
      investigations: [investigation('executing', true, 'run-none')],
    }]
    testState.runs['run-none'] = { hunt: { ...HUNT, evidence: [], evidence_count: 0 } }
    renderCase('case-none')

    fireEvent.click(await screen.findByRole('tab', { name: /^Evidence/ }))
    expect(await screen.findByText('No evidence yet')).toBeInTheDocument()
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
    expect(await within(now).findByText('Now · step 2')).toBeInTheDocument()
    expect(within(now).getByText('Examine')).toBeInTheDocument()
    expect(within(now).getByText('lead · since —')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: /Explanations/ }))
    expect(await screen.findByText('This workflow does not test explanations yet.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'About explanations' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: /^Evidence/ }))
    const finding = (await screen.findByText('benign traffic')).closest('[data-evidence-id]') as HTMLElement
    expect(screen.getByRole('heading', { name: 'Evidence trail · 1 row' })).toBeInTheDocument()
    expect(within(finding).getAllByText('—')).toHaveLength(2) // no tier, no stance
    expect(within(finding).getAllByText('lead')).toHaveLength(2) // source and by
    fireEvent.click(screen.getByRole('tab', { name: /Memory/ }))
    expect(await screen.findByText(/list users/)).toBeInTheDocument()
    expect(screen.getByText(/tool down/)).toBeInTheDocument()
  })

  function investigateCase(projection: unknown) {
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
    testState.runs['run-inv'] = { status: 'completed', projection }
    renderCase('case-inv')
  }

  it('shows a kernel investigation\'s record: explanations with their counts, linked rows with their stance', async () => {
    investigateCase(investigateProjection)

    fireEvent.click(await screen.findByRole('tab', { name: /Explanations/ }))
    const redTeam = (await screen.findByText('The activity is part of an authorized red-team engagement.')).closest('li') as HTMLElement
    expect(within(redTeam).getByText('2 for')).toBeInTheDocument()
    expect(within(redTeam).getByText('0 against')).toBeInTheDocument()
    expect(within(redTeam).getByText('Standing')).toBeInTheDocument()
    expect(screen.queryByText('This workflow does not test explanations yet.')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: /^Evidence/ }))
    expect(await screen.findByRole('heading', { name: 'Evidence trail · 3 rows' })).toBeInTheDocument()
    const finding = document.querySelector('[data-evidence-id="r2"]') as HTMLElement
    expect(within(finding).getByText('Supports')).toBeInTheDocument()
    expect(within(finding).getByText(/accessing lsass\.exe/)).toBeInTheDocument()
    // Links that disagree show each stance under "bears on", none in the column.
    const mixed = document.querySelector('[data-evidence-id="r9"]') as HTMLElement
    expect(within(mixed).getByText(/^Supports The activity is part.*· Goes against The user/)).toBeInTheDocument()
    expect(within(mixed).queryByText('Supports')).not.toBeInTheDocument()
  })

  it('shows a held dismissal as what it was held as, what was asked, and why; its open question as a blind spot', async () => {
    investigateCase(heldProjection)

    expect(await screen.findByText(/^Asked benign · held as needs review: the review was insufficient: Only the case note/)).toBeInTheDocument()
    expect(screen.getByText(/^Needs review — EDR detected/)).toBeInTheDocument()
    expect(screen.queryByText(/"disposition":"benign"/)).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: /Memory/ }))
    expect(await screen.findByText(/registered red-team operator on ws-0901/)).toBeInTheDocument()
    expect(screen.getByText('Open question')).toBeInTheDocument()
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
    testState.runs['run-hunt'] = { status: 'running', hunt: HUNT }
    testState.recordRows = [
      { id: 'r1', kind: 'run', text: 'started', at: '2026-06-15T09:00:00Z', chained: true },
      { id: 'r2', kind: 'directive', text: 'note', at: '2026-06-15T09:01:00Z', chained: false },
    ]
    renderCase('case-agents')

    const table = await screen.findByRole('table', { name: 'Agents' })
    const rows = within(table).getAllByRole('row')
    expect(rows).toHaveLength(2)
    const clock = '09:14' // UTC, whatever the machine's zone
    for (const text of ['threat_hunter', 'who logged in', 'search', clock, 'Working']) {
      expect(within(rows[0]).getByText(text)).toBeInTheDocument()
    }
    // The older move of the same worker is not a second row; the state sits on the latest row only.
    expect(within(table).queryByText('rank the hosts')).not.toBeInTheDocument()
    expect(within(rows[1]).getByText('network_analyst')).toBeInTheDocument()
    expect(within(rows[1]).getByText('proxy_query')).toBeInTheDocument()
    expect(within(rows[1]).queryByText('Working')).not.toBeInTheDocument()
    expect(within(rows[1]).getByText('Idle')).toBeInTheDocument()
    expect(table.textContent).not.toContain('executing')
    expect(screen.queryByText(/incident-response · executing/)).not.toBeInTheDocument()

    const doors = screen.getByRole('region', { name: 'Audit doors' })
    expect(within(doors).getAllByRole('button')).toHaveLength(5)
    expect(within(doors).getByText('1 forming · 1 ruled out')).toBeInTheDocument()
    expect(within(doors).getByText('0 support · 2 go against · 0 neither')).toBeInTheDocument()
    expect(within(doors).getByText('$0.20 · 0 gaps')).toBeInTheDocument()
    expect(within(doors).getByText('Nothing recalled')).toBeInTheDocument()
    await waitFor(() => expect(within(doors).getByText('2 rows · 1 chained')).toBeInTheDocument())
    fireEvent.click(within(doors).getByRole('button', { name: /^Checked/ }))
    expect(await screen.findByText(/What Vigil checked/)).toBeInTheDocument()
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
    expect(await within(now).findByText('The run has started and has not reported yet.')).toBeInTheDocument()
    expect(workflowApi.getRun).toHaveBeenCalledWith('run-only')
    // A run that has found nothing says so inside the Findings so far card.
    const findings = screen.getByRole('region', { name: 'Findings so far' })
    expect(findings).toHaveClass('case-card')
    expect(within(findings).getByRole('heading', { name: 'Findings so far' })).toBeInTheDocument()
    expect(within(findings).getByText('No findings yet.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Export' })).not.toBeInTheDocument()
  })

  it('shows a dash for a lead decision time and keeps the honest explanations line', async () => {
    testState.cases = [{ ...openCase('case-lead'), combined_state: 'executing', investigations: [investigation('executing', true, 'run-lead')] }]
    testState.runs['run-lead'] = {
      status: 'running',
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
    expect(rows.map((row) => row.textContent)).toEqual([expect.stringContaining('worker_bExamine——'), expect.stringContaining('worker_aDispatch——')])
    expect(within(await screen.findByRole('region', { name: 'Now' })).getByText('worker_b · since —')).toBeInTheDocument()
    const doors = screen.getByRole('region', { name: 'Audit doors' })
    expect(within(doors).getByText('Does not test explanations yet')).toBeInTheDocument()
    expect(within(doors).getByText('Findings, no for or against')).toBeInTheDocument()
  })

  it('shows a case with no run only the agents card and the doors, and keeps the Now card copy for a loading and an unreadable run', async () => {
    testState.cases = [
      { ...openCase('case-none'), investigations: [] },
      { ...openCase('case-slow'), combined_state: 'executing', investigations: [investigation('executing', true, 'run-slow')] },
      { ...openCase('case-bad'), combined_state: 'executing', investigations: [investigation('executing', true, 'run-bad')] },
    ]
    renderCase('case-none')
    expect(await screen.findByText('No live investigation.')).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Now' })).not.toBeInTheDocument()
    expect(screen.queryByText('What it changed')).not.toBeInTheDocument()
    expect(screen.queryByText('Planned next')).not.toBeInTheDocument()
    expect(screen.queryByText('Findings so far')).not.toBeInTheDocument()
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

  describe('Checked tab', () => {
    const leadCase = (run: unknown) => {
      testState.cases = [{
        case_id: 'case-chk',
        title: 'Checked case',
        status: 'open',
        priority: 'low',
        finding_ids: [],
        created_at: '2026-06-15T09:14:00Z',
        combined_state: 'open',
        investigations: [investigation('completed', false, 'run-chk')],
      }]
      testState.runs['run-chk'] = run
      renderCase('case-chk')
    }
    const openChecked = async () => {
      await screen.findByRole('heading', { name: 'Checked case' })
      fireEvent.click(screen.getByRole('tab', { name: /^Checked/ }))
    }
    const lead = (extra: Record<string, unknown>) => ({
      projection: { iterations: 1, decisions: [], findings: [], calls: [], gaps: [], recall: null, outcome: null, reason: '', cost_usd: null, ...extra },
    })

    it('says the run is loading, not that nothing was asked', async () => {
      testState.cases = [{ ...testState.cases[0], case_id: 'case-chk', title: 'Checked case', finding_ids: [], investigations: [investigation('open', false, 'run-chk')] }]
      vi.mocked(workflowApi.getRun).mockReturnValueOnce(new Promise(() => {}))
      renderCase('case-chk')
      await openChecked()
      expect(await screen.findByText('Loading the run…', { selector: 'h3' })).toBeInTheDocument()
      expect(screen.queryByText('No questions asked yet')).not.toBeInTheDocument()
    })

    it('says the run could not be read', async () => {
      testState.cases = [{ case_id: 'case-chk', title: 'Checked case', status: 'open', priority: 'low', finding_ids: [], created_at: '2026-06-15T09:14:00Z', combined_state: 'open', investigations: [investigation('open', false, 'run-chk')] }]
      vi.mocked(workflowApi.getRun).mockRejectedValueOnce(new Error('down'))
      renderCase('case-chk')
      await openChecked()
      expect(await screen.findByRole('alert')).toHaveTextContent('The run could not be read.')
    })

    it('lists the calls only, counts the no-source gaps in the header; "—" when the run has no cost', async () => {
      leadCase(lead({
        calls: [{ question: 'who logged in', tool: 'okta', result_length: 1240, cost_usd: 0.02, duration_ms: 800 }],
        gaps: [{ dispatch_id: 'dsp-1', agent_id: 'worker', failure_reason: 'tool down', query_intent: 'list users' }],
      }))
      await openChecked()
      const card = (await screen.findByText(/What Vigil checked/)).closest('section') as HTMLElement
      expect(card.querySelector('h3')).toHaveTextContent('What Vigil checked · 1 query · — · 1 with no source')
      // the gaps themselves are read on Memory and blind spots
      const rows = card.querySelectorAll('li')
      expect(rows).toHaveLength(1)
      expect(rows[0]).toHaveTextContent('okta')
      expect(rows[0]).toHaveTextContent('1,240 chars')
      expect(rows[0]).toHaveTextContent('800 ms')
      expect(card).not.toHaveTextContent('tool down')
      fireEvent.click(within(card).getByRole('button', { name: 'About the cost' }))
      expect(screen.getByRole('tooltip')).toHaveTextContent(/turn's cost/)
    })

    it('keeps the header when there are gaps but no calls', async () => {
      leadCase(lead({ cost_usd: 0.5, gaps: [{ dispatch_id: 'dsp-1', agent_id: 'worker', failure_reason: null, query_intent: 'list users' }] }))
      await openChecked()
      const card = (await screen.findByText(/What Vigil checked/)).closest('section') as HTMLElement
      expect(card.querySelector('h3')).toHaveTextContent('What Vigil checked · 0 queries · $0.50 · 1 with no source')
      expect(card.querySelectorAll('li')).toHaveLength(0)
    })

    it('shows the empty state for a ready run with no calls and no gaps', async () => {
      leadCase(lead({}))
      await openChecked()
      expect(await screen.findByText('No questions asked yet')).toBeInTheDocument()
    })
  })

  const CLOSURE = {
    closure_category: 'false_positive',
    closed_by: 'ada',
    closed_by_kind: 'analyst',
    closed_at: '2026-06-15T15:48:00Z',
    verdict: 'the scanner',
  }

  function closedCase(closure: Record<string, unknown> | null, investigations: unknown[] = [investigation('completed', false, 'run-closed')]) {
    testState.cases = [{
      case_id: 'case-closed',
      title: 'Closed case',
      status: 'closed',
      priority: 'high',
      finding_ids: [],
      created_at: '2026-06-15T09:14:00Z',
      combined_state: 'closed',
      closure,
      investigations,
    }]
  }

  it('shows the closed line and verdict, and reopens through the status update', async () => {
    closedCase(CLOSURE)
    testState.runs['run-closed'] = { projection: { iterations: 1, decisions: [], findings: [], calls: [], gaps: [], recall: null } }
    renderCase('case-closed')

    expect(await screen.findByRole('heading', { name: 'the scanner' })).toBeInTheDocument()
    // the header reason and the Summary read the same line
    expect(screen.getAllByText(/^Closed Jun 15, 2026 · \d\d:\d\d by ada · False positive · analyst$/)).toHaveLength(2)
    expect(document.querySelector('.case-sla')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Replay' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Agree' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Disagree' })).toBeDisabled()
    expect(screen.getByText('Coming in a later release')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Reopen' }))
    await waitFor(() => expect(casesApi.update).toHaveBeenCalledWith('case-closed', { status: 'open' }))
  })

  it('drops a missing category and closer kind from the Closed line without stray separators', async () => {
    closedCase({ ...CLOSURE, closure_category: '', closed_by_kind: '' })
    testState.runs['run-closed'] = { projection: { iterations: 1, decisions: [], findings: [], calls: [], gaps: [], recall: null } }
    renderCase('case-closed')

    expect(await screen.findAllByText(/^Closed Jun 15, 2026 · \d\d:\d\d by ada$/)).toHaveLength(2)
  })

  it('shows Loading the run, never the honest line, on Explanations while the case read is in flight', async () => {
    const lead = { projection: { iterations: 1, decisions: [], findings: [], calls: [], gaps: [], recall: null } }
    testState.runs['run-closed'] = lead
    closedCase(CLOSURE)
    let release: () => void = () => {}
    const held = new Promise<void>((resolve) => { release = resolve })
    const row = testState.cases[0]
    vi.mocked(casesApi.getById).mockImplementationOnce((async () => { await held; return { data: row } }) as never)
    renderCase('case-closed')
    fireEvent.click(await screen.findByRole('tab', { name: /Explanations/ }))
    expect(screen.getByText('Loading the run…')).toBeInTheDocument()
    expect(screen.queryByText('This workflow does not test explanations yet.')).not.toBeInTheDocument()
    release()
    expect(await screen.findByText('This workflow does not test explanations yet.')).toBeInTheDocument()
  })

  it('lists the strongest hunt rows with step and stance, both sides first', async () => {
    closedCase(CLOSURE)
    const ev = (n: number, relation: string | null, extra: Record<string, unknown> = {}) => ({
      evidence_id: `e${n}`, iteration: n, source_system: 'elastic', summary: `row ${n}`, is_gap: false, gap_detail: null,
      bears_on: relation ? [{ hypothesis_id: 'h1', relation }] : [], ...extra,
    })
    testState.runs['run-closed'] = {
      hunt: { ...HUNT, evidence: [ev(1, 'supports'), ev(2, 'supports'), ev(3, 'supports'), ev(4, 'weakens'), ev(5, null), ev(6, 'weakens', { is_gap: true })] },
    }
    renderCase('case-closed')

    const list = await screen.findByRole('list')
    const rows = within(list).getAllByRole('listitem').map((li) => li.textContent)
    expect(rows).toEqual(['Step 1row 1For', 'Step 2row 2For', 'Step 4row 4Against'])
  })

  it('shows a lead fold’s answers with dashes for step and stance', async () => {
    closedCase(CLOSURE)
    testState.runs['run-closed'] = { projection: { iterations: 1, decisions: [], findings: [{ agent_id: 'triage', answer: 'benign scanner' }], calls: [], gaps: [], recall: null } }
    renderCase('case-closed')

    const list = await screen.findByRole('list')
    expect(within(list).getAllByRole('listitem').map((li) => li.textContent)).toEqual(['—benign scanner—'])
  })

  it('drops the time without a closed_at and shows a dash for an empty verdict', async () => {
    closedCase({ ...CLOSURE, closed_at: null, verdict: '' })
    testState.runs['run-closed'] = { projection: { iterations: 1, decisions: [], findings: [], calls: [], gaps: [], recall: null } }
    renderCase('case-closed')

    expect(await screen.findByRole('heading', { name: '—' })).toBeInTheDocument()
    expect(screen.getAllByText(/^Closed by ada/)).toHaveLength(2)
    expect(document.body.textContent).not.toMatch(/Invalid Date/)
    expect(await screen.findByText('No findings yet.')).toBeInTheDocument()
  })

  it('says so while the run loads', async () => {
    closedCase(CLOSURE)
    vi.mocked(workflowApi.getRun).mockReturnValueOnce(new Promise(() => {}))
    renderCase('case-closed')
    const closed = await screen.findByRole('region', { name: 'Closed summary' }, { timeout: 5000 })
    expect(within(closed).getByText('Loading the run…')).toBeInTheDocument()
  })

  it('says so when the run cannot be read', async () => {
    closedCase(CLOSURE)
    vi.mocked(workflowApi.getRun).mockRejectedValueOnce(new Error('boom'))
    renderCase('case-closed')
    expect(await screen.findByText('The run could not be read.')).toBeInTheDocument()
  })

  it('keeps "No findings yet." for a closed case with no run', async () => {
    closedCase(CLOSURE, [])
    renderCase('case-closed')
    await screen.findByRole('heading', { name: 'the scanner' })
    expect(screen.getByText('No findings yet.')).toBeInTheDocument()
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
        { finding_id: 'f1', title: 'Beacon to rare host', description: 'console alert', source_link: 'https://example.test/alert/1', entities: ['web-1', 'alice'] },
        { finding_id: 'f2', description: 'no door', source_link: null, entities: ['alice', 'db-2'] },
      ],
    }]
    testState.runs['run-links'] = { hunt: HUNT }
    renderCase('case-links')

    const header = (await screen.findByRole('heading', { name: 'Linked case' })).closest('.detail-head') as HTMLElement
    expect(within(header).getByText('Combined from 3 alerts')).toBeInTheDocument()
    expect(within(header).queryByText('console alert')).not.toBeInTheDocument()
    const side = screen.getByRole('complementary', { name: 'Case details' })
    expect(within(side).getByText('Alerts (2)')).toBeInTheDocument()
    // The title names the alert; a row without one keeps its description.
    expect(within(side).getByText('Beacon to rare host')).toBeInTheDocument()
    expect(within(side).queryByText('console alert')).not.toBeInTheDocument()
    // The Entities row is what the linked alerts name, once each, while the run recalls none.
    expect(Array.from(side.querySelectorAll('.side-ent')).map((el) => el.textContent)).toEqual(['web-1', 'alice', 'db-2'])
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

  it('puts Details first in the side panel and folds Alerts after three with +N', async () => {
    testState.cases = [{
      case_id: 'case-many',
      title: 'Many alerts',
      status: 'open',
      priority: 'high',
      finding_ids: [],
      created_at: '2026-06-15T09:14:00Z',
      combined_state: 'executing',
      investigations: [investigation('executing', true, 'run-many')],
      linked_findings: ['a', 'b', 'c', 'd', 'e'].map((id) => ({ finding_id: id, title: `Alert ${id}`, source_link: null })),
    }]
    testState.runs['run-many'] = { hunt: HUNT }
    renderCase('case-many')

    const side = await screen.findByRole('complementary', { name: 'Case details' })
    const titles = within(side).getAllByRole('heading').map((h) => h.textContent)
    expect(titles.slice(0, 2)).toEqual(['Details', 'Alerts (5)'])
    expect(within(side).queryByText('Alert d')).not.toBeInTheDocument()
    fireEvent.click(within(side).getByRole('button', { name: '+2' }))
    expect(within(side).getByText('Alert e')).toBeInTheDocument()
    // Summary carries no count; the other tabs keep theirs.
    expect(screen.getByRole('tab', { name: 'Summary' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /^Explanations/ })).toHaveTextContent(/\d/)
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

  describe('Record tab', () => {
    const rows = [
      { id: 'r1', at: '2026-06-15T10:00:00Z', kind: 'agent', source: 'run', chained: true, text: 'Lead started step 7' },
      { id: 'r2', at: '2026-06-15T10:05:00Z', kind: 'recall', source: 'run', chained: true, text: 'Recalled 3 rows' },
      { id: 'r3', at: '2026-06-15T10:09:00Z', kind: 'case_audit_logs', source: 'case', chained: false, text: 'M. Kaur closed the task' },
      { id: 'r4', at: '2026-06-15T10:11:00Z', kind: 'run', source: 'run', chained: true, text: 'Run started' },
    ]

    const openRecord = async (ui: () => ReturnType<typeof renderCase> | ReturnType<typeof renderDetail> = () => renderCase('case-rec')) => {
      testState.cases = [{
        case_id: 'case-rec',
        title: 'Record case',
        status: 'open',
        priority: 'low',
        finding_ids: [],
        created_at: '2026-06-15T09:14:00Z',
        combined_state: 'executing',
        investigations: [investigation('executing', true, 'run/1')],
      }]
      testState.runs['run/1'] = { hunt: HUNT }
      ui()
      fireEvent.click(await screen.findByRole('tab', { name: /Record/ }))
    }

    it('types each row, counts All, and filters by chip', async () => {
      testState.recordRows = rows
      await openRecord()
      expect(await screen.findByRole('button', { name: 'All 4' })).toHaveAttribute('aria-pressed', 'true')
      expect(screen.getByText('Lead started step 7').closest('.rec-row')).toHaveTextContent('Agent')
      expect(screen.getByText('Lead started step 7').closest('.rec-row')).toHaveTextContent('chained')
      expect(screen.getByText('M. Kaur closed the task').closest('.rec-row')).not.toHaveTextContent('chained')
      expect(screen.queryByRole('button', { name: 'Gate' })).not.toBeInTheDocument()
      for (const name of ['Agent 1', 'Human 1', 'Memory 1', 'System 1']) expect(screen.getByRole('button', { name })).toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'Human 1' }))
      expect(screen.getByRole('button', { name: 'Human 1' })).toHaveAttribute('aria-pressed', 'true')
      expect(screen.getByText('M. Kaur closed the task')).toBeInTheDocument()
      expect(screen.queryByText('Lead started step 7')).not.toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'Memory 1' }))
      expect(screen.getByText('Recalled 3 rows')).toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'System 1' }))
      expect(screen.getByText('Run started')).toBeInTheDocument()
      expect(screen.queryByText('Recalled 3 rows')).not.toBeInTheDocument()
    })

    it('labels each run row with its run, and an unread run is a row, not the error state', async () => {
      testState.recordRows = [
        { id: 'run:root:1', at: '2026-06-15T10:20:00Z', kind: 'run', source: 'run', chained: true, text: 'Root cause started', run_id: 'root-run' },
        { id: 'run:gone:unread', at: '2026-06-15T10:15:00Z', kind: 'unread', source: 'run', chained: false, text: 'Could not read the record of run gone-run-1234 — boom', run_id: 'gone-run-1234' },
        { id: 'run:run/1:1', at: '2026-06-15T10:00:00Z', kind: 'run', source: 'run', chained: true, text: 'Hunt started', run_id: 'run/1' },
        { id: 'audit:1', at: '2026-06-15T10:09:00Z', kind: 'case_audit_logs', source: 'case_audit_logs', chained: false, text: 'M. Kaur closed the task' },
      ]
      testState.cases = [{
        case_id: 'case-rec',
        title: 'Record case',
        status: 'open',
        priority: 'low',
        finding_ids: [],
        created_at: '2026-06-15T09:14:00Z',
        combined_state: 'open',
        investigations: [
          { ...investigation('completed', false, 'root-run'), investigation_id: null, workflow_id: 'root-cause', primary: false, created_at: '2026-06-15T10:20:00Z' },
          { ...investigation('completed', false, 'run/1'), created_at: '2026-06-15T09:58:00Z' },
        ],
      }]
      testState.runs['run/1'] = { hunt: HUNT }
      testState.runs['root-run'] = { hunt: HUNT }
      renderCase('case-rec')
      fireEvent.click(await screen.findByRole('tab', { name: /Record/ }))
      const row = async (text: string) => (await screen.findByText(text, { exact: false })).closest('.rec-row')
      expect(await row('Hunt started')).toHaveTextContent('Incident response · started 09:58')
      expect(await row('Root cause started')).toHaveTextContent('root-cause · started 10:20')
      // a run the case does not list falls back to a short id
      expect(await row('Could not read the record')).toHaveTextContent('gone-run')
      expect(await row('Could not read the record')).toHaveTextContent('System')
      expect(await row('Could not read the record')).not.toHaveTextContent('chained')
      expect((await row('M. Kaur closed the task'))?.querySelector('.rec-run')).toBeNull()
      expect(screen.queryByText('Couldn’t load the record')).not.toBeInTheDocument()
    })

    it('shows the empty and loading states', async () => {
      testState.recordRows = []
      await openRecord()
      expect(await screen.findByText('No record yet')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'All 0' })).toBeInTheDocument()
    })

    it('opens the ⓘ note on the chain', async () => {
      testState.recordRows = rows
      await openRecord()
      fireEvent.click(await screen.findByRole('button', { name: 'About the record' }))
      expect(screen.getByRole('tooltip')).toHaveTextContent('Only run rows are hash-chained. Case audit and activity rows are not.')
    })

    it('Replay links to the run on Workflows, and closes the drawer', async () => {
      testState.recordRows = rows
      const onBack = vi.fn()
      await openRecord(() => renderDetail('case-rec', { onBack, onExpand: vi.fn() }))
      const link = await screen.findByRole('link', { name: 'Replay' })
      expect(link).toHaveAttribute('href', '/workflows?run=run%2F1')
      fireEvent.click(link)
      expect(onBack).toHaveBeenCalled()
    })

    it('shows Replay disabled, with an ⓘ saying why, without a run', async () => {
      testState.recordRows = rows
      testState.cases = [{
        case_id: 'case-rec',
        title: 'Record case',
        status: 'open',
        priority: 'low',
        finding_ids: [],
        created_at: '2026-06-15T09:14:00Z',
        combined_state: 'open',
        investigations: [],
      }]
      renderCase('case-rec')
      fireEvent.click(await screen.findByRole('tab', { name: /Record/ }))
      await screen.findByText('Lead started step 7')
      expect(screen.queryByRole('link', { name: 'Replay' })).not.toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Replay' })).toBeDisabled()
      fireEvent.click(screen.getByRole('button', { name: 'Why Replay is unavailable' }))
      expect(screen.getByRole('tooltip')).toHaveTextContent('This case has no run to replay.')
      expect(screen.queryByRole('button', { name: 'Verify chain' })).not.toBeInTheDocument()
    })

    it('Verify chain and Export audit keep their behaviour', async () => {
      testState.recordRows = rows
      let resolveVerify: (v: unknown) => void = () => {}
      vi.mocked(workflowApi.verifyRun).mockReturnValue(new Promise((r) => { resolveVerify = r }) as never)
      vi.mocked(orchestratorApi.exportInvestigation).mockResolvedValue({ data: { a: 1 } } as never)
      const createUrl = vi.fn(() => 'blob:x')
      Object.assign(URL, { createObjectURL: createUrl, revokeObjectURL: vi.fn() })
      await openRecord()
      fireEvent.click(await screen.findByRole('button', { name: 'Verify chain' }))
      const bar = document.querySelector('.rec-bar') as HTMLElement
      expect(within(bar).getByText('Verifying…')).toBeInTheDocument()
      resolveVerify({ data: { ok: true, events: 12 } })
      const result = await within(bar).findByText(/Chain verified · 12 events/)
      expect(result.closest('[aria-live="polite"]')).not.toBeNull()
      expect(within(bar).getByText('Good')).toBeInTheDocument()
      expect(document.querySelector('.detail-body > p.muted')).toBeNull()
      expect(workflowApi.verifyRun).toHaveBeenCalledWith('run/1')
      fireEvent.click(screen.getByRole('button', { name: 'Export audit' }))
      await waitFor(() => expect(orchestratorApi.exportInvestigation).toHaveBeenCalledWith('inv-1'))
      expect(createUrl).toHaveBeenCalled()
    })
    it('reports a chain break and a failed export as Poor in the Record header, and clears them on tab leave', async () => {
      testState.recordRows = rows
      vi.mocked(workflowApi.verifyRun).mockResolvedValue({ data: { ok: false, reason: 'hash mismatch at 4' } } as never)
      vi.mocked(orchestratorApi.exportInvestigation).mockRejectedValue({ response: { data: { detail: 'Export failed: no access' } } })
      await openRecord()
      fireEvent.click(await screen.findByRole('button', { name: 'Verify chain' }))
      const bar = document.querySelector('.rec-bar') as HTMLElement
      expect(await within(bar).findByText(/Chain break: hash mismatch at 4/)).toBeInTheDocument()
      expect(within(bar).getByText('Poor')).toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'Export audit' }))
      expect(await within(bar).findByText(/Export failed: no access/)).toBeInTheDocument()
      expect(within(bar).getByText('Poor')).toBeInTheDocument()
      fireEvent.click(screen.getByRole('tab', { name: /^Summary/ }))
      fireEvent.click(await screen.findByRole('tab', { name: /^Record/ }))
      await screen.findByRole('button', { name: 'Verify chain' })
      expect(screen.queryByText(/Export failed/)).not.toBeInTheDocument()
    })
    it('disables Export audit, with an ⓘ saying why, when the run has no investigation id', async () => {
      testState.recordRows = rows
      await openRecord(() => {
        const [row] = testState.cases as unknown as { investigations: unknown[] }[]
        row.investigations = row.investigations.map((item) => ({ ...(item as object), investigation_id: null }))
        return renderCase('case-rec')
      })
      const button = await screen.findByRole('button', { name: 'Export audit' })
      expect(button).toBeDisabled()
      vi.mocked(orchestratorApi.exportInvestigation).mockClear()
      fireEvent.click(button)
      expect(orchestratorApi.exportInvestigation).not.toHaveBeenCalled()
      fireEvent.click(screen.getByRole('button', { name: 'Why Export audit is unavailable' }))
      expect(screen.getByRole('tooltip')).toHaveTextContent('This run has no investigation record to export.')
    })

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

  it('sends an incoming seed as typed on the case thread, then clears it', async () => {
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
    const onSeedConsumed = vi.fn()
    renderDetail('case-hunt', { seed: 'what happened on web-01?', onSeedConsumed })
    await waitFor(() => expect(streamFetch).toHaveBeenCalled())
    const body = JSON.parse((vi.mocked(streamFetch).mock.calls[0][1] as { body: string }).body)
    expect(body.case_id).toBe('case-hunt')
    expect(body.messages.at(-1)).toEqual({ role: 'user', content: 'what happened on web-01?' })
    expect(onSeedConsumed).toHaveBeenCalled()
  })

  it('asks where the runs disagree only on a case with more than one run', async () => {
    const twoRuns = (investigations: unknown[]) => [{
      case_id: 'case-runs',
      title: 'Runs case',
      status: 'open',
      priority: 'high',
      finding_ids: [],
      created_at: '2026-06-15T09:14:00Z',
      combined_state: 'open',
      investigations,
    }]
    testState.cases = twoRuns([investigation('completed', false, 'run-a')])
    const { unmount } = renderDetail('case-runs')
    expect(await screen.findByRole('heading', { name: 'Findings so far' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Where do the runs disagree?' })).not.toBeInTheDocument()
    unmount()

    testState.cases = twoRuns([
      investigation('completed', false, 'run-a'),
      { ...investigation('completed', false, 'run-b'), investigation_id: 'inv-2', primary: false },
    ])
    renderDetail('case-runs')
    fireEvent.click(await screen.findByRole('button', { name: 'Where do the runs disagree?' }))
    await waitFor(() => expect(streamFetch).toHaveBeenCalled())
    const body = JSON.parse((vi.mocked(streamFetch).mock.calls[0][1] as { body: string }).body)
    expect(body.messages.at(-1)).toEqual({ role: 'user', content: "Where do this case's runs disagree, and what did each one say?" })
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
      messages: [{ role: 'assistant', content: 'The row e1 matters; ghost-9 does not, and h1 is ruled out.' }],
    }]
    const { showCase } = renderDetail('case-hunt')

    // the chip names the step and source the row came from (#2285), where the answer wrote the id and nowhere else (#2565)
    const chips = await screen.findAllByRole('button', { name: 'step 2 · elastic' })
    expect(chips).toHaveLength(1)
    expect(chips[0]).toHaveAttribute('title', 'no login')
    expect(screen.queryByRole('button', { name: 'e1' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'ghost-9' })).not.toBeInTheDocument()
    expect(screen.queryByText('no login')).not.toBeInTheDocument()
    fireEvent.click(chips[0])
    expect(screen.getByRole('tab', { name: /^Evidence/ })).toHaveAttribute('aria-selected', 'true')
    const row = (await screen.findByText('no login')).closest('[data-evidence-id]')
    expect(row).toHaveAttribute('data-evidence-id', 'e1')
    expect(row).toHaveClass('cite-target')

    // an explanation the answer names is a chip too, and opens the Explanations tab
    fireEvent.click(screen.getByRole('button', { name: 'explanation · The host is owned' }))
    expect(screen.getByRole('tab', { name: /^Explanations/ })).toHaveAttribute('aria-selected', 'true')

    showCase('case-next')
    expect(await screen.findByRole('heading', { name: 'Next case' })).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByText(/ghost-9 does not/)).not.toBeInTheDocument())
  })

  const decCase = {
    case_id: 'case-dec',
    title: 'Decision case',
    status: 'open',
    priority: 'high',
    finding_ids: [],
    created_at: '2026-06-15T09:14:00Z',
    combined_state: 'open',
    investigations: [],
  }
  const tick = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms) })
  const queue = (...items: NeedsYouItem[]) =>
    vi.mocked(approvalsApi.needsYou).mockResolvedValue({ data: { count: items.length, items } } as never)
  const irreversible = () =>
    need({ source_id: 'act-irr', title: 'Isolate host', reason: '', reversibility: 'irreversible' })

  it('shows a reversible and an irreversible ask with their own controls', async () => {
    testState.cases = [decCase]
    queue(need(), irreversible())
    renderCase('case-dec')

    expect(await screen.findByRole('heading', { name: 'Block 1.2.3.4' })).toBeInTheDocument()
    const body = document.querySelector('.detail-body') as HTMLElement
    expect(body.textContent?.indexOf('Block 1.2.3.4')).toBeLessThan(body.textContent?.indexOf('Agents on this case') ?? -1)
    expect(body.textContent?.indexOf('Block 1.2.3.4')).toBeLessThan(body.textContent?.indexOf('Isolate host') ?? -1)
    expect(screen.getByText('beacon')).toBeInTheDocument()
    expect(screen.getByText('Reversible')).toBeInTheDocument()
    expect(screen.getByText('Cannot be undone')).toBeInTheDocument()
    // Reversible asks never hold; irreversible ones have no plain Approve.
    expect(screen.getAllByRole('button', { name: 'Approve' })).toHaveLength(1)
    expect(screen.getAllByRole('button', { name: /Press and hold to confirm/ })).toHaveLength(1)
    expect(screen.getByText('Hold to approve')).toBeInTheDocument()
    expect(approvalsApi.needsYou).toHaveBeenCalledWith('case-dec')
  })

  it('approves a reversible ask after the 8 s fuse, not before, then refreshes the list', async () => {
    testState.cases = [decCase]
    queue(need())
    vi.mocked(approvalsApi.approve).mockResolvedValue({} as never)
    renderCase('case-dec')
    await screen.findByRole('heading', { name: 'Block 1.2.3.4' })
    const changed = vi.fn()
    window.addEventListener(CASES_CHANGED, changed)

    vi.useFakeTimers()
    try {
      const before = vi.mocked(approvalsApi.needsYou).mock.calls.length
      fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
      expect(screen.getByText('Approving: Block 1.2.3.4')).toBeInTheDocument()
      expect(screen.queryByRole('heading', { name: 'Block 1.2.3.4' })).not.toBeInTheDocument()
      // The fuse takes the item out of needs-you, but a run-less case grows no Now card in its place.
      expect(screen.queryByRole('region', { name: 'Now' })).not.toBeInTheDocument()

      await tick(7999)
      expect(approvalsApi.approve).not.toHaveBeenCalled()
      expect(changed).not.toHaveBeenCalled()
      queue()
      await tick(1)
      expect(approvalsApi.approve).toHaveBeenCalledWith('act-1')
      expect(screen.getByText('Approved: Block 1.2.3.4')).toBeInTheDocument()
      expect(vi.mocked(approvalsApi.needsYou).mock.calls.length).toBeGreaterThan(before)
      expect(changed).toHaveBeenCalledTimes(1)
    } finally {
      window.removeEventListener(CASES_CHANGED, changed)
      vi.useRealTimers()
    }
  })

  it('Approve is the accent primary, not the destructive outline, and shows the undo toast at once', async () => {
    testState.cases = [decCase]
    queue(need())
    renderCase('case-dec')
    await screen.findByRole('heading', { name: 'Block 1.2.3.4' })
    const approve = screen.getByRole('button', { name: 'Approve' })
    expect(approve).toHaveClass('btn', 'primary')
    expect(approve).not.toHaveClass('danger')
    fireEvent.click(approve)
    expect(screen.getByText('Approving: Block 1.2.3.4')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Undo' })).toBeInTheDocument()
  })

  it('Undo sends nothing and brings the block back', async () => {
    testState.cases = [decCase]
    queue(need())
    renderCase('case-dec')
    await screen.findByRole('heading', { name: 'Block 1.2.3.4' })

    vi.useFakeTimers()
    try {
      fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
      await tick(3000)
      fireEvent.click(screen.getByRole('button', { name: 'Undo' }))
      expect(screen.getByRole('heading', { name: 'Block 1.2.3.4' })).toBeInTheDocument()
      await tick(10_000)
      expect(approvalsApi.approve).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('sends the typed reason when rejecting, through the fuse', async () => {
    testState.cases = [decCase]
    queue(need({ source_id: 'act-no', title: 'Disable account' }))
    vi.mocked(approvalsApi.reject).mockResolvedValue({} as never)
    renderCase('case-dec')

    fireEvent.click(await screen.findByRole('button', { name: 'Reject' }))
    const submit = screen.getByRole('button', { name: 'Confirm reject' })
    expect(submit).toBeDisabled()
    fireEvent.submit(submit.closest('form') as HTMLFormElement)
    fireEvent.change(screen.getByRole('textbox', { name: 'Rejection reason' }), {
      target: { value: 'not our host' },
    })

    vi.useFakeTimers()
    try {
      fireEvent.click(submit)
      expect(approvalsApi.reject).not.toHaveBeenCalled()
      expect(screen.getByText('Rejecting: Disable account')).toBeInTheDocument()
      await tick(8000)
      expect(approvalsApi.reject).toHaveBeenCalledOnce()
      expect(approvalsApi.reject).toHaveBeenCalledWith('act-no', 'not our host')
      expect(screen.getByText('Rejected: Disable account')).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('commits a held irreversible approve at once, with no fuse', async () => {
    testState.cases = [decCase]
    queue(irreversible())
    vi.mocked(approvalsApi.approve).mockResolvedValue({} as never)
    renderCase('case-dec')
    const hold = await screen.findByRole('button', { name: /Press and hold to confirm/ })

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
      expect(approvalsApi.approve).not.toHaveBeenCalled()

      fireEvent.pointerDown(hold)
      act(() => {
        vi.advanceTimersByTime(1600)
      })
      expect(approvalsApi.approve).toHaveBeenCalledWith('act-irr')
      expect(screen.queryByText(/Approving:/)).not.toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('keeps the item hidden across a needs-you poll while the commit is in flight', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      testState.cases = [decCase]
      queue(need())
      vi.mocked(approvalsApi.approve).mockReturnValue(new Promise(() => {}) as never)
      renderCase('case-dec')
      await screen.findByRole('heading', { name: 'Block 1.2.3.4' })

      fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
      const before = vi.mocked(approvalsApi.needsYou).mock.calls.length
      await tick(21_000) // past the 8 s fuse and the 20 s poll; the server still lists the item
      expect(approvalsApi.approve).toHaveBeenCalledOnce()
      expect(vi.mocked(approvalsApi.needsYou).mock.calls.length).toBeGreaterThan(before)
      expect(screen.queryByRole('heading', { name: 'Block 1.2.3.4' })).not.toBeInTheDocument()
      expect(screen.queryByText('Needs you')).not.toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('refreshes the list when the fused commit fails, and the block returns', async () => {
    testState.cases = [decCase]
    queue(need())
    vi.mocked(approvalsApi.approve).mockRejectedValue({ response: { data: { detail: 'already decided' } } })
    renderCase('case-dec')
    await screen.findByRole('heading', { name: 'Block 1.2.3.4' })
    const changed = vi.fn()
    window.addEventListener(CASES_CHANGED, changed)

    vi.useFakeTimers()
    try {
      fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
      await tick(8000)
      expect(screen.getByRole('alert')).toHaveTextContent('already decided')
      expect(screen.getByRole('heading', { name: 'Block 1.2.3.4' })).toBeInTheDocument()
      expect(changed).toHaveBeenCalledTimes(1)
    } finally {
      window.removeEventListener(CASES_CHANGED, changed)
      vi.useRealTimers()
    }
  })

  it('keeps the fuse running when the case page is closed', async () => {
    testState.cases = [decCase]
    queue(need())
    vi.mocked(approvalsApi.approve).mockResolvedValue({} as never)
    const view = render(
      <MemoryRouter>
        <ToastProvider>
          <CasesDetail id="case-dec" onBack={vi.fn()} pageKey="cases" />
        </ToastProvider>
      </MemoryRouter>,
    )
    await screen.findByRole('heading', { name: 'Block 1.2.3.4' })

    vi.useFakeTimers()
    try {
      fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
      await tick(3000)
      view.rerender(
        <MemoryRouter>
          <ToastProvider>{null}</ToastProvider>
        </MemoryRouter>,
      )
      await tick(4999)
      expect(approvalsApi.approve).not.toHaveBeenCalled()
      await tick(1)
      expect(approvalsApi.approve).toHaveBeenCalledWith('act-1')
    } finally {
      vi.useRealTimers()
    }
  })

  it('titles the block with how long it has waited and drops empty rows', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-06-15T09:12:00Z'))
    try {
      testState.cases = [{ case_id: 'case-dec', title: 'Decision case', status: 'open', priority: 'high', finding_ids: [], created_at: '2026-06-15T09:14:00Z', combined_state: 'open', investigations: [] }]
      vi.mocked(approvalsApi.needsYou).mockResolvedValue({
        data: { count: 3, items: [need({ reason: '', kind: 'checkpoint' }), need({ source_id: 'a2', title: 'Two', kind: 'checkpoint', created_at: '2026-06-15T05:00:00Z' }), need({ source_id: 'a3', title: 'Three', kind: 'checkpoint', created_at: 'garbage' })] },
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

  it('gives a paused run the full block and collapses proposed actions into rows', async () => {
    testState.cases = [decCase]
    const proposals = ['Isolate WS-1', 'Isolate WS-2', 'Disable bob', 'Block 5.6.7.8', 'Disable eve'].map((title, i) =>
      need({ source_id: `p${i}`, title, reversibility: i === 1 ? 'irreversible' : 'reversible' }),
    )
    queue(...proposals, need({ source_id: 'cp', kind: 'checkpoint', title: 'Proceed to containment?' }))
    vi.mocked(approvalsApi.approve).mockResolvedValue({} as never)
    renderCase('case-dec')

    expect(await screen.findByRole('heading', { name: 'Proceed to containment?' })).toBeInTheDocument()
    expect(document.querySelectorAll('.case-needs article')).toHaveLength(1)
    const rows = screen.getByRole('list', { name: 'Proposed actions' })
    expect(within(rows).getAllByRole('listitem')).toHaveLength(3)
    expect(screen.getByText('+2 more proposed actions')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Show all' }))
    expect(within(rows).getAllByRole('listitem')).toHaveLength(5)
    expect(within(rows).getByRole('button', { name: /^Hold to approve Isolate WS-2\. Press and hold/ })).toBeInTheDocument()

    // a row approves through the same fuse and leaves at once
    vi.useFakeTimers()
    try {
      fireEvent.click(screen.getByRole('button', { name: 'Approve Disable bob' }))
      expect(screen.queryByText('Disable bob')).not.toBeInTheDocument()
      await tick(8000)
      expect(approvalsApi.approve).toHaveBeenCalledWith('p2')
    } finally {
      vi.useRealTimers()
    }

    fireEvent.click(screen.getByRole('tab', { name: /^Evidence/ }))
    expect(document.querySelector('.case-needs-strip')?.textContent).toContain('Proceed to containment?')
  })

  it('names how many proposals wait when no run is paused', async () => {
    testState.cases = [decCase]
    queue(need(), need({ source_id: 'a2', title: 'Isolate WS-1' }), need({ source_id: 'a3', title: 'Disable bob' }))
    renderCase('case-dec')

    expect(await screen.findByRole('heading', { name: 'Block 1.2.3.4' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'More proposed actions · 2' })).toBeInTheDocument()
    expect(document.querySelector('.case-reason')?.textContent).toBe('3 proposed actions')
    fireEvent.click(screen.getByRole('tab', { name: /^Evidence/ }))
    expect(document.querySelector('.case-needs-strip')?.textContent).toContain('3 proposed actions')
  })

  it('shows the error and renders nothing without items', async () => {
    testState.cases = [{ case_id: 'case-dec', title: 'Decision case', status: 'open', priority: 'high', finding_ids: [], created_at: '2026-06-15T09:14:00Z', combined_state: 'open', investigations: [] }]
    vi.mocked(approvalsApi.needsYou).mockResolvedValue({ data: { count: 0, items: [] } } as never)
    const { unmount } = renderCase('case-dec')
    expect(await screen.findByRole('heading', { name: 'Decision case' })).toBeInTheDocument()
    expect(document.querySelector('.case-needs')).toBeNull()
    unmount()

    vi.mocked(approvalsApi.needsYou).mockResolvedValue({ data: { count: 1, items: [need({ reversibility: 'irreversible' })] } } as never)
    vi.mocked(approvalsApi.approve).mockRejectedValueOnce({ response: { data: { detail: 'Already decided' } } })
    renderCase('case-dec')
    const hold = await screen.findByRole('button', { name: /Press and hold to confirm/ })
    fireEvent.pointerDown(hold)
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Already decided'), { timeout: 3000 })
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
      closure: { closure_category: 'false_positive', closed_by: 'ada', closed_by_kind: 'analyst', closed_at: null, verdict: 'the scanner' },
      investigations: [],
    }]
    vi.mocked(approvalsApi.needsYou).mockResolvedValue({
      data: { count: 1, items: [need({ title: 'Quarantine mailbox', case_id: 'case-closed' })] },
    } as never)
    renderCase('case-closed')

    expect(await screen.findByRole('heading', { name: 'Quarantine mailbox' })).toBeInTheDocument()
    const body = document.querySelector('.detail-body') as HTMLElement
    expect(body.textContent?.indexOf('Quarantine mailbox')).toBeLessThan(body.textContent?.indexOf('Closed by ada') ?? -1)
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
      expect(screen.getByText('case-9')).toBeInTheDocument()
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
      expect(within(head).getByText('Step 3 · 2 explanations · 1 evidence row so far')).toHaveClass('case-reason')
      expect(within(head).queryByText('who logged in')).not.toBeInTheDocument()
      expect(within(head).queryByText(/Combined from/)).not.toBeInTheDocument()
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

    it('leaves the workflow slot out of the header for a case with no run or SLA', async () => {
      vi.mocked(casesApi.getSLA).mockResolvedValueOnce({ data: null } as never)
      testState.cases = [open('case-9')]
      renderDetail('case-9')
      const head = ((await screen.findByRole('heading', { name: 'Frame case' })).closest('.detail-head')) as HTMLElement
      expect(within(head).queryByText('No workflow')).not.toBeInTheDocument()
      expect(within(head).getByText(/Resolve by —/)).toBeInTheDocument()
      expect(within(head).getByText('Idle')).toHaveClass('idle')
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

    it('drawer has Expand and Close; the full page has Close only', async () => {
      testState.cases = [open('case-9')]
      const onBack = vi.fn()
      const onExpand = vi.fn()
      const { unmount } = renderDetail('case-9', { onBack, onExpand })
      await screen.findByRole('heading', { name: 'Frame case' })
      fireEvent.click(screen.getByRole('button', { name: 'Expand' }))
      fireEvent.click(screen.getByRole('button', { name: 'Close' }))
      expect(onExpand).toHaveBeenCalledTimes(1)
      expect(onBack).toHaveBeenCalledTimes(1)
      fireEvent.keyDown(window, { key: 'Escape' }) // the drawer owns Escape
      expect(onBack).toHaveBeenCalledTimes(1)
      unmount()

      const onBackPage = vi.fn()
      renderDetail('case-9', { onBack: onBackPage })
      await screen.findByRole('heading', { name: 'Frame case' })
      expect(screen.queryByRole('button', { name: 'Expand' })).not.toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'Close' }))
      expect(onBackPage).toHaveBeenCalledTimes(1)
    })

    it('full page: Escape calls onBack, unless a menu is open, the key was handled, or a field has focus', async () => {
      testState.cases = [open('case-9')]
      const onBack = vi.fn()
      renderDetail('case-9', { onBack })
      await screen.findByRole('heading', { name: 'Frame case' })
      fireEvent.keyDown(document.body, { key: 'Escape' })
      expect(onBack).toHaveBeenCalledTimes(1)

      fireEvent.click(screen.getByRole('button', { name: 'Case actions' }))
      fireEvent.keyDown(document.body, { key: 'Escape' }) // closes only the menu
      expect(onBack).toHaveBeenCalledTimes(1)
      expect(screen.queryByRole('menu')).not.toBeInTheDocument()

      const box = document.createElement('textarea')
      document.body.appendChild(box)
      fireEvent.keyDown(box, { key: 'Escape' })
      box.remove()
      expect(onBack).toHaveBeenCalledTimes(1)
    })

    it('full page: a closed dialog that stays mounted (the Ask Vigil dock) does not stop Escape; an open one does', async () => {
      testState.cases = [open('case-9')]
      const onBack = vi.fn()
      renderDetail('case-9', { onBack })
      await screen.findByRole('heading', { name: 'Frame case' })
      const dock = document.createElement('aside')
      dock.setAttribute('role', 'dialog')
      dock.setAttribute('aria-hidden', 'true')
      document.body.appendChild(dock)
      fireEvent.keyDown(document.body, { key: 'Escape' })
      expect(onBack).toHaveBeenCalledTimes(1)

      dock.setAttribute('aria-hidden', 'false')
      fireEvent.keyDown(document.body, { key: 'Escape' })
      expect(onBack).toHaveBeenCalledTimes(1)
      dock.remove()
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

  describe('side panel', () => {
    const base = (over: Record<string, unknown> = {}) => ({
      case_id: 'case-9',
      title: 'Frame case',
      status: 'open',
      priority: 'high',
      assignee: 'ada lovelace',
      finding_ids: [],
      created_at: '2026-06-15T09:14:00Z',
      ...over,
    })
    beforeEach(() => {
      vi.mocked(casesApi.getComments).mockClear()
      vi.mocked(casesApi.getEscalations).mockClear()
    })
    const side = async () => within(await screen.findByRole('complementary', { name: 'Case details' }))
    const withBudget = (cost: number, max: number, health: string) => ({
      ...base(),
      investigations: [{ ...investigation('open', false, 'run-x'), cost_usd: cost, max_cost_usd: max, budget_health: health }],
    })

    it('writes details, a level-coloured cost meter and the recall cards', async () => {
      testState.cases = [withBudget(1, 2, 'warning')]
      testState.runs['run-x'] = {
        hunt: {
          ...HUNT,
          recall: {
            keys: ['host-1', 'ada'],
            verdicts: [{ investigation_kind: 'case', investigation_id: 'case-1', concluded_at: '2026-05-02T10:00:00Z', hypothesis_id: 'h1', statement: 'malicious', outcome: 'proven' }],
            sightings: [{ investigation_kind: 'hunt', investigation_id: 'hunt-1', concluded_at: '2026-05-02T10:00:00Z', entity_key: 'host-1', source_system: 'splunk', hit_count: 2 }],
            gaps: [{ investigation_kind: 'hunt', investigation_id: 'hunt-2', concluded_at: '2026-05-02T10:00:00Z', hypothesis_id: 'h2', statement: 'no EDR', disposition: 'no_evidence_gathered' }],
          },
        },
      }
      renderDetail('case-9')
      const panel = await side()
      expect(await panel.findByText('Incident response')).toBeInTheDocument()
      expect(panel.getByText('Limit').nextElementSibling).toHaveTextContent('$2.00')
      expect(await panel.findByText('host-1')).toHaveClass('side-ent-head')
      expect(panel.getByText('ada')).toHaveClass('side-ent-head')
      expect(panel.getByText('$1.00 of $2.00')).toBeInTheDocument()
      expect(panel.getByText('Fair')).toHaveClass('fair')
      const meter = panel.getByRole('meter')
      expect(meter).toHaveAttribute('aria-valuenow', '50')
      expect(meter.firstElementChild).toHaveClass('meter-fill', 'fair')
      expect(panel.getByText('proven — malicious').nextElementSibling).toHaveTextContent('Verdict')
      expect(panel.getByText('host-1 · splunk · 2 hits').nextElementSibling).toHaveTextContent('Sighting')
      expect(panel.getByText('no evidence gathered — no EDR').nextElementSibling).toHaveTextContent('Gap')
      expect(document.querySelector('.case-side .k')).toBeNull()
    })

    it('clamps an overspent bar and reads Poor', async () => {
      testState.cases = [withBudget(3, 2, 'critical')]
      renderDetail('case-9')
      const panel = await side()
      const meter = await panel.findByRole('meter')
      expect(meter).toHaveAttribute('aria-valuenow', '100')
      expect(meter.firstElementChild).toHaveStyle({ width: '100%' })
      expect(panel.getByText('Poor')).toBeInTheDocument()
    })

    it('shows Not measured, no bar and no Good for no run or a zero limit', async () => {
      testState.cases = [base()]
      const { unmount } = renderDetail('case-9')
      let panel = await side()
      expect(await panel.findByText('Not measured yet')).toBeInTheDocument()
      expect(panel.queryByRole('meter')).not.toBeInTheDocument()
      expect(panel.getByText('Limit').nextElementSibling).toHaveTextContent('—')
      unmount()

      testState.cases = [withBudget(0, 0, 'healthy')]
      renderDetail('case-9')
      panel = await side()
      expect(await panel.findByText('Not measured yet')).toBeInTheDocument()
      expect(panel.queryByRole('meter')).not.toBeInTheDocument()
      expect(panel.queryByText('Good')).not.toBeInTheDocument()
    })

    it('keeps the two empty recall copies', async () => {
      testState.cases = [withBudget(1, 2, 'healthy')]
      testState.runs['run-x'] = { hunt: HUNT }
      const { unmount } = renderDetail('case-9')
      expect(await (await side()).findByText('The run did not journal a recall.')).toBeInTheDocument()
      unmount()

      testState.runs['run-x'] = { hunt: { ...HUNT, recall: { unavailable: 'memory down' } } }
      renderDetail('case-9')
      expect(await (await side()).findByText('Recall did not happen: memory down')).toBeInTheDocument()
    })

    it('collapses People to the owner while comments load, then counts them once, and expands', async () => {
      testState.cases = [base()]
      let resolve: (v: unknown) => void = () => undefined
      vi.mocked(casesApi.getComments).mockReturnValueOnce(new Promise((r) => { resolve = r }) as never)
      vi.mocked(casesApi.getTasks).mockResolvedValueOnce({ data: { tasks: [{ task_id: 1, title: 'Isolate host', status: 'pending', priority: 'high' }] } } as never)
      renderDetail('case-9')
      const panel = await side()
      const toggle = await panel.findByRole('button', { name: /^People/ })
      expect(toggle).toHaveTextContent('People · ada lovelace')
      expect(toggle).not.toHaveTextContent('comment')
      expect(toggle).toHaveAttribute('aria-expanded', 'false')
      await act(async () => {
        resolve({ data: { comments: [
          { comment_id: 1, author: 'ada', content: 'first', created_at: '2026-06-15T09:00:00Z' },
          { comment_id: 2, author: 'bob', content: 'reply', created_at: '2026-06-15T09:05:00Z', parent_comment_id: 1 },
        ] } })
      })
      await waitFor(() => expect(toggle).toHaveTextContent('People · ada lovelace, 2 comments'))
      expect(panel.queryByText('first')).not.toBeInTheDocument()

      fireEvent.click(toggle)
      expect(toggle).toHaveAttribute('aria-expanded', 'true')
      expect(panel.getByText('AL')).toHaveClass('avatar')
      expect(await panel.findByText('Comments 2 · Tasks 1 · Tickets 0')).toBeInTheDocument()
      expect(panel.getByText('first')).toBeInTheDocument()
      expect(panel.getByText('Isolate host')).toBeInTheDocument()
      expect(panel.getByText('No linked tickets.')).toBeInTheDocument()
      // lifted: one fetch each, open or closed
      expect(casesApi.getEscalations).toHaveBeenCalledTimes(1)
    })

    it('says comments are unavailable on a failed read, and posting reloads them', async () => {
      testState.cases = [base({ assignee: null })]
      vi.mocked(casesApi.getComments).mockRejectedValueOnce(new Error('down'))
      renderDetail('case-9')
      const panel = await side()
      const toggle = await panel.findByRole('button', { name: /^People/ })
      await waitFor(() => expect(toggle).toHaveTextContent('People · unassigned, comments unavailable'))
      fireEvent.click(toggle)
      expect(await panel.findByText('Couldn’t load comments.')).toBeInTheDocument()
      expect(panel.getByText('Comments — · Tasks 0 · Tickets 0')).toBeInTheDocument()

      vi.mocked(casesApi.addComment).mockResolvedValueOnce({ data: {} } as never)
      fireEvent.change(panel.getByPlaceholderText('Write a comment…'), { target: { value: 'hello' } })
      fireEvent.click(panel.getByRole('button', { name: /Post/ }))
      await waitFor(() => expect(casesApi.getComments).toHaveBeenCalledTimes(2))
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
    const recalled = within(await screen.findByRole('region', { name: 'Recalled' }))
    // sighting + verdict + recall_entity call + declared gap + visibility gap
    expect(screen.getByRole('tab', { name: /Memory/ })).toHaveTextContent('5')

    expect(recalled.getByText('Asked about 10.0.0.7, m.kaur')).toBeInTheDocument()
    expect(recalled.getByText('10.0.0.7 · splunk · 4 hits')).toBeInTheDocument()
    expect(recalled.getByText('Hunt hunt-9 · May 2, 2026')).toBeInTheDocument()
    expect(recalled.getByText('proven — Loader family, finance laptops')).toBeInTheDocument()
    expect(recalled.getByText('Case case-4302 · May 2, 2026')).toBeInTheDocument()
    expect(recalled.getByText('what is 10.0.0.7')).toBeInTheDocument()
    expect(recalled.getByText('340 bytes')).toBeInTheDocument()
    // declared gaps live in Blind spots, not here
    expect(recalled.queryByText(/MFA push logs/)).toBeNull()

    const blind = within(screen.getByRole('region', { name: 'Blind spots' }))
    expect(blind.getByText('Declared')).toBeInTheDocument()
    expect(blind.getByText('no evidence gathered — MFA push logs never checked')).toBeInTheDocument()
    // the hunt's own account of what it could not see, under the source it lost
    expect(blind.getByText('Telemetry')).toBeInTheDocument()
    expect(blind.getByText('could not answer')).toBeInTheDocument()
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
    expect(within(screen.getByRole('region', { name: 'Recalled' })).getByText('10.0.0.7 · splunk · 4 hits')).toBeInTheDocument()
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
    expect(screen.getByText('who is m.kaur')).toBeInTheDocument()
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

describe('a case with several runs', () => {
  const ref = (workflow_id: string, run_id: string, created_at: string, primary = false) => ({
    ...investigation('completed', false, run_id), workflow_id, created_at, primary,
  })
  const ROOT_CAUSE = { run_id: 'run-rca', run_kind: 'root_cause', status: 'terminal', outcome: 'concluded', reason: '', cost_usd: 0.4, max_cost_usd: 2, steps: [{ step_id: 's1' }], open_checkpoint: null }
  const threeRuns = (id: string) => {
    testState.cases = [{
      ...openCase(id),
      combined_state: 'executing',
      investigations: [
        ref('incident-response', 'run-inv', '2026-06-15T10:00:00Z'),
        ref('root-cause-analysis', 'run-rca', '2026-06-15T11:00:00Z'),
        { ...ref('threat-hunt', 'run-hunt', '2026-06-15T09:00:00Z', true), status: 'executing', live: true },
      ],
    }]
    testState.runs['run-hunt'] = { status: 'running', hunt: { ...HUNT, status: 'running' } }
    testState.runs['run-inv'] = { status: 'completed', projection: { findings: [{ agent_id: 'triage', answer: 'benign login' }], cost_usd: 0.1 } }
    testState.runs['run-rca'] = { status: 'completed', projection: ROOT_CAUSE }
  }
  const HUNT_TAG = 'threat-hunt · started 09:00'
  const RCA_TAG = 'root-cause-analysis · started 11:00'
  const INV_TAG = 'Incident response · started 10:00'
  const tabs = ['Explanations', 'Evidence', 'Checked', 'Memory and blind spots']
  const hyp = (hypothesis_id: string, status: string, supports: number, weakens: number) =>
    ({ hypothesis_id, statement: `claim ${hypothesis_id}`, status, supports, weakens, neither: 0, resolution_reason: null, provenance: 'agent' })
  const evidence = (evidence_id: string, summary: string, captured_at: string, extra: object = {}) =>
    ({ evidence_id, iteration: 1, source_system: 'elastic', summary, is_gap: false, gap_detail: null, source_tier: 'telemetry', origins: [], bears_on: [], captured_at, ...extra })
  /** A hunt and the investigation it handed off to, each with a record. */
  const twoRuns = (id: string) => {
    testState.cases = [{
      ...openCase(id),
      investigations: [
        ref('threat-hunt', 'run-hunt', '2026-06-15T09:00:00Z', true),
        ref('incident-response', 'run-inv', '2026-06-15T10:00:00Z'),
      ],
    }]
    testState.runs['run-hunt'] = {
      status: 'completed',
      hunt: {
        ...HUNT,
        status: 'concluded',
        hypotheses: [hyp('h1', 'disproven', 0, 2), hyp('h2', 'proven', 3, 0)],
        evidence: [
          evidence('e-gap', 'the hunt’s empty query', '2026-06-15T09:30:00Z', { is_gap: true, provenance: 'tool_failure' }),
          evidence('e-old', 'hunt row', '2026-06-15T09:10:00Z'),
        ],
        evidence_count: 2,
      },
    }
    testState.runs['run-inv'] = {
      status: 'completed',
      projection: {
        ...investigateProjection,
        hypotheses: [hyp('h1', 'active', 2, 0), hyp('h3', 'proven', 4, 0)],
        evidence: [evidence('e-transfer', 'the investigation’s transfer row', '2026-06-15T10:20:00Z')],
        evidence_count: 1,
      },
    }
  }
  const rowText = (name: string) => (screen.getByText(name).closest('li') as HTMLElement).textContent

  it('lists the case as one: its runs on a filter, the primary first, and the honest line only when no run keeps a record', async () => {
    threeRuns('case-three')
    renderCase('case-three')
    const now = await screen.findByRole('region', { name: 'Now' })
    expect(await within(now).findByText('Now · step 3')).toBeInTheDocument()
    const others = screen.getByRole('region', { name: 'Other runs on this case' })
    await waitFor(() => expect(others).toHaveTextContent('Concluded'))
    expect(others).toHaveTextContent('$0.40 of $2.00')
    expect(within(others).getAllByRole('listitem')).toHaveLength(2)

    // the door counts what the tabs list across every run
    expect(screen.getByRole('tab', { name: /^Evidence/ })).toHaveTextContent('2')

    fireEvent.click(screen.getByRole('tab', { name: /Explanations/ }))
    const filter = await screen.findByRole('group', { name: 'Run' })
    expect(within(filter).getAllByRole('button').map((b) => b.textContent)).toEqual(['All runs', HUNT_TAG, RCA_TAG, INV_TAG])
    // one card, the hunt's rows tagged; the two runs with no record add no line of their own
    expect(await screen.findByText('The host is owned')).toBeInTheDocument()
    expect(document.querySelectorAll('.case-expl-card')).toHaveLength(1)
    expect(rowText('The host is owned')).toContain(HUNT_TAG)
    expect(screen.queryByText('This workflow does not test explanations yet.')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: /^Evidence/ }))
    expect(await screen.findByText('no login')).toBeInTheDocument()
    expect(screen.getByText('benign login')).toBeInTheDocument()
    expect(document.querySelectorAll('.ev-trail')).toHaveLength(1)

    fireEvent.click(screen.getByRole('tab', { name: /Checked/ }))
    expect(await screen.findByText(/What Vigil checked · 2 queries/)).toBeInTheDocument()
  })

  describe('How it started', () => {
    const chain = (n: number) => Array.from({ length: n }, (_, i) => ({
      step_id: `s${i}`, event: `event ${i}`, who: `host-${i}`, at: `2026-06-15T10:0${i}:00Z`, link: '', artifact: '',
      cause_id: i === 0 ? null : `s${i - 1}`, origin: i === 0, proven: i < 3,
    })).reverse()
    const withChain = (id: string, steps: unknown[], proven = 3) => {
      threeRuns(id)
      testState.runs['run-rca'] = { status: 'completed', projection: { ...ROOT_CAUSE, steps, proven } }
    }

    it('names the origin, counts the proof, shows the first and last links and links the run', async () => {
      withChain('case-how', chain(5))
      renderCase('case-how')
      const card = await screen.findByRole('region', { name: 'How it started' })
      expect(await within(card).findByText('5 events · 3 proven')).toBeInTheDocument()
      expect(within(card).getAllByText('event 0')).toHaveLength(2)
      expect(within(card).getByText('event 4')).toBeInTheDocument()
      expect(within(card).queryByText('event 2')).not.toBeInTheDocument()
      expect(within(card).getAllByRole('listitem')).toHaveLength(2)
      expect(within(card).getByRole('link', { name: 'Show all 5' })).toHaveAttribute('href', '/workflows?run=run-rca')
    })

    it('shows every row of a two-step chain and no Show all', async () => {
      withChain('case-how2', chain(2), 2)
      renderCase('case-how2')
      const card = await screen.findByRole('region', { name: 'How it started' })
      expect(within(card).getAllByRole('listitem')).toHaveLength(2)
      expect(within(card).queryByText(/Show all/)).not.toBeInTheDocument()
    })

    it('is absent with no root-cause run or no steps', async () => {
      twoRuns('case-how-none')
      renderCase('case-how-none')
      await screen.findByRole('region', { name: 'Other runs on this case' })
      expect(screen.queryByRole('region', { name: 'How it started' })).not.toBeInTheDocument()
      cleanup()
      withChain('case-how-empty', [], 0)
      renderCase('case-how-empty')
      await waitFor(() => expect(screen.getByRole('region', { name: 'Other runs on this case' })).toHaveTextContent('Concluded'))
      expect(screen.queryByRole('region', { name: 'How it started' })).not.toBeInTheDocument()
    })

    it('says one line when the root-cause run cannot be read', async () => {
      withChain('case-how-err', chain(3))
      const read = (runId: string) => Promise.resolve({ data: { run_id: runId, ...(testState.runs[runId] as object) } } as never)
      vi.mocked(workflowApi.getRun).mockImplementation((runId: string) => (runId === 'run-rca' ? Promise.reject(new Error('down')) : read(runId)))
      onTestFinished(() => void vi.mocked(workflowApi.getRun).mockImplementation(read))
      renderCase('case-how-err')
      const card = await screen.findByRole('region', { name: 'How it started' })
      expect(card).toHaveTextContent('The run could not be read.')
    })

    it('shows on the closed mood too', async () => {
      withChain('case-how-closed', chain(5))
      testState.cases = [{ ...testState.cases[0], status: 'closed', combined_state: 'closed', closure: { closed_by: 'ada', closed_at: '2026-06-15T15:48:00Z', verdict: 'the scanner' } }]
      renderCase('case-how-closed')
      expect(await screen.findByRole('region', { name: 'Closed summary' })).toBeInTheDocument()
      expect(await screen.findByRole('link', { name: 'Show all 5' })).toBeInTheDocument()
    })
  })

  it('says the honest line once when no run keeps a record, and a run that cannot be read under its own tag', async () => {
    threeRuns('case-broken')
    testState.runs['run-hunt'] = { status: 'completed', projection: { findings: [], cost_usd: 0 } }
    vi.mocked(workflowApi.getRun).mockImplementation((runId: string) =>
      runId === 'run-inv' ? Promise.reject(new Error('down')) : Promise.resolve({ data: { run_id: runId, ...(testState.runs[runId] as object) } } as never),
    )
    renderCase('case-broken')
    fireEvent.click(await screen.findByRole('tab', { name: /Explanations/ }))
    const line = await screen.findByText(INV_TAG, { selector: '.case-run-line .rec-run' })
    expect(line.closest('p')).toHaveTextContent('The run could not be read.')
    // a run that cannot be read might keep a record, so the honest line waits for it
    expect(screen.queryByText(/does not test explanations yet/)).not.toBeInTheDocument()
    vi.mocked(workflowApi.getRun).mockImplementation((runId: string) => Promise.resolve({ data: { run_id: runId, ...(testState.runs[runId] as object) } } as never))
  })

  it('shows one Explanations list across runs, each row tagged, by standing word then newest run', async () => {
    twoRuns('case-merged')
    renderCase('case-merged')
    fireEvent.click(await screen.findByRole('tab', { name: /Explanations/ }))
    expect(await screen.findByText('claim h3')).toBeInTheDocument()
    await waitFor(() => expect(document.querySelectorAll('.case-expl-rows li')).toHaveLength(4))
    expect(document.querySelectorAll('.case-expl-card')).toHaveLength(1)
    // proven first (newest run's first), then standing, then ruled out; the same id in two runs is two rows
    const rows = Array.from(document.querySelectorAll('.case-expl-rows li')).map((li) => li.textContent as string)
    expect(rows.map((text) => /claim (h\d)/.exec(text)?.[1])).toEqual(['h3', 'h2', 'h1', 'h1'])
    expect(rows[0]).toContain('Proven')
    expect(rows[0]).toContain(INV_TAG)
    expect(rows[1]).toContain(HUNT_TAG)
    expect(rows[2]).toContain('Standing')
    expect(rows[2]).toContain(INV_TAG)
    expect(rows[3]).toContain('Ruled out')
    expect(rows[3]).toContain(HUNT_TAG)
  })

  it('narrows the four tabs to one run on the filter and leaves the tab counts alone', async () => {
    twoRuns('case-filter')
    renderCase('case-filter')
    fireEvent.click(await screen.findByRole('tab', { name: /Explanations/ }))
    await waitFor(() => expect(document.querySelectorAll('.case-expl-rows li')).toHaveLength(4))
    const counts = () => tabs.map((name) => screen.getByRole('tab', { name: new RegExp(`^${name}`) }).textContent)
    const before = counts()

    fireEvent.click(within(screen.getByRole('group', { name: 'Run' })).getByRole('button', { name: HUNT_TAG }))
    await waitFor(() => expect(document.querySelectorAll('.case-expl-rows li')).toHaveLength(2))
    expect(screen.queryByText('claim h3')).not.toBeInTheDocument()
    expect(counts()).toEqual(before)

    // the choice is one for every tab
    fireEvent.click(screen.getByRole('tab', { name: /^Evidence/ }))
    expect(await screen.findByText('hunt row')).toBeInTheDocument()
    expect(screen.queryByText('the investigation’s transfer row')).not.toBeInTheDocument()
    expect(within(screen.getByRole('group', { name: 'Run' })).getByRole('button', { name: HUNT_TAG })).toHaveAttribute('aria-pressed', 'true')

    fireEvent.click(within(screen.getByRole('group', { name: 'Run' })).getByRole('button', { name: 'All runs' }))
    expect(await screen.findByText('the investigation’s transfer row')).toBeInTheDocument()
  })

  it('merges a gap row from the hunt and a row from the investigation into one Evidence table, newest first', async () => {
    twoRuns('case-evidence')
    renderCase('case-evidence')
    fireEvent.click(await screen.findByRole('tab', { name: /^Evidence/ }))
    expect(await screen.findByText('the investigation’s transfer row')).toBeInTheDocument()
    await waitFor(() => expect(document.querySelectorAll('.ev-row')).toHaveLength(3))
    expect(document.querySelectorAll('.ev-trail')).toHaveLength(1)
    const rows = Array.from(document.querySelectorAll('.ev-row')).map((row) => row.textContent as string)
    expect(rows[0]).toContain('the investigation’s transfer row')
    expect(rows[0]).toContain(INV_TAG)
    expect(rows[1]).toContain('the hunt’s empty query (gap)')
    expect(rows[1]).toContain(HUNT_TAG)
    expect(rows[2]).toContain('hunt row')
    expect(screen.getByText(/Evidence trail · 3 rows/)).toBeInTheDocument()
    // no stance tally added up across two runs
    expect(screen.queryByText(/support ·/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'About the totals' })).not.toBeInTheDocument()
  })

  it('keeps the rows on screen while "Show older" asks the hunts that hold more', async () => {
    twoRuns('case-older')
    const many = Array.from({ length: 50 }, (_, i) => evidence(`n${i}`, `row ${i}`, new Date(Date.UTC(2026, 5, 15, 9, 0, 0) - i * 1000).toISOString()))
    testState.runs['run-hunt'] = { status: 'completed', hunt: { ...(testState.runs['run-hunt'] as { hunt: object }).hunt, evidence: many, evidence_count: 60 } }
    const inv = testState.runs['run-inv'] as { projection: { evidence: unknown[] } }
    inv.projection.evidence.push(evidence('e-older', 'an older investigation row', '2026-06-15T08:00:00Z'))
    renderCase('case-older')
    fireEvent.click(await screen.findByRole('tab', { name: /^Evidence/ }))
    expect(await screen.findByText('row 48')).toBeInTheDocument()
    expect(screen.queryByText('row 49')).not.toBeInTheDocument()
    expect(screen.getByText(/showing 50 of 61/)).toBeInTheDocument()
    // the hunt holds rows newer than this one that it has not listed, so it waits
    expect(screen.queryByText('an older investigation row')).not.toBeInTheDocument()

    let release: () => void = () => undefined
    vi.mocked(workflowApi.getRun).mockImplementation((runId: string, limit?: number) =>
      limit
        ? new Promise((resolve) => {
            release = () => resolve({ data: { run_id: runId, ...(testState.runs[runId] as object) } } as never)
          })
        : Promise.resolve({ data: { run_id: runId, ...(testState.runs[runId] as object) } } as never),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Show older' }))
    // the rows already read show at once, and the hunt is asked for a larger page
    expect(await screen.findByText('row 49')).toBeInTheDocument()
    await waitFor(() => expect(vi.mocked(workflowApi.getRun)).toHaveBeenCalledWith('run-hunt', 100))
    expect(screen.getByRole('button', { name: 'Loading older rows…' })).toBeDisabled()
    expect(screen.getByText('row 49')).toBeInTheDocument()
    expect(document.querySelectorAll('.ev-row').length).toBeGreaterThan(50)
    release()
    vi.mocked(workflowApi.getRun).mockImplementation((runId: string) => Promise.resolve({ data: { run_id: runId, ...(testState.runs[runId] as object) } } as never))
  })

  it('says how many runs are unpriced beside the total, on the Checked door and header, never as $0', async () => {
    threeRuns('case-unpriced')
    testState.runs['run-inv'] = { status: 'completed', projection: { findings: [{ agent_id: 'triage', answer: 'benign login' }] } }
    renderCase('case-unpriced')
    const door = within(await screen.findByRole('region', { name: 'Audit doors' })).getByRole('button', { name: /^Checked/ })
    await waitFor(() => expect(door).toHaveTextContent('$0.60 · 1 run unpriced'))
    fireEvent.click(screen.getByRole('tab', { name: /Checked/ }))
    const card = (await screen.findByText(/What Vigil checked/)).closest('section') as HTMLElement
    expect(card.querySelector('h3')).toHaveTextContent('What Vigil checked · 2 queries · $0.60 · 1 run unpriced')
  })

  it('counts a run it could not read as unpriced, on the Checked door and header', async () => {
    threeRuns('case-unread')
    vi.mocked(workflowApi.getRun).mockImplementation((runId: string) =>
      runId === 'run-inv' ? Promise.reject(new Error('down')) : Promise.resolve({ data: { run_id: runId, ...(testState.runs[runId] as object) } } as never),
    )
    renderCase('case-unread')
    const door = within(await screen.findByRole('region', { name: 'Audit doors' })).getByRole('button', { name: /^Checked/ })
    await waitFor(() => expect(door).toHaveTextContent('$0.60 · 1 run unpriced'))
    fireEvent.click(screen.getByRole('tab', { name: /Checked/ }))
    const card = (await screen.findByText(/What Vigil checked/)).closest('section') as HTMLElement
    expect(card.querySelector('h3')).toHaveTextContent('What Vigil checked · 2 queries · $0.60 · 1 run unpriced')
    vi.mocked(workflowApi.getRun).mockImplementation((runId: string) => Promise.resolve({ data: { run_id: runId, ...(testState.runs[runId] as object) } } as never))
  })

  it('still counts the unpriced runs when none is priced', async () => {
    threeRuns('case-none-priced')
    testState.runs['run-inv'] = { status: 'completed', projection: { findings: [{ agent_id: 'triage', answer: 'benign login' }] } }
    testState.runs['run-rca'] = { status: 'completed', projection: { ...ROOT_CAUSE, cost_usd: null } }
    testState.runs['run-hunt'] = { status: 'running', hunt: { ...HUNT, status: 'running', cost_usd: null } }
    renderCase('case-none-priced')
    const door = within(await screen.findByRole('region', { name: 'Audit doors' })).getByRole('button', { name: /^Checked/ })
    await waitFor(() => expect(door).toHaveTextContent('3 runs unpriced'))
    expect(door).not.toHaveTextContent('$')
  })

  it('adds no unpriced note when every run is priced', async () => {
    threeRuns('case-all-priced')
    renderCase('case-all-priced')
    const door = within(await screen.findByRole('region', { name: 'Audit doors' })).getByRole('button', { name: /^Checked/ })
    await waitFor(() => expect(door).toHaveTextContent('$0.70'))
    expect(door).not.toHaveTextContent('unpriced')
    fireEvent.click(screen.getByRole('tab', { name: /Checked/ }))
    const card = (await screen.findByText(/What Vigil checked/)).closest('section') as HTMLElement
    expect(card.querySelector('h3')).toHaveTextContent('What Vigil checked · 2 queries · $0.70')
    expect(card.querySelector('h3')).not.toHaveTextContent('unpriced')
  })

  it('offers "Show older" again across runs when the hunt\'s larger read fails, and reads again on the next press', async () => {
    twoRuns('case-older-fail-many')
    const many = Array.from({ length: 50 }, (_, i) => evidence(`n${i}`, `row ${i}`, new Date(Date.UTC(2026, 5, 15, 9, 0, 0) - i * 1000).toISOString()))
    testState.runs['run-hunt'] = { status: 'completed', hunt: { ...(testState.runs['run-hunt'] as { hunt: object }).hunt, evidence: many, evidence_count: 60 } }
    renderCase('case-older-fail-many')
    fireEvent.click(await screen.findByRole('tab', { name: /^Evidence/ }))
    expect(await screen.findByText('row 48')).toBeInTheDocument()
    const ok = (runId: string) => Promise.resolve({ data: { run_id: runId, ...(testState.runs[runId] as object) } } as never)
    vi.mocked(workflowApi.getRun).mockImplementation((runId: string, limit?: number) => (limit === 100 ? Promise.reject(new Error('down')) : ok(runId)))
    try {
      vi.mocked(workflowApi.getRun).mockClear()
      fireEvent.click(screen.getByRole('button', { name: 'Show older' }))
      await waitFor(() => expect(workflowApi.getRun).toHaveBeenCalledWith('run-hunt', 100))
      // the read failed and the hunt keeps what it showed, so the button comes back rather than spinning
      const button = await screen.findByRole('button', { name: 'Show older' })
      expect(button).toBeEnabled()
      expect(screen.getByText('row 49')).toBeInTheDocument()
      vi.mocked(workflowApi.getRun).mockClear()
      fireEvent.click(button)
      await waitFor(() => expect(workflowApi.getRun).toHaveBeenCalledWith('run-hunt', 100))
      expect(await screen.findByRole('button', { name: 'Show older' })).toBeEnabled()
    } finally {
      vi.mocked(workflowApi.getRun).mockImplementation(ok)
    }
  })

  it('tags Checked and Memory rows with their run, and opens a call in its own run', async () => {
    twoRuns('case-checked')
    testState.runs['run-hunt'] = {
      status: 'completed',
      hunt: { ...(testState.runs['run-hunt'] as { hunt: object }).hunt, calls: [{ ref: 'c1', question: 'who logged in', tool: 'search', result_length: 12, cost_usd: 0.01, iteration: 1 }] },
    }
    testState.runs['run-inv'] = {
      status: 'completed',
      projection: { ...(testState.runs['run-inv'] as { projection: object }).projection, calls: [{ ref: 'c9', question: 'read the proxy', tool: 'proxy_query', result_length: 4, cost_usd: 0.02, iteration: 1 }] },
    }
    vi.mocked(workflowApi.getCall).mockResolvedValue({ data: { ref: 'c9', tool: 'proxy_query', arguments: '{}', result: 'ok', ok: true } } as never)
    renderCase('case-checked')
    fireEvent.click(await screen.findByRole('tab', { name: /Checked/ }))
    expect(await screen.findByText(/What Vigil checked · 2 queries/)).toBeInTheDocument()
    expect(document.querySelectorAll('.case-checked')).toHaveLength(1)
    const turn = (text: string) => screen.getByText(text).closest('.checked-turn') as HTMLElement
    expect(turn('who logged in')).toHaveTextContent(HUNT_TAG)
    expect(turn('read the proxy')).toHaveTextContent(INV_TAG)
    fireEvent.click(within(turn('read the proxy')).getByRole('button', { name: /c9/ }))
    await waitFor(() => expect(vi.mocked(workflowApi.getCall)).toHaveBeenCalledWith('run-inv', 'c9'))
  })

  it('shows no run tag, filter or other runs on a case with one run', async () => {
    testState.cases = [{ ...openCase('case-one'), investigations: [investigation('executing', true, 'run-one')] }]
    testState.runs['run-one'] = { status: 'running', hunt: { ...HUNT, status: 'running' } }
    renderCase('case-one')
    expect(await screen.findByText('Now · step 3')).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Other runs on this case' })).not.toBeInTheDocument()
    for (const name of [/Explanations/, /^Evidence/, /Checked/, /Memory and blind spots/]) {
      fireEvent.click(screen.getByRole('tab', { name }))
      await waitFor(() => expect(document.querySelector('.case-expl-card, .ev-trail, .case-checked, .case-mem-card, .empty-state')).not.toBeNull())
      expect(screen.queryByRole('group', { name: 'Run' })).not.toBeInTheDocument()
      expect(document.querySelector('.rec-run')).toBeNull()
    }
  })
})

describe('the case verdict', () => {
  const ref = (run_id: string, created_at: string, primary: boolean, workflow_id = 'threat-hunt') => ({
    ...investigation('completed', false, run_id), workflow_id, created_at, primary,
  })
  const hunt = (disposition: string | null) => ({ status: 'completed', hunt: { ...HUNT, status: 'concluded', outcome: 'concluded', disposition } })
  const lead = (disposition: string | null, nextSteps: string[] = []) => ({
    status: 'completed',
    projection: { disposition, outcome: 'concluded', findings: [{ agent_id: 'agent', answer: { summary: 'the account', disposition, next_steps: nextSteps } }], cost_usd: 0.1 },
  })
  const openWith = (id: string, investigations: unknown[], runs: Record<string, unknown>, extra: Record<string, unknown> = {}) => {
    testState.cases = [{ ...openCase(id), combined_state: 'open', investigations, ...extra }]
    Object.assign(testState.runs, runs)
    renderCase(id)
  }

  it('reads needs review, naming the older hunt, when a newer rerun dismisses what it found', async () => {
    openWith('case-rerun', [ref('run-old', '2026-06-15T09:00:00Z', true), ref('run-new', '2026-06-15T10:00:00Z', false)], { 'run-old': hunt('malicious'), 'run-new': hunt('benign') })
    const verdict = await screen.findByRole('region', { name: 'Case verdict' })
    expect(verdict).toHaveTextContent('Needs review')
    expect(verdict).toHaveTextContent(/^Runs sayNeeds reviewthreat-hunt · started 09:00 says malicious\.$/)
    expect(within(screen.getByRole('region', { name: 'Other runs on this case' })).getByText('Benign')).toBeInTheDocument()
  })

  it('holds back every verdict while a run is still loading', async () => {
    vi.mocked(workflowApi.getRun).mockImplementationOnce(() => new Promise(() => {}))
    openWith('case-loading', [ref('run-a', '2026-06-15T09:00:00Z', true), ref('run-b', '2026-06-15T10:00:00Z', false)], { 'run-a': hunt('benign'), 'run-b': hunt('benign') })
    await screen.findByRole('region', { name: 'Other runs on this case' })
    expect(screen.queryByRole('region', { name: 'Case verdict' })).not.toBeInTheDocument()
  })

  it('does not let a dismissal stand over a run it could not read', async () => {
    vi.mocked(workflowApi.getRun).mockImplementation((runId: string) =>
      runId === 'run-bad' ? Promise.reject(new Error('down')) : Promise.resolve({ data: { run_id: runId, ...(testState.runs[runId] as object) } } as never),
    )
    openWith('case-unread', [ref('run-ok', '2026-06-15T10:00:00Z', true), ref('run-bad', '2026-06-15T09:00:00Z', false)], { 'run-ok': hunt('benign') })
    const verdict = await screen.findByRole('region', { name: 'Case verdict' })
    expect(verdict).toHaveTextContent('Needs review')
    expect(verdict).toHaveTextContent('threat-hunt · started 09:00 could not be read.')
    vi.mocked(workflowApi.getRun).mockImplementation((runId: string) => Promise.resolve({ data: { run_id: runId, ...(testState.runs[runId] as object) } } as never))
  })

  it('shows the lone run’s word with no basis, and Do now for an investigation but not a hunt', async () => {
    openWith('case-one-inv', [ref('run-one', '2026-06-15T09:00:00Z', true, 'incident-response')], { 'run-one': lead('suspicious', ['Isolate the host']) })
    const verdict = await screen.findByRole('region', { name: 'Case verdict' })
    expect(verdict).toHaveTextContent('Suspicious')
    expect(within(verdict).getByText('Do now')).toBeInTheDocument()
    expect(within(verdict).getByText('Isolate the host')).toBeInTheDocument()
    cleanup()
    openWith('case-one-hunt', [ref('run-h', '2026-06-15T09:00:00Z', true)], { 'run-h': hunt('suspicious') })
    const hunted = await screen.findByRole('region', { name: 'Case verdict' })
    expect(hunted).toHaveTextContent('Suspicious')
    expect(within(hunted).queryByText('Do now')).not.toBeInTheDocument()
  })

  it('adds nothing when no run says a word', async () => {
    openWith('case-silent', [ref('run-s', '2026-06-15T09:00:00Z', true)], { 'run-s': hunt(null) })
    await screen.findByText(/Open case/)
    await waitFor(() => expect(vi.mocked(workflowApi.getRun).mock.calls.some(([id]) => id === 'run-s')).toBe(true))
    expect(screen.queryByRole('region', { name: 'Case verdict' })).not.toBeInTheDocument()
  })

  it('flags a false-positive closure over a malicious run, under the closure line', async () => {
    openWith('case-flag', [ref('run-m', '2026-06-15T09:00:00Z', true, 'incident-response')], { 'run-m': lead('malicious', ['Contain the host']) }, {
      status: 'closed',
      combined_state: 'closed',
      closure: { closure_category: 'false_positive', closed_by: 'ada', closed_by_kind: 'analyst', closed_at: '2026-06-15T15:48:00Z', verdict: 'the scanner' },
    })
    const closed = await screen.findByRole('region', { name: 'Closed summary' })
    expect(await within(closed).findByText('Malicious')).toBeInTheDocument()
    expect(within(closed).getByText('Contain the host')).toBeInTheDocument()
  })

  it('does not flag a closure the runs agree with', async () => {
    openWith('case-agree', [ref('run-b', '2026-06-15T09:00:00Z', true, 'incident-response')], { 'run-b': lead('benign') }, {
      status: 'closed',
      combined_state: 'closed',
      closure: { closure_category: 'false_positive', closed_by: 'ada', closed_by_kind: 'analyst', closed_at: '2026-06-15T15:48:00Z', verdict: 'the scanner' },
    })
    const closed = await screen.findByRole('region', { name: 'Closed summary' })
    await waitFor(() => expect(vi.mocked(workflowApi.getRun).mock.calls.some(([id]) => id === 'run-b')).toBe(true))
    expect(within(closed).queryByText('Runs say')).not.toBeInTheDocument()
  })

  it('lets a benign investigation stand over an inconclusive hunt, open and closed as false positive', async () => {
    const refs = [ref('run-h', '2026-06-15T09:00:00Z', true), ref('run-i', '2026-06-15T10:00:00Z', false, 'incident-response')]
    const runs = { 'run-h': hunt('inconclusive'), 'run-i': lead('benign') }
    openWith('case-handoff', refs, runs)
    const verdict = await screen.findByRole('region', { name: 'Case verdict' })
    expect(verdict).toHaveTextContent(/^Runs sayBenign$/)
    cleanup()
    openWith('case-handoff-closed', refs, runs, {
      status: 'closed',
      combined_state: 'closed',
      closure: { closure_category: 'false_positive', closed_by: 'ada', closed_by_kind: 'analyst', closed_at: '2026-06-15T15:48:00Z', verdict: 'the scanner' },
    })
    const closed = await screen.findByRole('region', { name: 'Closed summary' })
    await waitFor(() => expect(vi.mocked(workflowApi.getRun).mock.calls.some(([id]) => id === 'run-i')).toBe(true))
    expect(within(closed).queryByText('Runs say')).not.toBeInTheDocument()
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
    expect(screen.queryByRole('table', { name: 'Agents' })).not.toBeInTheDocument()
    expect(screen.queryByText('Agents on this case, right now')).not.toBeInTheDocument()

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

  it('says handed off in Findings so far, as a word beside the explanation rather than a list bullet', async () => {
    open('case-words', live({ hunt: { ...HUNT, hypotheses: [{ ...HUNT.hypotheses[1], status: 'handed_off' }] } }))
    const row = (await screen.findByText('Still forming')).closest('li') as HTMLElement
    expect(row.closest('ul')).toHaveClass('case-findings')
    expect(within(row).getByText('Proven')).toHaveClass('case-expl-pill')
    expect(within(row).getByText('handed off')).not.toHaveClass('case-expl-pill')
  })
})

describe('watching a run, its cost and its agents', () => {
  const hunt = (over: Record<string, unknown> = {}) => ({ ...HUNT, status: 'active', ...over })
  const open = (id: string, run: unknown, ref: Record<string, unknown> = {}, props: Parameters<typeof renderDetail>[1] = {}) => {
    testState.cases = [{ ...openCase(id), combined_state: 'executing', investigations: [{ ...investigation('executing', true, `run-${id}`), cost_usd: 0, max_cost_usd: 0, ...ref }] }]
    testState.runs[`run-${id}`] = run
    return renderDetail(id, props)
  }
  const side = async () => within(await screen.findByRole('complementary', { name: 'Case details' }))

  it('links the Now card and the stopped card to Watch a run, and closes the drawer', async () => {
    const onBack = vi.fn()
    open('case-w', { status: 'running', hunt: hunt() }, {}, { onBack, onExpand: vi.fn() })
    const link = await within(await screen.findByRole('region', { name: 'Now' })).findByRole('link', { name: 'Watch it run' })
    expect(link).toHaveAttribute('href', '/workflows?run=run-case-w')
    fireEvent.click(link)
    expect(onBack).toHaveBeenCalled()
    cleanup()

    open('case-w2', { status: 'running', hunt: hunt({ status: 'parked', reason: 'budget' }) })
    const stopped = await screen.findByRole('region', { name: 'Run state' })
    expect(within(stopped).getByRole('link', { name: 'Watch it run' })).toHaveAttribute('href', '/workflows?run=run-case-w2')
  })

  it('offers no Watch link without a run', async () => {
    testState.cases = [openCase('case-none')]
    renderDetail('case-none')
    await screen.findByText('No live investigation.')
    expect(screen.queryByRole('link', { name: 'Watch it run' })).not.toBeInTheDocument()
  })

  it('shows a hunt’s live cost against the cap off the run, and keeps it once parked', async () => {
    open('case-c', { status: 'running', hunt: hunt({ cost_usd: 1.4989, budgets: { max_iterations: 10, max_cost_usd: 5 } }) })
    const panel = await side()
    expect(await panel.findByText('$1.50 of $5.00')).toBeInTheDocument()
    expect(panel.getByText('Good')).toBeInTheDocument()
    expect(panel.getByText('Limit').nextElementSibling).toHaveTextContent('$5.00')
    expect(panel.queryByText('Not measured yet')).not.toBeInTheDocument()
    // The header's trust strip is a different thing and stays.
    expect(within(document.querySelector('.dh-meta') as HTMLElement).getByText(/Not measured yet/)).toBeInTheDocument()
    cleanup()

    open('case-c2', { status: 'running', hunt: hunt({ status: 'parked', cost_usd: 4.8, budgets: { max_iterations: 10, max_cost_usd: 5 } }) })
    const parked = await side()
    expect(await parked.findByText('$4.80 of $5.00')).toBeInTheDocument()
    expect(parked.getByText('Poor')).toBeInTheDocument()
  })

  it('shows the cost alone when no cap is known, and Not measured only when no cost is', async () => {
    open('case-c3', { status: 'running', hunt: hunt({ cost_usd: 0.4 }) })
    const panel = await side()
    expect(await panel.findByText('$0.40')).toBeInTheDocument()
    expect(panel.queryByRole('meter')).not.toBeInTheDocument()
    expect(panel.queryByText('Good')).not.toBeInTheDocument()
    cleanup()

    open('case-c4', { status: 'running', hunt: hunt({ cost_usd: 0 }) })
    expect(await (await side()).findByText('Not measured yet')).toBeInTheDocument()
  })

  it('shows a lead run’s cost against the cap off its projection, and never past full', async () => {
    const lead = (cost: number, cap: number) => ({
      status: 'completed',
      projection: { iterations: 2, decisions: [], findings: [], calls: [], gaps: [], recall: null, outcome: 'completed', reason: 'done', cost_usd: cost, max_cost_usd: cap },
    })
    open('case-l1', lead(2.1, 15), { workflow_id: 'root-cause-analysis', live: false, cost_usd: 2.1 })
    const panel = await side()
    expect(await panel.findByText('$2.10 of $15.00')).toBeInTheDocument()
    expect(panel.getByText('Good')).toBeInTheDocument()
    expect(panel.getByText('Limit').nextElementSibling).toHaveTextContent('$15.00')
    cleanup()

    open('case-l2', lead(20, 15), { workflow_id: 'root-cause-analysis', live: false, cost_usd: 20 })
    const over = await side()
    expect(await over.findByText('$20.00 of $15.00')).toBeInTheDocument()
    expect(over.getByRole('meter')).toHaveAttribute('aria-valuenow', '100')
    cleanup()

    open('case-l3', lead(2.1, 0), { workflow_id: 'root-cause-analysis', live: false, cost_usd: 2.1 })
    const none = await side()
    expect(await none.findByText('$2.10')).toBeInTheDocument()
    expect(none.queryByRole('meter')).not.toBeInTheDocument()
  })

  it('lists agents only while the run is in flight', async () => {
    open('case-a', { status: 'running', hunt: hunt() })
    const table = await screen.findByRole('table', { name: 'Agents' })
    expect(within(table).getAllByRole('row')).toHaveLength(2)
    cleanup()

    open('case-a2', { status: 'running', hunt: hunt({ status: 'parked', reason: 'budget' }) })
    await screen.findByRole('region', { name: 'Run state' })
    expect(screen.queryByRole('table', { name: 'Agents' })).not.toBeInTheDocument()
    cleanup()

    open('case-a3', { status: 'completed', hunt: hunt({ outcome: 'completed' }) })
    await waitFor(() => expect(screen.getByText('No live investigation.')).toBeInTheDocument())
    expect(screen.queryByRole('table', { name: 'Agents' })).not.toBeInTheDocument()
  })

  it('says a lead’s decision in plain words in the Now card, the title line and the agent rows', async () => {
    open('case-words', {
      status: 'running',
      hunt: hunt({ moves: [
        { query_intent: '', action: 'ABANDON', worker_agent_id: 'lead', iteration: 2, created_at: '2026-06-15T09:10:00Z' },
        { query_intent: 'who logged in', action: 'INVESTIGATE', worker_agent_id: 'worker', iteration: 1, created_at: '2026-06-15T09:05:00Z' },
      ] }),
    })
    const now = await screen.findByRole('region', { name: 'Now' })
    expect(await within(now).findByText('Rule out an explanation')).toBeInTheDocument()
    expect(document.querySelector('.case-reason')).toHaveTextContent(/^Step \d+ · /)
    const rows = within(screen.getByRole('table', { name: 'Agents' })).getAllByRole('row')
    expect(rows[1]).toHaveTextContent('who logged in')
    expect(screen.queryByText('ABANDON')).not.toBeInTheDocument()
  })
})

describe('a kernel hunt on its case', () => {
  const run = (hunt: Record<string, unknown>, over: Record<string, unknown> = {}) => ({ status: 'running', hunt: { ...HUNT, status: 'active', ...hunt }, ...over })
  const open = (id: string, body: unknown, state = 'executing') => {
    testState.cases = [{ ...openCase(id), combined_state: state, investigations: [investigation(state, state === 'executing', `run-${id}`)] }]
    testState.runs[`run-${id}`] = body
    renderCase(id)
  }
  let queued: NeedsYouItem[] = []
  const tick = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms) })
  beforeEach(() => {
    queued = []
    vi.mocked(approvalsApi.needsYou).mockImplementation(() => Promise.resolve({ data: { count: queued.length, items: queued } }) as never)
  })

  const GATE = {
    checkpoint_id: 'cp-1',
    checkpoint_class: 'hypothesis_approval',
    question: 'Approve and start this hunt on 2 explanations',
    raised_at: '2026-06-15T09:00:00Z',
    context: {
      hypotheses: [
        { hypothesis_id: 'h1', statement: 'FIN-WS-114 is beaconing to cdn-invoicesync.net after rundll32 ran from Temp', provenance: 'operator' },
        { hypothesis_id: 'h0', statement: 'the activity has a legitimate explanation and is not adversary action', provenance: 'base_rate' },
      ],
    },
  }
  const gated = () => run({ status: 'pending_approval', open_checkpoint: GATE }, { status: 'paused', reason: 'Waiting for your approval: Approve and start this hunt on 2 explanations' })
  const served = (): NeedsYouItem => need({
    kind: 'checkpoint', source_id: 'act-gate', title: GATE.question, reason: 'Vigil wants your go-ahead on the explanations it will test.',
    case_id: 'case-gate', checkpoint_id: 'cp-1', checkpoint_class: 'hypothesis_approval',
    hypotheses: GATE.context.hypotheses,
  })

  it('names every explanation it asks you to approve, the benign one labelled, and counts as the Explanations tab does', async () => {
    queued = [served()]
    open('case-gate', gated(), 'waiting_approval')
    const block = (await screen.findByRole('heading', { name: GATE.question })).closest('article') as HTMLElement
    const listed = within(block).getByRole('list', { name: 'What you are approving' })
    const rows = within(listed).getAllByRole('listitem')
    expect(rows).toHaveLength(2)
    expect(rows[0]).toHaveTextContent('Yours, to test')
    expect(rows[0]).toHaveTextContent('FIN-WS-114 is beaconing to cdn-invoicesync.net after rundll32 ran from Temp')
    expect(rows[1]).toHaveTextContent('The benign explanation it is tested against')
    expect(rows[1]).toHaveTextContent('the activity has a legitimate explanation and is not adversary action')
    // "2 explanations" in the ask, as the Explanations tab will count them
    await waitFor(() => expect(screen.getByRole('tab', { name: /Explanations/ })).toHaveTextContent('2'))
  })

  it('shows the decision with its Approve the moment the run waits, before the approvals queue has served it', async () => {
    open('case-gate', gated(), 'waiting_approval')
    const block = (await screen.findByRole('heading', { name: GATE.question })).closest('article') as HTMLElement
    expect(within(block).getByRole('button', { name: 'Approve' })).toBeEnabled()
    expect(within(block).getAllByRole('listitem')).toHaveLength(2)
    // no paused strip without its block
    expect(screen.queryByRole('region', { name: 'Run state' })).not.toBeInTheDocument()
    expect(screen.queryByText(/Waiting for your approval/)).not.toBeInTheDocument()

    // Approve goes through the queue's own item once it is there.
    queued = [served()]
    fireEvent.click(within(block).getByRole('button', { name: 'Approve' }))
    expect(await screen.findByText(`Approving: ${GATE.question}`)).toBeInTheDocument()
  })

  it('says approved at once after Approve and never flips back to waiting while the run catches up', async () => {
    queued = [served()]
    vi.mocked(approvalsApi.approve).mockResolvedValue({} as never)
    open('case-gate', gated(), 'waiting_approval')
    await screen.findByRole('heading', { name: GATE.question })

    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
      const state = screen.getByRole('region', { name: 'Run state' })
      expect(state).toHaveTextContent('Approved')
      expect(document.querySelector('.state-pill')).toHaveTextContent('Executing')

      queued = []
      for (const ms of [4000, 4000, 1, 5000, 5000]) {
        await tick(ms) // the fuse commits, the queue empties, the run is still parked on cp-1
        expect(screen.queryByText(/Waiting for your approval/)).not.toBeInTheDocument()
        expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument()
        expect(screen.getByRole('region', { name: 'Run state' })).toHaveTextContent('Approved')
      }
      expect(approvalsApi.approve).toHaveBeenCalledWith('act-gate')

      // The run reads the answer and goes on: the live card takes over.
      testState.runs['run-case-gate'] = run({ status: 'active', iteration: 4 })
      testState.cases = testState.cases.map((c) => ({ ...c, combined_state: 'executing' }))
      await tick(5000)
      expect(await screen.findByText('Now · step 4')).toBeInTheDocument()
      expect(screen.queryByText('Approved')).not.toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  const PARKED = run({ status: 'parked', reason: 'ran out of turns: iteration 48 of 48, having spent $0.1200 of $15.00' }, { status: 'paused' })

  it('offers Keep going on a hunt parked at its limit, sends the run page\'s extend, and counts it as needing you', async () => {
    queued = [need({ kind: 'parked', source_id: 'run-case-cap', run_id: 'run-case-cap', title: 'Keep the hunt going?', reason: 'Ran out of turns: iteration 48 of 48, having spent $0.1200 of $15.00', created_at: '', case_id: 'case-cap' })]
    open('case-cap', PARKED, 'open')
    const block = (await screen.findByRole('heading', { name: 'Keep the hunt going?' })).closest('article') as HTMLElement
    expect(document.querySelector('.state-pill')).toHaveTextContent('Needs you')
    expect(block).toHaveTextContent('Ran out of turns: iteration 48 of 48')
    expect(within(block).queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument()
    fireEvent.click(within(block).getByRole('button', { name: '+3 turns' }))
    await waitFor(() => expect(workflowApi.steer).toHaveBeenCalledWith('run-case-cap', 'extend', '', { grant: { iterations: 3, cost_usd: 0, wall_ms: 0 } }))
    expect(await within(block).findByRole('status')).toHaveTextContent('Keep going sent: +3 turns')
  })

  it('offers Keep going on the paused card too, before the queue has listed the parked hunt', async () => {
    open('case-cap2', PARKED, 'open')
    const state = await screen.findByRole('region', { name: 'Run state' })
    expect(state).toHaveTextContent('Ran out of turns')
    expect(within(state).getByRole('group', { name: 'Keep going' })).toBeInTheDocument()
  })

  it('offers nothing new on a run that ended for good', async () => {
    open('case-done', run({ status: 'terminal', outcome: 'completed', reason: 'concluded' }, { status: 'completed' }), 'open')
    expect(await screen.findByRole('region', { name: 'Run outcome' })).toBeInTheDocument()
    expect(screen.queryByRole('group', { name: 'Keep going' })).not.toBeInTheDocument()
  })

  it('ends the Now card with the run: one outcome off the verdicts, the prose behind the ⓘ, the open question as a decision', async () => {
    const prose = 'Accurate standing: h-1 should be reported as inconclusive, not proven, pending the host.'
    open('case-ended', run({
      status: 'terminal',
      outcome: 'completed',
      reason: `the agent stopped before the hunt could conclude (refused: open question q-fd133cef scores 8, at or above the priority floor of 5: Which host carries dl-005?): ${prose}`,
      open_questions: [{ question_id: 'q-fd133cef', question: 'Which host carries dl-005 and dl-003?', entity_key: null, hypothesis_id: null, spawned_iteration: 30 }],
      hypotheses: [
        { ...HUNT.hypotheses[0], status: 'proven', supports: 5, weakens: 0, statement: 'A finance workstation is beaconing' },
        { ...HUNT.hypotheses[1], status: 'inconclusive', statement: 'the benign account' },
      ],
    }, { status: 'completed' }), 'open')

    const outcome = await screen.findByRole('region', { name: 'Run outcome' })
    expect(outcome).toHaveTextContent('Ended')
    // an unresolved explanation reads off its bearing, never as ruled out
    expect(outcome).toHaveTextContent('1 proven · 1 forming')
    expect(document.querySelector('.case-reason')).toHaveTextContent('hunt concluded · 1 proven · 1 forming')
    expect(screen.queryByText(/Now · step/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Stop to ask you/)).not.toBeInTheDocument()
    expect(screen.queryByText(prose, { exact: false })).not.toBeInTheDocument()
    expect(within(outcome).getByRole('button', { name: 'What the run reported' })).toBeInTheDocument()

    const left = screen.getByRole('region', { name: 'Left open' })
    expect(within(left).getByRole('heading', { name: 'Which host carries dl-005 and dl-003?' })).toBeInTheDocument()
    expect(within(left).getByRole('button', { name: 'Ask about this' })).toBeInTheDocument()
    // Findings so far says the same verdicts the outcome does.
    expect(screen.getByText('A finance workstation is beaconing').closest('li')).toHaveTextContent('Proven')
  })

  it('says a handed-off hunt concluded after the Idle pill, tallies its words, and says why root cause did not run', async () => {
    testState.recordRows = [
      { id: 'activity:0', at: '2026-06-15T10:00:00Z', kind: 'agent_run_root_cause_not_started', source: 'case_activities', chained: false, text: 'Root cause was not started: it needs a SIEM.' },
    ]
    open('case-handed', run({
      status: 'terminal',
      outcome: 'completed',
      reason: 'concluded',
      hypotheses: [
        { ...HUNT.hypotheses[0], status: 'handed_off', supports: 5, weakens: 0 },
        { ...HUNT.hypotheses[1], status: 'parked', supports: 0, weakens: 8 },
      ],
    }, { status: 'completed' }), 'open')
    const outcome = await screen.findByRole('region', { name: 'Run outcome' })
    expect(outcome).toHaveTextContent('1 proven · 1 ruled out')
    expect(document.querySelector('.state-pill')).toHaveTextContent('Idle')
    expect(document.querySelector('.case-reason')).toHaveTextContent('hunt concluded, handed to IR')
    expect(await screen.findByRole('region', { name: 'Root cause' })).toHaveTextContent('Root cause was not started: it needs a SIEM.')
  })

  it('shows no root-cause line when the record has none', async () => {
    open('case-no-line', run({ status: 'terminal', outcome: 'completed', reason: 'concluded' }, { status: 'completed' }), 'open')
    expect(await screen.findByRole('region', { name: 'Run outcome' })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Root cause' })).not.toBeInTheDocument()
  })

  it('names the hunt lead as the agent on the case and shows the move in words', async () => {
    open('case-words2', run({
      moves: [{ decision_id: 'r9', query_intent: 'Searched alerts for HTTPS beaconing', action: 'QUERY', worker_agent_id: 'Hunt lead', iteration: 3, created_at: '2026-06-15T09:14:00Z' }],
    }))
    const now = await screen.findByRole('region', { name: 'Now' })
    await waitFor(() => expect(now).toHaveTextContent('Searched alerts for HTTPS beaconing'))
    expect(now).not.toHaveTextContent('{')
    const agents = screen.getByRole('table', { name: 'Agents' })
    expect(agents).toHaveTextContent('Hunt lead')
  })

  it('says not recorded for a cost the run never priced, and shows a sub-cent turn to the digit', async () => {
    open('case-costs', run({
      calls: [
        { question: 'q', tool: 'search_findings', result_length: 10, cost_usd: null, duration_ms: 8, iteration: 1 },
        { question: 'q', tool: 'lookup_indicators', result_length: 10, cost_usd: 0.0015, iteration: 2 },
      ],
    }))
    fireEvent.click(await screen.findByRole('tab', { name: /Checked/ }))
    const card = (await screen.findByText(/What Vigil checked/)).closest('section') as HTMLElement
    const [unpriced, priced] = Array.from(card.querySelectorAll('section.checked-turn'))
    expect(unpriced).toHaveTextContent('8 ms')
    expect(unpriced).not.toHaveTextContent('$')
    expect(priced).toHaveTextContent('$0.0015')
    expect(priced).not.toHaveTextContent('$0.00 ')
  })

  it('says "Coming in a later release" on every drawn but not live control, never the internal wording', async () => {
    open('case-later', run({ evidence: [...HUNT.evidence, { evidence_id: 'g1', iteration: 1, source_system: 'deployment', summary: 'no telemetry search', is_gap: true, gap_detail: null, source_tier: null, bears_on: [] }] }))
    fireEvent.click(await screen.findByRole('tab', { name: /Explanations/ }))
    for (const name of ['+ Add an explanation', 'Rule one out']) {
      expect(await screen.findByRole('button', { name })).toHaveAttribute('title', 'Coming in a later release')
    }
    fireEvent.click(screen.getByRole('tab', { name: /Memory and blind spots/ }))
    expect(await screen.findByRole('button', { name: 'Record a blind spot' })).toHaveAttribute('title', 'Coming in a later release')
    expect(document.querySelector('[title*="phase-2"], [aria-label*="phase-2"]')).toBeNull()
    expect(screen.getByRole('button', { name: 'Tell' })).toHaveAttribute('title', 'Coming in a later release')
  })
})
