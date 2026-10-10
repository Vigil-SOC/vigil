import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import CasesScreen from './CasesScreen'
import { ToastProvider } from '../../shell/toast'
import { casesApi, workflowApi } from '../../services/api'
import { EMPTY_STRIP, notifyCasesChanged } from './useCases'

const testState = vi.hoisted(() => ({
  canDelete: true,
  cases: [] as Array<Record<string, unknown>>,
  strip: undefined as Record<string, unknown> | undefined,
}))

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: { username: 'analyst' },
    hasPermission: (permission: string) => permission !== 'cases.delete' || testState.canDelete,
  }),
}))

vi.mock('../../services/api', () => ({
  casesApi: {
    getAll: vi.fn(() => Promise.resolve({ data: { cases: testState.cases, strip: testState.strip } })),
    getById: vi.fn((id: string) =>
      Promise.resolve({ data: testState.cases.find((item) => item.case_id === id) }),
    ),
    delete: vi.fn(),
    update: vi.fn(() => Promise.resolve({ data: { success: true } })),
    getSLA: vi.fn(() => Promise.resolve({ data: {} })),
    getRecord: vi.fn(() => Promise.resolve({ data: { rows: [], run_id: null, investigation_id: null } })),
    getComments: vi.fn(() => Promise.resolve({ data: { comments: [] } })),
    getTasks: vi.fn(() => Promise.resolve({ data: { tasks: [] } })),
    getEvidence: vi.fn(() => Promise.resolve({ data: { evidence: [] } })),
    getIOCs: vi.fn(() => Promise.resolve({ data: { iocs: [] } })),
    getEscalations: vi.fn(() => Promise.resolve({ data: { escalations: [] } })),
  },
  workflowApi: {
    listAll: vi.fn(() =>
      Promise.resolve({
        data: {
          workflows: [
            { id: 'incident-response', name: 'Incident response', run_kind: 'investigate' },
            { id: 'hunt-a', name: 'Hunt A', run_kind: 'hunt' },
            { id: 'hunt-b', name: 'Hunt B', run_kind: 'hunt' },
            { id: 'rc', name: 'RC', run_kind: 'root_cause' },
            { id: 'odd', name: 'Odd', run_kind: 'triage_lite' },
          ],
        },
      }),
    ),
    getRun: vi.fn(() => Promise.resolve({ data: {} })),
    replayRun: vi.fn(),
    verifyRun: vi.fn(),
  },
  orchestratorApi: {
    exportInvestigation: vi.fn(),
  },
  findingsApi: { getById: vi.fn() },
  caseSearchApi: { search: vi.fn() },
  timelineApi: { getCaseTimeline: vi.fn(() => Promise.resolve({ data: { events: [] } })) },
  default: { get: vi.fn(() => Promise.resolve({ data: {} })) },
  agentsApi: { listAgents: vi.fn(() => Promise.resolve({ data: { agents: [] } })) },
  conversationsApi: {
    list: vi.fn(() => Promise.resolve({ data: { conversations: [] } })),
    get: vi.fn(() => Promise.resolve({ data: { messages: [] } })),
    update: vi.fn(() => Promise.resolve({ data: {} })),
    delete: vi.fn(),
    importHistory: vi.fn(() => Promise.resolve({ data: {} })),
  },
  reasoningApi: {
    getSessionSummary: vi.fn(() => Promise.resolve(null)),
    listInteractions: vi.fn(() => Promise.resolve({ interactions: [] })),
    getInteraction: vi.fn(),
  },
  streamFetch: vi.fn(),
  approvalsApi: {
    needsYou: vi.fn(() => Promise.resolve({ data: { count: 0, items: [] } })),
    approve: vi.fn(() => Promise.resolve({})),
    reject: vi.fn(() => Promise.resolve({})),
  },
}))

const CASE = {
  case_id: 'case-2026-0142',
  title: 'Suspicious outbound traffic',
  status: 'open',
  priority: 'high',
  assignee: 'analyst',
  finding_ids: [],
  findings: [],
  created_at: '2026-06-15T09:14:00Z',
  updated_at: '2026-06-15T09:14:00Z',
}

