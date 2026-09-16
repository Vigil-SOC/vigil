/* `/workflows?run=<id>` is how a case's activity reaches the run it was written
   by (#951). The screen must open that run in place of the catalog, and clearing
   the param must give the catalog back without a reload. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import WorkflowsScreen from './WorkflowsScreen'
import { workflowApi } from '../../services/api'

vi.mock('../../services/api', () => ({
  workflowApi: {
    listAll: vi.fn(() => Promise.resolve({ data: { workflows: [{ id: 'wf-1', name: 'Beacon hunt', description: 'd', steps: [] }] } })),
    getRun: vi.fn(),
  },
  agentsApi: { listAgents: vi.fn(() => Promise.resolve({ data: { agents: [] } })) },
}))
vi.mock('../../services/skillsApi', () => ({
  skillsApi: { list: vi.fn(() => Promise.resolve([])) },
}))

function Search() {
  return <span data-testid="search">{useLocation().search}</span>
}

const renderAt = (path: string) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route
          path="/workflows"
          element={<><WorkflowsScreen openChat={vi.fn()} go={vi.fn()} goSettings={vi.fn()} setViewFull={vi.fn()} /><Search /></>}
        />
      </Routes>
    </MemoryRouter>,
  )

beforeEach(() => {
  vi.mocked(workflowApi.getRun).mockReset()
  vi.mocked(workflowApi.getRun).mockResolvedValue({
    data: { run_id: 'run-1', status: 'completed', result_summary: 'Two hosts were beaconing.' },
  } as never)
})

afterEach(() => {
  vi.useRealTimers()
})

describe('/workflows?run=<id>', () => {
  it('opens that run in place of the catalog, and the back control clears the param', async () => {
    renderAt('/workflows?run=run-1')

    expect(await screen.findByText('Two hosts were beaconing.')).toBeInTheDocument()
    expect(workflowApi.getRun).toHaveBeenCalledWith('run-1')
    expect(screen.queryByText('Beacon hunt')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /All workflows/ }))
    expect(await screen.findByText('Beacon hunt')).toBeInTheDocument()
    expect(screen.getByTestId('search').textContent).toBe('')
    expect(screen.queryByText('Two hosts were beaconing.')).not.toBeInTheDocument()
  })

  it('says the run could not be loaded rather than leaving the screen blank, and stops asking', async () => {
    // Only the interval is faked, so the fetch promise and findBy* keep real timers.
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    vi.mocked(workflowApi.getRun).mockRejectedValue({ response: { status: 404 } })
    renderAt('/workflows?run=missing')

    expect(await screen.findByText(/Couldn’t load run missing/)).toBeInTheDocument()

    // With no seed status the hook would treat the run as in flight; an unknown
    // run must not be polled every five seconds for as long as the tab is open.
    const before = vi.mocked(workflowApi.getRun).mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
    expect(vi.mocked(workflowApi.getRun).mock.calls.length).toBe(before)
  })

  it('leaves the catalog alone when no run is named', async () => {
    renderAt('/workflows')

    expect(await screen.findByText('Beacon hunt')).toBeInTheDocument()
    expect(workflowApi.getRun).not.toHaveBeenCalled()
  })
})
