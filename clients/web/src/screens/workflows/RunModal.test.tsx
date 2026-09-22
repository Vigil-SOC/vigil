/* The run modal. Everything here is a fact the deployment already knew and the
   console used to withhold until after the money was spent: what the run will
   cost at most, what it will not be able to look at, and where it went. */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { RunModal } from './WorkflowsScreen'

const execute = vi.fn(() => Promise.resolve({ data: { run_id: 'run-abc12345' } }))
const getWorkflow = vi.fn()
const getRun = vi.fn(() => Promise.resolve({ data: { run_id: 'run-abc12345', status: 'running' } }))
const steer = vi.fn(() => Promise.resolve({ data: {} }))
const checkCoverage = vi.fn()

vi.mock('../../services/api', () => ({
  workflowApi: {
    execute: (...a: unknown[]) => execute(...(a as [])),
    get: (...a: unknown[]) => getWorkflow(...(a as [])),
    getRun: (...a: unknown[]) => getRun(...(a as [])),
    steer: (...a: unknown[]) => steer(...(a as [])),
    checkCoverage: (...a: unknown[]) => checkCoverage(...(a as [])),
    cancelRun: vi.fn(() => Promise.resolve({ data: {} })),
  },
  agentsApi: { listAgents: vi.fn(() => Promise.resolve({ data: { agents: [] } })) },
  findingsApi: { getAll: vi.fn(() => Promise.resolve({ data: { findings: [] } })) },
  casesApi: { getAll: vi.fn(() => Promise.resolve({ data: { cases: [] } })) },
}))
vi.mock('../../services/skillsApi', () => ({
  skillsApi: { list: vi.fn(() => Promise.resolve([])) },
}))

// huntLike is the backend's own answer about the kind, which is what the dialog
// gates on. Defaulted from the kind here so a case that only cares about one of
// them says one thing.
const wf = (runKind = 'hunt', huntLike = runKind === 'hunt' || runKind === 'root_cause') => ({
  id: 'threat-hunt', icon: 'flow' as const, name: 'Threat Hunt', desc: '',
  agents: [], cmds: [], source: 'file', useCase: '', runKind, huntLike,
})

const limits = (unbound: string[], source = 'heuristic') => ({
  data: {
    capabilities: { bound: ['findings_search'], unbound },
    budgets: { max_iterations: 8, max_cost_usd: 3.0 },
    pricing: { model: 'vertex/gemini-3.5-flash', source },
  },
})

// The coverage panel links to ?run=<id> through the router, so the modal renders inside one.
const open = (runKind = 'hunt') =>
  render(
    <MemoryRouter>
      <RunModal wf={wf(runKind)} onStarted={() => {}} onClose={() => {}} />
    </MemoryRouter>,
  )

/* A cost ceiling nothing can measure is not a ceiling, so the hunt stops after its
   first few calls. That refusal is right; arriving after the spend, naming neither the
   model nor the remedy, is not. */
describe('a model nothing can price', () => {
  it('says so before the run rather than three calls in', async () => {
    getWorkflow.mockResolvedValueOnce(limits([], 'unknown'))
    open()
    expect(await screen.findByText(/Nothing here can price/)).toBeInTheDocument()
    expect(screen.getByText(/vertex\/gemini-3.5-flash/)).toBeInTheDocument()
  })

  it('stays quiet when the model has a rate', async () => {
    getWorkflow.mockResolvedValueOnce(limits([], 'heuristic'))
    open()
    await waitFor(() => expect(getWorkflow).toHaveBeenCalled())
    expect(screen.queryByText(/Nothing here can price/)).toBeNull()
  })
})

describe('what the hunt will not be able to see', () => {
  it('names an unbound capability before anything is spent', async () => {
    getWorkflow.mockResolvedValueOnce(limits(['telemetry_search']))
    open()

    expect(await screen.findByText(/No tool here answers telemetry_search/)).toBeInTheDocument()
  })

  // The distinction ADR 0015 exists for: without a SIEM the hunt proves nothing
  // whatever the estate looks like, and that reads identically to a clean estate.
  it('says a hunt without telemetry cannot corroborate anything', async () => {
    getWorkflow.mockResolvedValueOnce(limits(['telemetry_search']))
    open()

    expect(await screen.findByText(/a fact about this deployment, not about your estate/)).toBeInTheDocument()
  })

  it('says nothing at all when every capability bound', async () => {
    getWorkflow.mockResolvedValueOnce(limits([]))
    open()

    await waitFor(() => expect(getWorkflow).toHaveBeenCalled())
    expect(screen.queryByText(/No tool here answers/)).toBeNull()
  })
})