function renderCases(path = '/cases') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <ToastProvider>
        <Routes>
          <Route
            path="/cases"
            element={
              <CasesScreen
                openChat={vi.fn()}
                go={vi.fn()}
                goSettings={vi.fn()}
                openCase={openCase}
                setViewFull={vi.fn()}
              />
            }
          />
        </Routes>
      </ToastProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  testState.canDelete = true
  testState.cases = [{ ...CASE }]
  testState.strip = undefined
  vi.mocked(casesApi.delete).mockReset()
  vi.mocked(casesApi.getAll).mockClear()
  openCase.mockClear()
})

const openCase = vi.fn()

/** Deletion lives on the case page, behind the ⋯ menu. */
async function openDelete() {
  expect(await screen.findByRole('heading', { name: CASE.title })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Case actions' }))
  fireEvent.click(screen.getByRole('menuitem', { name: 'Delete case' }))
}

describe('case deletion', () => {
  it('leaves no delete control on the list rows', async () => {
    renderCases()

    expect(await screen.findByText(CASE.title)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Delete case/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Open case/ })).not.toBeInTheDocument()
  })

  it('hides the Delete item without cases.delete permission', async () => {
    testState.canDelete = false
    renderCases(`/cases?case=${CASE.case_id}`)

    expect(await screen.findByRole('heading', { name: CASE.title })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Case actions' }))
    expect(screen.queryByRole('menuitem', { name: 'Delete case' })).not.toBeInTheDocument()
  })

  it('requires confirmation and supports cancellation', async () => {
    renderCases(`/cases?case=${CASE.case_id}`)
    await openDelete()

    expect(screen.getByRole('dialog', { name: 'Delete case?' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(casesApi.delete).not.toHaveBeenCalled()
    expect(screen.queryByRole('dialog', { name: 'Delete case?' })).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: CASE.title })).toBeInTheDocument()
  })

  it('keeps the confirmation open and shows backend errors', async () => {
    vi.mocked(casesApi.delete).mockRejectedValueOnce({
      response: { data: { detail: 'Case is locked by an active workflow' } },
    })
    renderCases(`/cases?case=${CASE.case_id}`)
    await openDelete()
    fireEvent.click(
      within(screen.getByRole('dialog', { name: 'Delete case?' })).getByRole('button', { name: 'Delete case' }),
    )

    expect(await screen.findByText('Case is locked by an active workflow', { selector: 'span[role="alert"]' }))
      .toBeInTheDocument()
    expect(screen.getByRole('dialog', { name: 'Delete case?' })).toBeInTheDocument()
  })

  it('returns to the refreshed case list after deleting', async () => {
    vi.mocked(casesApi.delete).mockImplementationOnce(async () => {
      testState.cases = []
      return { data: { success: true } } as never
    })
    renderCases(`/cases?case=${CASE.case_id}`)
    await openDelete()
    fireEvent.click(
      within(screen.getByRole('dialog', { name: 'Delete case?' })).getByRole('button', { name: 'Delete case' }),
    )

    await waitFor(() => expect(casesApi.delete).toHaveBeenCalledWith(CASE.case_id))
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'New case' }).length).toBeGreaterThan(0))
    expect(screen.getByText(`Deleted ${CASE.case_id}. Linked findings were preserved.`)).toBeInTheDocument()
    expect(screen.queryByText(CASE.title)).not.toBeInTheDocument()
  })
})

