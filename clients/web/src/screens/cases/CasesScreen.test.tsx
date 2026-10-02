import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import CasesScreen from './CasesScreen'
import { ToastProvider } from '../../shell/toast'
import { casesApi } from '../../services/api'

const testState = vi.hoisted(() => ({
  canDelete: true,
  cases: [] as Array<Record<string, unknown>>,
}))

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({
    hasPermission: (permission: string) => permission !== 'cases.delete' || testState.canDelete,
  }),
}))

vi.mock('../../services/api', () => ({
  casesApi: {
    getAll: vi.fn(() => Promise.resolve({ data: { cases: testState.cases } })),
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
  vi.mocked(casesApi.delete).mockReset()
})

describe('case deletion', () => {
  it('hides delete controls without cases.delete permission', async () => {
    testState.canDelete = false
    renderCases()

    expect(await screen.findByText(CASE.title)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: `Delete case ${CASE.case_id}` })).not.toBeInTheDocument()
  })

  it('requires confirmation and supports cancellation', async () => {
    renderCases()
    fireEvent.click(await screen.findByRole('button', { name: `Delete case ${CASE.case_id}` }))

    expect(screen.getByRole('dialog', { name: 'Delete case?' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(casesApi.delete).not.toHaveBeenCalled()
    expect(screen.queryByRole('dialog', { name: 'Delete case?' })).not.toBeInTheDocument()
    expect(screen.getByText(CASE.title)).toBeInTheDocument()
  })

  it('deletes a case and refreshes the list', async () => {
    vi.mocked(casesApi.delete).mockImplementationOnce(async () => {
      testState.cases = []
      return { data: { success: true } } as never
    })
    renderCases()
    fireEvent.click(await screen.findByRole('button', { name: `Delete case ${CASE.case_id}` }))
    fireEvent.click(screen.getByRole('button', { name: 'Delete case' }))

    await waitFor(() => expect(casesApi.delete).toHaveBeenCalledWith(CASE.case_id))
    await waitFor(() => expect(screen.queryByText(CASE.title)).not.toBeInTheDocument())
    expect(screen.getByText(`Deleted ${CASE.case_id}. Linked findings were preserved.`)).toBeInTheDocument()
  })

  it('keeps the confirmation open and shows backend errors', async () => {
    vi.mocked(casesApi.delete).mockRejectedValueOnce({
      response: { data: { detail: 'Case is locked by an active workflow' } },
    })
    renderCases()
    fireEvent.click(await screen.findByRole('button', { name: `Delete case ${CASE.case_id}` }))
    fireEvent.click(screen.getByRole('button', { name: 'Delete case' }))

    expect(await screen.findByText('Case is locked by an active workflow', { selector: 'span[role="alert"]' }))
      .toBeInTheDocument()
    expect(screen.getByRole('dialog', { name: 'Delete case?' })).toBeInTheDocument()
    expect(screen.getByText(CASE.title)).toBeInTheDocument()
  })

  it('returns to the case list after deleting an open detail', async () => {
    vi.mocked(casesApi.delete).mockImplementationOnce(async () => {
      testState.cases = []
      return { data: { success: true } } as never
    })
    renderCases(`/cases?case=${CASE.case_id}`)

    expect(await screen.findByRole('heading', { name: CASE.title })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Delete case' }))
    fireEvent.click(
      within(screen.getByRole('dialog', { name: 'Delete case?' }))
        .getByRole('button', { name: 'Delete case' }),
    )

    await waitFor(() => expect(casesApi.delete).toHaveBeenCalledWith(CASE.case_id))
    await waitFor(() => expect(screen.getByRole('button', { name: 'New Case' })).toBeInTheDocument())
  })
})

describe('server queue', () => {
  it('asks the server for the first page and opens a row', async () => {
    renderCases()

    await screen.findByText(CASE.title)
    expect(casesApi.getAll).toHaveBeenCalledWith(expect.objectContaining({ limit: 100, offset: 0 }))
    fireEvent.click(screen.getByText(CASE.title))
    expect(await screen.findByRole('heading', { name: CASE.title })).toBeInTheDocument()
  })

  it('labels needs-you rows and leaves the others unlabeled', async () => {
    testState.cases = [
      { ...CASE, case_id: 'needs', title: 'Waiting on a person', needs_you: true },
      { ...CASE, case_id: 'sla', title: 'Closer to SLA', needs_you: false },
    ]
    renderCases()

    const needsRow = (await screen.findByText('Waiting on a person')).closest('tr')
    const slaRow = screen.getByText('Closer to SLA').closest('tr')
    expect(needsRow).not.toBeNull()
    expect(slaRow).not.toBeNull()
    expect(within(needsRow as HTMLElement).getByText('Needs you')).toBeInTheDocument()
    expect(within(slaRow as HTMLElement).queryByText('Needs you')).not.toBeInTheDocument()
  })

  it('sends a closed filter to the server', async () => {
    renderCases()
    await screen.findByText(CASE.title)

    fireEvent.click(screen.getByRole('button', { name: 'Filters' }))
    fireEvent.click(
      within(screen.getByRole('dialog', { name: 'Filters' })).getByRole('button', { name: 'Closed' }),
    )

    await waitFor(() =>
      expect(casesApi.getAll).toHaveBeenCalledWith(expect.objectContaining({ closed: true, offset: 0 })),
    )
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
    expect(screen.getByText('Unknown priority')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    expect(within(screen.getByRole('dialog', { name: 'Edit case' })).getByText('Unknown')).toBeInTheDocument()
  })
})