describe('what the run will cost', () => {
  it('states the ceiling rather than estimating a total', async () => {
    getWorkflow.mockResolvedValueOnce(limits([]))
    open()

    expect(await screen.findByText(/It stops at \$3\.00 whatever happens/)).toBeInTheDocument()
  })

  it('counts the turns the operator actually asked for', async () => {
    getWorkflow.mockResolvedValueOnce(limits([]))
    open()
    await screen.findByText(/It stops at/)

    fireEvent.change(screen.getByLabelText(/Iterations/), { target: { value: '3' } })

    expect(await screen.findByText(/^3 turn\(s\)/)).toBeInTheDocument()
  })

  it('sends the ceiling the operator typed, not the shipped one', async () => {
    getWorkflow.mockResolvedValueOnce(limits([]))
    open()
    await screen.findByText(/It stops at/)

    fireEvent.change(screen.getByLabelText('Context'), { target: { value: 'beaconing' } })
    fireEvent.change(screen.getByLabelText(/Hypothesis/), { target: { value: 'a host beacons' } })
    fireEvent.change(screen.getByLabelText(/Cost ceiling/), { target: { value: '25' } })
    expect(await screen.findByText(/It stops at \$25\.00 whatever happens/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Run workflow/ }))

    await waitFor(() => expect(execute).toHaveBeenCalledWith('threat-hunt', {
      context: 'beaconing', hypothesis: 'a host beacons', max_cost_usd: 25,
    }))
  })

  it('refuses to start on a ceiling that is not money', async () => {
    getWorkflow.mockResolvedValueOnce(limits([]))
    open()
    await screen.findByText(/It stops at/)

    fireEvent.change(screen.getByLabelText('Context'), { target: { value: 'beaconing' } })
    fireEvent.change(screen.getByLabelText(/Hypothesis/), { target: { value: 'a host beacons' } })
    fireEvent.change(screen.getByLabelText(/Cost ceiling/), { target: { value: '-3' } })

    expect(await screen.findByText(/above 0 and no more than 100/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Run workflow/ })).toBeDisabled()
  })

  it('offers no iterations field for a workflow that walks phases', async () => {
    getWorkflow.mockResolvedValueOnce({ data: {} })
    open('compose')

    await waitFor(() => expect(getWorkflow).toHaveBeenCalled())
    expect(screen.queryByLabelText(/Iterations/)).toBeNull()
  })

  /* root_cause runs the same hypothesis loop a hunt does. Asking by kind gave it the
     phase-walking dialog: the ceilings the operator typed were dropped on the way to
     the server, and nothing named an unbound tool before the spend. */
  it('gives a root-cause workflow the same ceilings and warnings a hunt gets', async () => {
    getWorkflow.mockResolvedValueOnce(limits(['telemetry_search']))
    open('root_cause')

    expect(await screen.findByLabelText(/Iterations/)).toBeInTheDocument()
    expect(screen.getByLabelText(/Cost ceiling/)).toBeInTheDocument()
    expect(screen.getByText(/telemetry_search/)).toBeInTheDocument()
  })
})

describe('after the run starts', () => {
  it('stays open on the run it started rather than closing on it', async () => {
    getWorkflow.mockResolvedValueOnce(limits([]))
    open()
    await screen.findByText(/It stops at/)

    fireEvent.change(screen.getByLabelText('Context'), { target: { value: 'beaconing on the finance subnet' } })
    fireEvent.change(screen.getByLabelText(/Hypothesis/), { target: { value: 'a host beacons' } })
    fireEvent.click(screen.getByRole('button', { name: /Run workflow/ }))

    expect(await screen.findByText(/It runs on the server whether this stays open or not/)).toBeInTheDocument()
    expect(await screen.findByText('run-abc1')).toBeInTheDocument()
  })
})

/* A hunt raises its approval checkpoint in the first second, so this modal is where
   the operator meets it. It printed the question and put the answer a screen away:
   the hunt sat parked looking like it offered no way to start. */