describe('server queue', () => {
  it('asks the server for the first page and opens a row in the drawer', async () => {
    renderCases()

    await screen.findByText(CASE.title)
    expect(casesApi.getAll).toHaveBeenCalledWith(expect.objectContaining({ limit: 100, offset: 0 }))
    fireEvent.click(screen.getByText(CASE.title))
    expect(openCase).toHaveBeenCalledWith(CASE.case_id)
    expect(screen.queryByRole('heading', { name: CASE.title })).not.toBeInTheDocument()
  })

  it('reloads the list when a case changes elsewhere', async () => {
    renderCases()
    await screen.findByText(CASE.title)
    testState.cases = [{ ...CASE, title: 'Renamed in the drawer' }]
    act(() => notifyCasesChanged())
    expect(await screen.findByText('Renamed in the drawer')).toBeInTheDocument()
  })

  it('shows the case page alone, without a list pane', async () => {
    testState.cases = [{ ...CASE }, { ...CASE, case_id: 'other', title: 'Another case' }]
    renderCases(`/cases?case=${CASE.case_id}`)

    expect(await screen.findByRole('heading', { name: CASE.title })).toBeInTheDocument()
    expect(screen.queryByText('Another case')).not.toBeInTheDocument()
    expect(casesApi.getAll).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Cases' }))
    expect(await screen.findByText('Another case')).toBeInTheDocument()
  })

  it('carries the full case id in the id cell', async () => {
    const id = 'case-2026-10-09-0b43d6c8'
    testState.cases = [{ ...CASE, case_id: id }]
    renderCases('/cases')

    const cell = (await screen.findByText(id)).closest('[role="cell"]')
    expect(cell).toHaveClass('cg-id')
    expect(cell).toHaveAttribute('title', id)
  })

  it('labels needs-you rows and leaves the others unlabeled', async () => {
    testState.cases = [
      { ...CASE, case_id: 'needs', title: 'Waiting on a person', needs_you: true },
      { ...CASE, case_id: 'sla', title: 'Closer to SLA', needs_you: false },
    ]
    renderCases()

    const needsRow = (await screen.findByText('Waiting on a person')).closest('[role="row"]')
    const slaRow = screen.getByText('Closer to SLA').closest('[role="row"]')
    expect(needsRow).not.toBeNull()
    expect(slaRow).not.toBeNull()
    expect(within(needsRow as HTMLElement).getByText('Needs you')).toBeInTheDocument()
    expect(within(slaRow as HTMLElement).queryByText('Needs you')).not.toBeInTheDocument()
    expect(needsRow).toHaveClass('needs')
    expect(slaRow).not.toHaveClass('needs')
  })
})

