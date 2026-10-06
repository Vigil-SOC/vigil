/* `/workflows?run=<id>` is how a case's activity reaches the run it was written
   by (#951). The screen must open that run, as the Watch a run page, in place of
   the catalog, and clearing the param must give the catalog back without a reload.
   The catalog's "Watch it run" is the other way in. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import WorkflowsScreen from './WorkflowsScreen'
import { workflowApi } from '../../services/api'

vi.mock('../../services/api', () => ({
  workflowApi: {
    listAll: vi.fn(() => Promise.resolve({ data: { workflows: [{ id: 'wf-1', name: 'Beacon hunt', description: 'd', steps: [] }] } })),
    getRun: vi.fn(),
    listRuns: vi.fn(),
    replayRun: vi.fn(() => Promise.reject({ response: { status: 404 } })),
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
          element={<><WorkflowsScreen openChat={vi.fn()} go={vi.fn()} goSettings={vi.fn()} openCase={vi.fn()} setViewFull={vi.fn()} /><Search /></>}
        />
      </Routes>
    </MemoryRouter>,
  )

beforeEach(() => {
  vi.mocked(workflowApi.getRun).mockReset()
  vi.mocked(workflowApi.getRun).mockResolvedValue({
    data: { run_id: 'run-1', status: 'completed', workflow_name: 'Beacon hunt', workflow_version: 2, projection: { run_kind: 'root_cause' } },
  } as never)
  vi.mocked(workflowApi.listRuns).mockReset()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('/workflows?run=<id>', () => {
  it('opens that run in place of the catalog, and the back control clears the param', async () => {
    renderAt('/workflows?run=run-1')

    expect(await screen.findByRole('heading', { name: 'Watch it run · Beacon hunt' })).toBeInTheDocument()
    expect(screen.getByText(/Beacon hunt version 2/)).toBeInTheDocument()
    expect(workflowApi.getRun).toHaveBeenCalledWith('run-1')
    expect(screen.queryByText('Look for beacons')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Workflows' }))
    expect(await screen.findByText('Beacon hunt')).toBeInTheDocument()
    expect(screen.getByTestId('search').textContent).toBe('')
    expect(screen.queryByRole('heading', { name: /Watch it run/ })).not.toBeInTheDocument()
  })

  it('says a kind with no replay has none, rather than showing an empty player', async () => {
    renderAt('/workflows?run=run-1')

    expect(await screen.findByText('Replay isn’t available for this kind of run yet.')).toBeInTheDocument()
    expect(screen.getByText('Limits used')).toBeInTheDocument()
    expect(screen.queryByText('What the lead agent did')).not.toBeInTheDocument()
  })

  it('says the run could not be loaded rather than leaving the screen blank, and stops asking', async () => {
    // Only the interval is faked, so the fetch promise and findBy* keep real timers.
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    vi.mocked(workflowApi.getRun).mockRejectedValue({ response: { status: 404 } })
    renderAt('/workflows?run=missing')

    expect(await screen.findByText(/Couldn’t load run missing/)).toBeInTheDocument()

    // With no seed status the hook must not invent an in-flight run; an unknown
    // run is asked for once, not every five seconds for as long as the tab is open.
    const before = vi.mocked(workflowApi.getRun).mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
    expect(vi.mocked(workflowApi.getRun).mock.calls.length).toBe(before)
  })

  it('leaves the catalog alone when no run is named', async () => {
    renderAt('/workflows')

    expect(await screen.findByText('Beacon hunt')).toBeInTheDocument()
    expect(workflowApi.getRun).not.toHaveBeenCalled()
  })

  it('opens the latest run from the catalog row, looked up on the click', async () => {
    vi.mocked(workflowApi.listRuns).mockResolvedValue({ data: { runs: [{ run_id: 'run-9' }] } } as never)
    renderAt('/workflows')

    await screen.findByText('Beacon hunt')
    expect(workflowApi.listRuns).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: /Watch it run/ }))

    expect(await screen.findByRole('heading', { name: /Watch it run/ })).toBeInTheDocument()
    expect(workflowApi.listRuns).toHaveBeenCalledWith('wf-1', { limit: 1 })
    expect(workflowApi.getRun).toHaveBeenCalledWith('run-9')
    expect(screen.getByTestId('search').textContent).toBe('?run=run-9')
  })

  it('disables the catalog row, saying so, when the workflow has never run', async () => {
    vi.mocked(workflowApi.listRuns).mockResolvedValue({ data: { runs: [] } } as never)
    renderAt('/workflows')

    await screen.findByText('Beacon hunt')
    fireEvent.click(screen.getByRole('button', { name: /Watch it run/ }))

    const none = await screen.findByRole('button', { name: /No runs yet/ })
    expect(none).toBeDisabled()
    expect(workflowApi.getRun).not.toHaveBeenCalled()
  })

  it('opens a History row’s run, and closes History as it goes', async () => {
    vi.mocked(workflowApi.listRuns).mockResolvedValue({ data: { runs: [{ run_id: 'run-1', status: 'completed', triggered_by: 'mk' }] } } as never)
    renderAt('/workflows')

    await screen.findByText('Beacon hunt')
    fireEvent.click(screen.getByRole('button', { name: /History/ }))
    const link = await screen.findByRole('link', { name: /Watch it run/ })
    // the row's own click expands it; the link must not
    expect(workflowApi.getRun).not.toHaveBeenCalledWith('run-1')
    fireEvent.click(link)

    expect(await screen.findByRole('heading', { name: /Watch it run/ })).toBeInTheDocument()
    expect(screen.queryByText(/^History/)).not.toBeInTheDocument()
    expect(screen.getByTestId('search').textContent).toBe('?run=run-1')
  })
})