describe('the checkpoint the hunt raises before it can start', () => {
  const parked = {
    data: {
      run_id: 'run-abc12345',
      status: 'parked',
      hunt: {
        run_id: 'run-abc12345',
        status: 'parked',
        iteration: 0,
        evidence_count: 0,
        hypotheses: [],
        open_checkpoint: {
          checkpoint_id: 'cp-2950de71',
          checkpoint_class: 'hypothesis_approval',
          question: 'Approve and start this hunt on 8 hypothesis(es), 8 from your request?',
        },
      },
    },
  }

  const start = async () => {
    getWorkflow.mockResolvedValueOnce(limits([]))
    getRun.mockImplementation(() => Promise.resolve(parked))
    open()
    await screen.findByText(/It stops at/)
    fireEvent.change(screen.getByLabelText('Context'), { target: { value: 'beaconing on the finance subnet' } })
    fireEvent.change(screen.getByLabelText(/Hypothesis/), { target: { value: 'a host beacons' } })
    fireEvent.click(screen.getByRole('button', { name: /Run workflow/ }))
  }

  it('can be answered from here rather than only from the run panel', async () => {
    await start()

    expect(await screen.findByText(/Approve and start this hunt/)).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: 'approve' })).toBeInTheDocument()
  })

  it('sends the answer against the checkpoint it was asked about', async () => {
    await start()

    fireEvent.click(await screen.findByRole('button', { name: 'approve' }))

    await waitFor(() => expect(steer).toHaveBeenCalled())
    expect(steer.mock.calls[0]).toEqual([
      'run-abc12345',
      'approve',
      '',
      { checkpoint_id: 'cp-2950de71' },
    ])
  })
})

describe('a hunt tests what the operator states', () => {
  // Says so when Run is pressed rather than sitting dead through a sentence: a
  // disabled button beside a hint reading "required" argues with the form instead
  // of answering about the run.
  it('says what is missing on Run rather than while the field is being typed', async () => {
    getWorkflow.mockResolvedValueOnce(limits([]))
    open()
    await screen.findByText(/It stops at/)

    fireEvent.change(screen.getByLabelText('Context'), { target: { value: 'beaconing' } })
    expect(screen.getByRole('button', { name: /Run workflow/ })).not.toBeDisabled()

    execute.mockClear()
    fireEvent.click(screen.getByRole('button', { name: /Run workflow/ }))

    expect(await screen.findByText(/at least one in Hypothesis/)).toBeInTheDocument()
    expect(execute).not.toHaveBeenCalled()
  })

  it('starts once a belief is stated', async () => {
    getWorkflow.mockResolvedValueOnce(limits([]))
    open()
    await screen.findByText(/It stops at/)

    fireEvent.change(screen.getByLabelText('Context'), { target: { value: 'beaconing' } })
    fireEvent.change(screen.getByLabelText(/Hypothesis/), { target: { value: 'a host beacons' } })

    expect(screen.getByRole('button', { name: /Run workflow/ })).not.toBeDisabled()
  })

  // A phase-walking workflow states its own phases and needs no belief.
  it('asks a phase workflow for no hypothesis', async () => {
    getWorkflow.mockResolvedValueOnce({ data: {} })
    open('compose')
    await waitFor(() => expect(getWorkflow).toHaveBeenCalled())

    fireEvent.change(screen.getByLabelText('Context'), { target: { value: 'ransomware on HOST-42' } })

    expect(screen.getByRole('button', { name: /Run workflow/ })).not.toBeDisabled()
  })
})