describe('board grid', () => {
  const rowOf = async (title: string) => (await screen.findByText(title)).closest('[role="row"]') as HTMLElement

  it('lays a case out in the board columns, owner "(you)", kind by workflow name', async () => {
    testState.cases = [{
      ...CASE,
      workflow_id: 'hunt-a',
      findings_count: 1,
      iteration_count: 4,
      age_seconds: 2 * 86400,
      last_activity: new Date(Date.now() - 3 * 3600_000).toISOString(),
      sla_seconds_left: 22 * 3600 + 50 * 60,
      health_status: 'healthy',
      cost_usd: 2,
      max_cost_usd: 5,
      budget_health: 'healthy',
      comment_count: 3,
    }]
    renderCases()

    const row = within(await rowOf(CASE.title))
    expect(row.getByText(CASE.case_id)).toBeInTheDocument()
    expect(row.getByText('1 alert · step 4 · active 3h ago · age 2d')).toBeInTheDocument()
    expect(row.getByText('Hunt A')).toBeInTheDocument()
    expect(row.getByText('High')).toBeInTheDocument()
    expect(row.getByText('Open')).toBeInTheDocument()
    expect(row.getByText('analyst (you)')).toBeInTheDocument()
    expect(row.getByText('22 h 50 min left')).toBeInTheDocument()
    expect(row.getByText('Good')).toHaveClass('level-pill', 'good')
    expect(row.getByRole('meter')).toHaveAttribute('aria-valuenow', '40')
    expect(row.getByText('40%')).toBeInTheDocument()
    expect(row.getByText('3')).toBeInTheDocument()
    // the old trash and arrow stay gone
    expect(row.queryByRole('button')).not.toBeInTheDocument()
  })

  it('falls back to the workflow id when it is not in the catalog, and a dash with none', async () => {
    testState.cases = [
      { ...CASE, case_id: 'a', title: 'Known elsewhere', workflow_id: 'unlisted' },
      { ...CASE, case_id: 'b', title: 'No run yet', assignee: 'someone.else' },
    ]
    renderCases()

    expect(within(await rowOf('Known elsewhere')).getByText('unlisted')).toBeInTheDocument()
    const bare = within(await rowOf('No run yet'))
    expect(bare.getByText('someone.else')).toBeInTheDocument()
    expect(bare.queryByText(/\(you\)/)).not.toBeInTheDocument()
  })

  it('says Timer paused with no level pill when the SLA is paused', async () => {
    testState.cases = [{ ...CASE, sla_paused: true, sla_seconds_left: null, health_status: 'healthy' }]
    renderCases()

    const row = within(await rowOf(CASE.title))
    expect(row.getByText('Timer paused')).toBeInTheDocument()
    expect(row.queryByText('Good')).not.toBeInTheDocument()
  })

  it('shows a dash for no SLA, and Poor once past due', async () => {
    testState.cases = [
      { ...CASE, case_id: 'a', title: 'No SLA' },
      { ...CASE, case_id: 'b', title: 'Late', sla_seconds_left: -600, health_status: 'breached' },
    ]
    renderCases()

    expect(within(await rowOf('No SLA')).queryByText('Poor')).not.toBeInTheDocument()
    const late = within(await rowOf('Late'))
    expect(late.getByText('10 min overdue')).toBeInTheDocument()
    expect(late.getByText('Poor')).toHaveClass('poor')
  })

  it('shows a dash instead of a meter when there is no cap, and clamps over-spend', async () => {
    testState.cases = [
      { ...CASE, case_id: 'a', title: 'Uncapped', cost_usd: 3 },
      { ...CASE, case_id: 'b', title: 'Over', cost_usd: 9, max_cost_usd: 5, budget_health: 'critical' },
    ]
    renderCases()

    const uncapped = within(await rowOf('Uncapped'))
    expect(uncapped.queryByRole('meter')).not.toBeInTheDocument()
    expect(uncapped.queryByText(/%/)).not.toBeInTheDocument()
    const over = within(await rowOf('Over'))
    expect(over.getByRole('meter')).toHaveAttribute('aria-valuenow', '100')
    // the shared MeterBar puts the level on its fill
    expect(over.getByRole('meter').firstElementChild).toHaveClass('meter-fill', 'poor')
  })

  it('puts the description on hover and not in the row', async () => {
    testState.cases = [{ ...CASE, description: 'Beaconing to a rare domain' }]
    renderCases()

    const row = await rowOf(CASE.title)
    expect(within(row).queryByText('Beaconing to a rare domain')).not.toBeInTheDocument()
    expect(screen.getByTitle('Beaconing to a rare domain')).toContainElement(screen.getByText(CASE.title))
  })

  it('has a disabled select box that neither opens the case nor claims a selection', async () => {
    renderCases()

    const box = await screen.findByRole('checkbox', { name: `Select case ${CASE.case_id}` })
    expect(box).toBeDisabled()
    expect(box.closest('[title]')).toHaveAttribute('title', 'Coming in a later release')
    fireEvent.click(box.closest('[title]') as HTMLElement)
    expect(openCase).not.toHaveBeenCalled()
  })

  it('opens a row from the keyboard', async () => {
    renderCases()

    const row = await rowOf(CASE.title)
    fireEvent.keyDown(row, { key: 'Enter' })
    expect(openCase).toHaveBeenCalledWith(CASE.case_id)
  })
})

describe('unknown priority', () => {
  it('renders Unknown on the list and offers it as a filter', async () => {
    testState.cases = [{ ...CASE, priority: 'unknown' }]
    renderCases()

    expect(await screen.findByText('Unknown')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Filters' }))
    expect(
      within(screen.getByRole('dialog', { name: 'Filters' })).getByRole('button', { name: 'Unknown' }),
    ).toBeInTheDocument()
  })

  it('keeps Unknown selected in the edit dialog', async () => {
    testState.cases = [{ ...CASE, priority: 'unknown' }]
    renderCases(`/cases?case=${CASE.case_id}`)

    expect(await screen.findByRole('heading', { name: CASE.title })).toBeInTheDocument()
    expect(screen.getByTitle('Unknown priority')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Case actions' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Edit' }))
    expect(within(screen.getByRole('dialog', { name: 'Edit case' })).getByText('Unknown')).toBeInTheDocument()
  })
})

