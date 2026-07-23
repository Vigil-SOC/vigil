import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import CasesScreen from './CasesScreen'
import { ToastProvider } from '../../shell/toast'
import { casesApi } from '../../../services/api'

const testState = vi.hoisted(() => ({
  canDelete: true,
  cases: [] as Array<Record<string, unknown>>,
}))

vi.mock('../../../contexts/AuthContext', () => ({
  useAuth: () => ({
    hasPermission: (permission: string) => permission !== 'cases.delete' || testState.canDelete,
  }),
}))

vi.mock('../../../services/api', () => ({
  casesApi: {
    getAll: vi.fn(() => Promise.resolve({ data: { cases: testState.cases } })),
    getById: vi.fn((id: string) =>
      Promise.resolve({ data: testState.cases.find((item) => item.case_id === id) }),
    ),
    delete: vi.fn(),
  },
  findingsApi: { getById: vi.fn() },
  caseSearchApi: { search: vi.fn() },
  timelineApi: { getCaseTimeline: vi.fn(() => Promise.resolve({ data: { events: [] } })) },
  timesketchApi: { exportCase: vi.fn() },
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