// A pasted paragraph with hard wraps becomes one belief per wrapped line: half a
// sentence, a note to yourself, the criteria for the belief above. Nine beliefs
// where four were meant doubles the matrix every later view is built from — and
// the only place it is cheap to catch is before the run starts.
describe('what the hypothesis field will actually put on the board', () => {
  it('counts the beliefs the splitter will make', async () => {
    getWorkflow.mockResolvedValueOnce(limits([]))
    open()
    fireEvent.change(screen.getByPlaceholderText(/Credentials taken from HOST-42/), {
      target: { value: 'a host is beaconing out\nanother host is doing the same' },
    })

    expect(await screen.findByText('H1')).toBeInTheDocument()
    expect(screen.getByText('H2')).toBeInTheDocument()
    expect(screen.queryByText('H3')).toBeNull()
  })

  it('marks a line that reads as a fragment rather than a claim', async () => {
    getWorkflow.mockResolvedValueOnce(limits([]))
    open()
    fireEvent.change(screen.getByPlaceholderText(/Credentials taken from HOST-42/), {
      target: { value: 'A host is beaconing to 45.77.53.176 over HTTPS\nat a regular interval, and another host is too.' },
    })

    expect(await screen.findByText(/reads as a fragment/)).toBeInTheDocument()
  })

  it('says nothing when every line reads as a claim', async () => {
    getWorkflow.mockResolvedValueOnce(limits([]))
    open()
    fireEvent.change(screen.getByPlaceholderText(/Credentials taken from HOST-42/), {
      target: { value: 'A host is beaconing out.\nData left over DNS.' },
    })

    expect(await screen.findByText('H1')).toBeInTheDocument()
    expect(screen.queryByText(/reads as a fragment/)).toBeNull()
  })
})

/* Report in, one of three answers out. The backend already said running, concluded
   or uncovered and handed back an execute body; the console gave an operator holding
   a report no way to ask. Whatever the answer, only the Run button ever executes. */