describe('edit case assignee', () => {
  const openEdit = async () => {
    renderCases(`/cases?case=${CASE.case_id}`)
    expect(await screen.findByRole('heading', { name: CASE.title })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Case actions' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Edit' }))
    return within(screen.getByRole('dialog', { name: 'Edit case' }))
  }
  const sentBody = () => vi.mocked(casesApi.update).mock.lastCall?.[1]

  it('sends the assignee it was preloaded with on a title-only edit', async () => {
    testState.cases = [CASE]
    const dialog = await openEdit()
    fireEvent.change(dialog.getByDisplayValue(CASE.title), { target: { value: 'Renamed' } })
    fireEvent.click(dialog.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(sentBody()).toMatchObject({ title: 'Renamed', assignee: 'analyst' }))
  })

  it('sends an empty assignee, not an omitted key, when the field is cleared', async () => {
    testState.cases = [CASE]
    const dialog = await openEdit()
    fireEvent.change(dialog.getByPlaceholderText('name or email'), { target: { value: '' } })
    fireEvent.click(dialog.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(sentBody()).toHaveProperty('assignee', ''))
  })

  it('does not preload "unassigned" into the field for an unassigned case', async () => {
    testState.cases = [{ ...CASE, assignee: null }]
    const dialog = await openEdit()
    expect(dialog.getByPlaceholderText('name or email')).toHaveValue('')
  })
})

const STRIP = {
  by_state: { investigating: 3, waiting_approval: 1, assigned: 2, executing: 0 },
  sla_at_risk: 4,
  closed_today: 5,
  agent_closure_share: 0.6,
  needs_you: 2,
}

const lastParams = () => {
  const calls = vi.mocked(casesApi.getAll).mock.calls
  return calls[calls.length - 1]?.[0]
}
const chip = (name: string | RegExp) => screen.getByRole('button', { name })

describe('page head', () => {
  it('heads the list with the actions beside it and no KPI tiles', async () => {
    renderCases()
    await screen.findByText(CASE.title)

    expect(screen.getByRole('heading', { name: 'Cases' })).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Search cases' })).toBeInTheDocument()
    for (const name of ['Filters', 'Advanced', 'Refresh', 'New case']) {
      expect(chip(name)).toBeInTheDocument()
    }
    for (const gone of ['SLA at risk', 'Agent closures']) expect(screen.queryByText(gone)).not.toBeInTheDocument()
  })

  it('opens the advanced search panel from Advanced', async () => {
    renderCases()
    await screen.findByText(CASE.title)
    fireEvent.click(chip('Advanced'))
    expect(screen.getByText('Full-text query')).toBeInTheDocument()
  })

  it('keeps the Filters panel to Priority, Assignee, Workflow and Data source', async () => {
    renderCases()
    await screen.findByText(CASE.title)
    fireEvent.click(chip('Filters'))
    const panel = within(screen.getByRole('dialog', { name: 'Filters' }))
    expect(panel.getByText('Priority')).toBeInTheDocument()
    expect(panel.getByLabelText('Data source filter')).toBeInTheDocument()
    expect(panel.queryByText('State')).not.toBeInTheDocument()
    expect(panel.queryByText('SLA')).not.toBeInTheDocument()
  })
})

describe('chip rows', () => {
  beforeEach(() => {
    testState.strip = STRIP
  })

  it('counts the state chips from the strip and hides the empty ones', async () => {
    renderCases()
    await screen.findByText(CASE.title)
    const states = within(screen.getByRole('group', { name: 'State' }))

    expect(states.getByRole('button', { name: 'All open 6' })).toHaveAttribute('aria-pressed', 'true')
    expect(states.getByRole('button', { name: 'Needs you 2' })).toBeInTheDocument()
    expect(states.getByRole('button', { name: 'Investigating 3' })).toBeInTheDocument()
    expect(states.getByRole('button', { name: 'Waiting approval 1' })).toBeInTheDocument()
    expect(states.getByRole('button', { name: 'Assigned 2' })).toBeInTheDocument()
    expect(states.queryByRole('button', { name: /Executing/ })).not.toBeInTheDocument()
    expect(states.getByRole('button', { name: 'Closed today 5 · 60% by an agent alone' })).toBeInTheDocument()
  })

  it('sends the state each chip names, one at a time', async () => {
    renderCases()
    await screen.findByText(CASE.title)

    fireEvent.click(chip('Investigating 3'))
    await waitFor(() => expect(lastParams()).toMatchObject({ state: 'investigating', offset: 0 }))
    expect(chip('Investigating 3')).toHaveAttribute('aria-pressed', 'true')
    expect(chip('All open 6')).toHaveAttribute('aria-pressed', 'false')

    fireEvent.click(chip('Assigned 2'))
    await waitFor(() => expect(lastParams()).toMatchObject({ state: 'assigned' }))
    expect(chip('Investigating 3')).toHaveAttribute('aria-pressed', 'false')

    fireEvent.click(chip('All open 6'))
    await waitFor(() => expect(lastParams()).not.toHaveProperty('state'))
  })

  it('keeps a selected state chip visible when its count reaches zero', async () => {
    renderCases()
    await screen.findByText(CASE.title)
    fireEvent.click(chip('Investigating 3'))
    testState.strip = { ...STRIP, by_state: { assigned: 2 } }
    fireEvent.click(chip('Refresh'))
    expect(await screen.findByRole('button', { name: 'Investigating 0' })).toHaveAttribute('aria-pressed', 'true')
  })

  it('Needs you sends needs_you and drops the state, and a state drops it again', async () => {
    renderCases()
    await screen.findByText(CASE.title)
    fireEvent.click(chip('Assigned 2'))
    await waitFor(() => expect(lastParams()).toMatchObject({ state: 'assigned' }))

    fireEvent.click(chip('Needs you 2'))
    await waitFor(() => expect(lastParams()).toMatchObject({ needs_you: true }))
    expect(lastParams()).not.toHaveProperty('state')
    expect(chip('Assigned 2')).toHaveAttribute('aria-pressed', 'false')

    fireEvent.click(chip('Investigating 3'))
    await waitFor(() => expect(lastParams()).toMatchObject({ state: 'investigating' }))
    expect(lastParams()).not.toHaveProperty('needs_you')
  })

  it('Closed today toggles the closed filter', async () => {
    renderCases()
    await screen.findByText(CASE.title)
    const closed = () => chip(/^Closed today/)

    fireEvent.click(closed())
    await waitFor(() => expect(lastParams()).toMatchObject({ closed: true }))
    expect(closed()).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(closed())
    await waitFor(() => expect(lastParams()).not.toHaveProperty('closed'))
  })

  it('shows a dash for the agent share when nothing closed today', async () => {
    testState.strip = { ...STRIP, closed_today: 0, agent_closure_share: 0 }
    renderCases()
    expect(await screen.findByRole('button', { name: 'Closed today 0 · — by an agent alone' })).toBeInTheDocument()
  })

  it('offers a chip per run kind in the catalog and sends the one picked', async () => {
    renderCases()
    const kinds = within(await screen.findByRole('group', { name: 'Kind' }))

    expect(kinds.getAllByRole('button').map((b) => b.textContent)).toEqual([
      'All kinds', 'Investigation', 'Hunt', 'Root cause', 'Triage lite',
    ])
    fireEvent.click(kinds.getByRole('button', { name: 'Hunt' }))
    await waitFor(() => expect(lastParams()).toMatchObject({ kind: 'hunt' }))
    fireEvent.click(kinds.getByRole('button', { name: 'All kinds' }))
    await waitFor(() => expect(lastParams()).not.toHaveProperty('kind'))
  })

  it('hides the kind row when the catalog fails', async () => {
    vi.mocked(workflowApi.listAll).mockRejectedValueOnce(new Error('down'))
    renderCases()
    await screen.findByText(CASE.title)
    expect(screen.getByRole('group', { name: 'State' })).toBeInTheDocument()
    expect(screen.queryByRole('group', { name: 'Kind' })).not.toBeInTheDocument()
  })

  it('Assigned to me sends the signed-in username and shares the Assignee field', async () => {
    renderCases()
    await screen.findByText(CASE.title)

    fireEvent.click(chip('Assigned to me'))
    await waitFor(() => expect(lastParams()).toMatchObject({ assignee: 'analyst' }))
    expect(chip('Assigned to me')).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(chip(/^Filters/))
    expect(screen.getByLabelText('Assignee filter')).toHaveValue('analyst')

    fireEvent.click(chip('Everyone’s'))
    await waitFor(() => expect(lastParams()).not.toHaveProperty('assignee'))
  })

  it('Near SLA sends sla_at_risk, counts from the strip, and excludes Assigned to me', async () => {
    renderCases()
    await screen.findByText(CASE.title)
    fireEvent.click(chip('Assigned to me'))
    await waitFor(() => expect(lastParams()).toMatchObject({ assignee: 'analyst' }))

    fireEvent.click(chip('Near SLA 4'))
    await waitFor(() => expect(lastParams()).toMatchObject({ sla_at_risk: true }))
    expect(lastParams()).not.toHaveProperty('assignee')
    expect(chip('Assigned to me')).toHaveAttribute('aria-pressed', 'false')

    fireEvent.click(chip('Near SLA 4'))
    await waitFor(() => expect(lastParams()).not.toHaveProperty('sla_at_risk'))
    expect(chip('Everyone’s')).toHaveAttribute('aria-pressed', 'true')
  })

  it('has no Watching, Stuck, Paused or On hold chip', async () => {
    renderCases()
    await screen.findByText(CASE.title)
    expect(screen.queryByRole('button', { name: /Watching|Stuck|Paused|On hold/ })).not.toBeInTheDocument()
  })
})