describe('checking a report against what is already hunted', () => {
  const proposal = {
    hypothesis: 'Activity from the reported indicators ip:45.77.53.176 and techniques T1071 is present in the environment',
    hypothesis_subjects: { 'Activity from the reported indicators ip:45.77.53.176 and techniques T1071 is present in the environment': ['ip:45.77.53.176'] },
    approve_hypotheses: true,
  }
  const split = {
    keys: ['ip:45.77.53.176'], techniques: ['T1071'],
    matched_keys: ['ip:45.77.53.176'], unmatched_keys: [], matched_techniques: [], unmatched_techniques: ['T1071'],
  }

  const check = async (report = 'Beaconing to 45[.]77[.]53[.]176 via T1071') => {
    getWorkflow.mockResolvedValueOnce(limits([]))
    open()
    await screen.findByText(/It stops at/)
    fireEvent.change(screen.getByLabelText(/^Report/), { target: { value: report } })
    fireEvent.click(screen.getByRole('button', { name: 'Check coverage' }))
    await waitFor(() => expect(checkCoverage).toHaveBeenCalled())
  }

  beforeEach(() => { execute.mockClear(); steer.mockClear(); checkCoverage.mockReset() })

  it('lists the in-flight runs, each linked to ?run=, and extends one with the report', async () => {
    checkCoverage.mockResolvedValueOnce({ data: {
      ...split, status: 'running',
      in_flight: [{ run_id: 'run-11111111', status: 'running', matched_keys: ['ip:45.77.53.176'], matched_techniques: [] }],
    } })
    await check()

    const link = await screen.findByRole('link', { name: 'run-1111' })
    expect(link).toHaveAttribute('href', '/?run=run-11111111')
    expect(screen.getByText('T1071')).toBeInTheDocument() // the unmatched half stays visible

    fireEvent.click(screen.getByRole('button', { name: 'Extend run-1111' }))
    await waitFor(() => expect(steer).toHaveBeenCalledTimes(1))
    expect(steer.mock.calls[0]).toEqual(['run-11111111', 'extend', 'Beaconing to 45[.]77[.]53[.]176 via T1071'])
    expect(execute).not.toHaveBeenCalled()
  })

  // A check on typed subjects alone still answers running; an extend with no report is not a directive.
  it('cannot extend when there is no report to extend with', async () => {
    checkCoverage.mockResolvedValueOnce({ data: {
      ...split, status: 'running',
      in_flight: [{ run_id: 'run-11111111', status: 'running', matched_keys: ['ip:45.77.53.176'], matched_techniques: [] }],
    } })
    getWorkflow.mockResolvedValueOnce(limits([]))
    open()
    await screen.findByText(/It stops at/)
    fireEvent.change(screen.getByLabelText(/Hypothesis/), { target: { value: 'a host beacons' } })
    fireEvent.change(screen.getByLabelText('What H1 is about'), { target: { value: 'ip:45.77.53.176' } })
    fireEvent.click(screen.getByRole('button', { name: 'Check coverage' }))

    expect(await screen.findByRole('button', { name: 'Extend run-1111' })).toBeDisabled()
    expect(steer).not.toHaveBeenCalled()
  })

  it('lists the verdicts linked by origin_run_id and reopens from the proposal', async () => {
    checkCoverage.mockResolvedValueOnce({ data: {
      ...split, status: 'concluded', proposal,
      concluded: [
        { statement: 'the host beacons out', outcome: 'refuted', concluded_at: '2026-09-01T10:00:00Z', origin_run_id: 'run-22222222', matched_keys: ['ip:45.77.53.176'], matched_techniques: [] },
        { statement: 'an orphaned verdict', outcome: 'supported', concluded_at: null, origin_run_id: null, matched_keys: [], matched_techniques: [] },
      ],
    } })
    await check()

    expect(await screen.findByRole('link', { name: 'run-2222' })).toHaveAttribute('href', '/?run=run-22222222')
    expect(screen.getByText('an orphaned verdict')).toBeInTheDocument()
    expect(screen.getAllByRole('link')).toHaveLength(1)

    fireEvent.click(screen.getByRole('button', { name: 'Reopen' }))
    expect(screen.getByLabelText(/Hypothesis/)).toHaveValue(proposal.hypothesis)
    expect(screen.getByLabelText('What H1 is about')).toHaveValue('ip:45.77.53.176')
    expect(screen.getByRole('checkbox')).toBeChecked()
    expect(execute).not.toHaveBeenCalled()
  })

  it('sends the proposal body unchanged when Run follows Use proposal', async () => {
    checkCoverage.mockResolvedValueOnce({ data: { ...split, status: 'uncovered', proposal } })
    await check()
    expect(checkCoverage).toHaveBeenCalledWith({ report: 'Beaconing to 45[.]77[.]53[.]176 via T1071' })

    fireEvent.click(await screen.findByRole('button', { name: 'Use proposal' }))
    expect(execute).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText(/Iterations/), { target: { value: '3' } })
    fireEvent.click(screen.getByRole('button', { name: /Run workflow/ }))

    await waitFor(() => expect(execute).toHaveBeenCalledTimes(1))
    expect(execute.mock.calls[0]).toEqual(['threat-hunt', { ...proposal, iterations: 3 }])
    expect(steer).not.toHaveBeenCalled()
  })

  it('sends the subjects already typed as entity_keys', async () => {
    checkCoverage.mockResolvedValueOnce({ data: { ...split, status: 'uncovered', proposal } })
    getWorkflow.mockResolvedValueOnce(limits([]))
    open()
    await screen.findByText(/It stops at/)
    fireEvent.change(screen.getByLabelText(/Hypothesis/), { target: { value: 'a host beacons' } })
    fireEvent.change(screen.getByLabelText('What H1 is about'), { target: { value: 'host:dev-830, ip:10.0.0.1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Check coverage' }))

    await waitFor(() => expect(checkCoverage).toHaveBeenCalledWith({ entity_keys: ['host:dev-830', 'ip:10.0.0.1'] }))
  })

  it('shows a 400 as the modal error and leaves the form alone', async () => {
    checkCoverage.mockRejectedValueOnce({ response: { data: { detail: 'nothing to check: no entity keys or techniques were found' } } })
    getWorkflow.mockResolvedValueOnce(limits([]))
    open()
    await screen.findByText(/It stops at/)
    fireEvent.change(screen.getByLabelText(/Hypothesis/), { target: { value: 'a host beacons' } })
    fireEvent.change(screen.getByLabelText(/^Report/), { target: { value: 'nothing useful here' } })
    fireEvent.click(screen.getByRole('button', { name: 'Check coverage' }))

    expect(await screen.findByText(/nothing to check: no entity keys/)).toBeInTheDocument()
    expect(screen.getByLabelText(/Hypothesis/)).toHaveValue('a host beacons')
    expect(screen.getByRole('checkbox')).not.toBeChecked()
    expect(screen.queryByTestId('coverage-answer')).toBeNull()
  })

  it('offers no report field to a workflow that walks phases', async () => {
    getWorkflow.mockResolvedValueOnce({ data: {} })
    open('compose')
    await waitFor(() => expect(getWorkflow).toHaveBeenCalled())
    expect(screen.queryByLabelText(/^Report/)).toBeNull()
  })
})