describe('list states', () => {
  it('renders a zero strip (demo) with the chips intact', async () => {
    testState.strip = EMPTY_STRIP as unknown as Record<string, unknown>
    testState.cases = []
    renderCases()

    expect(await screen.findByText('No cases yet')).toBeInTheDocument()
    expect(chip('All open 0')).toBeInTheDocument()
    expect(chip('Needs you 0')).toBeInTheDocument()
    expect(chip('Near SLA 0')).toBeInTheDocument()
    expect(chip('Closed today 0 · — by an agent alone')).toBeInTheDocument()
  })

  it('shows the loading row, then the rows', async () => {
    renderCases()
    expect(screen.getByText('Loading cases…')).toBeInTheDocument()
    expect(await screen.findByText(CASE.title)).toBeInTheDocument()
  })

  it('shows the error with a retry', async () => {
    vi.mocked(casesApi.getAll).mockRejectedValueOnce(new Error('boom'))
    renderCases()
    expect(await screen.findByText('Couldn’t load cases')).toBeInTheDocument()
    fireEvent.click(chip('Retry'))
    expect(await screen.findByText(CASE.title)).toBeInTheDocument()
  })

  it('says no cases match when a chip filter is empty, and Clear filters resets every chip', async () => {
    testState.strip = STRIP
    renderCases()
    await screen.findByText(CASE.title)
    fireEvent.click(chip('Needs you 2'))
    testState.cases = []
    fireEvent.click(chip('Hunt'))

    expect(await screen.findByText('No cases match these filters')).toBeInTheDocument()
    fireEvent.click(chip('Clear filters'))
    await waitFor(() => expect(lastParams()).not.toHaveProperty('needs_you'))
    expect(lastParams()).not.toHaveProperty('kind')
    expect(chip('All open 6')).toHaveAttribute('aria-pressed', 'true')
  })
})
