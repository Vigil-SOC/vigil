/* The Watch a run page: what each kind of run reduces to, how the player steps,
   and which mark a step wears from where the cursor is. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { WatchRun, STEP_MS, UNSUPPORTED, PLAYBOOK } from './WatchRun'
import { callFailure, type WfRunDetail } from './runRead'
import { workflowApi } from '../../services/api'

vi.mock('../../services/api', () => ({
  workflowApi: { replayRun: vi.fn(), getReplay: vi.fn(), steer: vi.fn() },
}))

const move = (iteration: number, extra = {}) => ({
  decision_id: `dec-${iteration}`,
  iteration,
  action: iteration === 1 ? 'INVESTIGATE' : 'EXPAND',
  rationale: `why ${iteration}`,
  worker_agent_id: `worker-${iteration}`,
  cost_usd: 0.01 * iteration,
  created_at: `2026-10-06T13:${10 + iteration}:00Z`,
  ...extra,
})

// newest first, as the projection sends them
const hunt = (status: string, moves = [move(3), move(2), move(1)], extra = {}) =>
  ({
    run_id: 'run-1', status, workflow_name: 'Hypothesis hunt', workflow_version: 3,
    hunt: { status, iteration: moves.length, evidence_count: 0, hypotheses: [], moves, ...extra },
  }) as unknown as WfRunDetail

const caption = () => screen.getByText(/^Step \d+ of \d+/).textContent
const tick = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms) })

beforeEach(() => { vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] }) })
afterEach(() => { vi.useRealTimers(); vi.mocked(workflowApi.replayRun).mockReset() })

describe('the header', () => {
  it('names the workflow and its version, and says so when the version was not recorded', () => {
    const { unmount } = render(<WatchRun d={hunt('completed')} onBack={vi.fn()} />)
    expect(screen.getByRole('heading', { name: 'Watch it run · Hypothesis hunt' })).toBeInTheDocument()
    expect(screen.getByText(/Hypothesis hunt version 3/)).toBeInTheDocument()
    unmount()

    render(<WatchRun d={{ ...hunt('completed'), workflow_version: null }} onBack={vi.fn()} />)
    expect(screen.getByText(/version not recorded/)).toBeInTheDocument()
  })

  it('names the workflow by its display name, and does not end on a hunt name that is the workflow id', () => {
    render(<WatchRun d={{ ...hunt('completed', [move(1)], { name: 'threat-hunt' }), workflow_name: 'threat-hunt', trigger_context: { case_id: 'c-1' } } as unknown as WfRunDetail} onBack={vi.fn()} />)
    // the run id sits in its own span, so read the line whole
    const line = screen.getByText('run-1'.slice(0, 8)).parentElement as HTMLElement
    expect(line.textContent).toBe('Run run-1 · Threat hunt version 3 · case c-1. Replayed step by step from the record.')
  })
})

describe('a hunt', () => {
  it('lays the moves out in ledger order and starts a finished run at step 1, paused', async () => {
    render(<WatchRun d={hunt('completed')} onBack={vi.fn()} />)

    expect(caption()).toBe('Step 1 of 3 · Investigate')
    const cards = screen.getAllByTitle(/^Go to step/)
    expect(cards.map((c) => c.textContent)).toEqual([
      expect.stringContaining('Investigate'), expect.stringContaining('Expand'), expect.stringContaining('Expand'),
    ])
    expect(cards[0]).toHaveTextContent('13:11')
    // the cursor step is done; the ones past it are pending, and say nothing yet
    expect(screen.getAllByRole('img', { name: 'Done' })).toHaveLength(1)
    expect(screen.getAllByRole('img', { name: 'Not started' })).toHaveLength(2)
    expect(screen.queryByText('why 2')).not.toBeInTheDocument()
    await tick(STEP_MS * 3)
    expect(caption()).toBe('Step 1 of 3 · Investigate')
  })

  it('plays on a timer, stops at the last step, and replays from the start', async () => {
    render(<WatchRun d={hunt('completed')} onBack={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: 'Play the replay' }))
    await tick(STEP_MS)
    expect(caption()).toBe('Step 2 of 3 · Expand')
    await tick(STEP_MS)
    expect(caption()).toBe('Step 3 of 3 · Expand')
    await tick(STEP_MS * 2)
    expect(caption()).toBe('Step 3 of 3 · Expand')
    expect(screen.getAllByRole('img', { name: 'Done' })).toHaveLength(3)

    fireEvent.click(screen.getByRole('button', { name: 'Replay from the start' }))
    expect(caption()).toBe('Step 1 of 3 · Investigate')
  })

  it('pauses, and a segment jumps to its step', async () => {
    render(<WatchRun d={hunt('completed')} onBack={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: 'Play the replay' }))
    await tick(STEP_MS)
    fireEvent.click(screen.getByRole('button', { name: 'Pause the replay' }))
    await tick(STEP_MS * 3)
    expect(caption()).toBe('Step 2 of 3 · Expand')

    fireEvent.click(screen.getByRole('listitem', { name: 'Step 3: Expand' }))
    expect(caption()).toBe('Step 3 of 3 · Expand')
  })

  it('draws the newest step of a run in flight as running, and follows it', () => {
    const { rerender } = render(<WatchRun d={hunt('running', [move(2), move(1)])} onBack={vi.fn()} />)
    expect(caption()).toBe('Step 2 of 2 · Expand')
    expect(screen.getAllByRole('img', { name: 'Working on it' })).toHaveLength(1)
    expect(screen.getAllByRole('img', { name: 'Done' })).toHaveLength(1)

    rerender(<WatchRun d={hunt('running', [move(3), move(2), move(1)])} onBack={vi.fn()} />)
    expect(caption()).toBe('Step 3 of 3 · Expand')

    // finished: nothing is running any more
    rerender(<WatchRun d={hunt('completed', [move(3), move(2), move(1)])} onBack={vi.fn()} />)
    expect(screen.queryByRole('img', { name: 'Working on it' })).not.toBeInTheDocument()
  })

  it('draws a paused run waiting on a person: warn mark, stopped caption, waiting clock, no spinner', () => {
    const open = { checkpoint_id: 'c1', checkpoint_class: 'scope_extension', question: 'Widen?' }
    render(<WatchRun d={hunt('paused', [move(2), move(1)], { open_checkpoint: open })} onBack={vi.fn()} />)
    expect(screen.getByText('Stopped · needs you', { selector: 'span.truncate' })).toBeInTheDocument()
    expect(screen.getByText(/13:12 · waiting on you/)).toBeInTheDocument()
    expect(screen.getAllByRole('img', { name: 'Needs you' })).toHaveLength(1)
    expect(screen.queryByRole('img', { name: 'Working on it' })).not.toBeInTheDocument()
    expect(screen.queryByText('Working on it…')).not.toBeInTheDocument()
    expect(screen.queryByText(/in progress/)).not.toBeInTheDocument()
    // the stopped step still says what it did
    expect(screen.getByText('why 2')).toBeInTheDocument()
  })

  it('reads a paused run with no checkpoint as paused, with its reason, not as needing you', () => {
    render(<WatchRun d={hunt('paused', [move(2), move(1)], { reason: 'waiting for the vendor feed' })} onBack={vi.fn()} />)
    expect(screen.getByText('Paused · waiting for the vendor feed')).toBeInTheDocument()
    expect(screen.getByText(/13:12 · paused/)).toBeInTheDocument()
    expect(screen.getAllByRole('img', { name: 'Paused' })).toHaveLength(1)
    expect(screen.queryByRole('img', { name: 'Needs you' })).not.toBeInTheDocument()
    expect(screen.queryByText(/in progress/)).not.toBeInTheDocument()
  })

  it('shows a parked hunt as paused though its run row still says running', () => {
    render(<WatchRun d={hunt('running', [move(2), move(1)], { status: 'parked', reason: 'budget reached' })} onBack={vi.fn()} />)
    expect(screen.getByText('Paused · budget reached')).toBeInTheDocument()
    expect(screen.queryByRole('img', { name: 'Working on it' })).not.toBeInTheDocument()
    expect(screen.queryByRole('img', { name: 'Needs you' })).not.toBeInTheDocument()
    expect(screen.queryByText(/in progress/)).not.toBeInTheDocument()
  })

  it('shows a failed run as stopped with its error, the last step failed and the error labelled as one', () => {
    const d = { ...hunt('failed', [move(2), move(1)]), error: 'agent service unreachable' }
    render(<WatchRun d={d} onBack={vi.fn()} />)
    fireEvent.click(screen.getByRole('listitem', { name: /^Step 2:/ }))
    expect(screen.getByText('Stopped · agent service unreachable', { selector: 'span.truncate' })).toBeInTheDocument()
    expect(screen.getAllByRole('img', { name: 'Failed' })).toHaveLength(1)
    expect(screen.getByText('agent service unreachable', { selector: 'span.line-clamp-2, span.break-words' })).toBeInTheDocument()
    // the model's own words keep their label, the error gets its own
    expect(screen.getAllByText('model text')).toHaveLength(2)
    expect(screen.getAllByText('error')).toHaveLength(1)
  })

  it('labels a rationale that is only the run’s error as an error, not as model text', () => {
    const d = { ...hunt('failed', [move(1, { rationale: 'boom' })]), error: 'boom' }
    render(<WatchRun d={d} onBack={vi.fn()} />)
    expect(screen.queryByText('model text')).not.toBeInTheDocument()
    expect(screen.getByText('error')).toBeInTheDocument()
  })

  it('goes back to following a live run when the viewer scrubs to its newest step, and when playback ends there', async () => {
    const { rerender } = render(<WatchRun d={hunt('running', [move(3), move(2), move(1)])} onBack={vi.fn()} />)
    fireEvent.click(screen.getByRole('listitem', { name: /^Step 1:/ }))
    rerender(<WatchRun d={hunt('running', [move(4), move(3), move(2), move(1)])} onBack={vi.fn()} />)
    expect(caption()).toBe('Step 1 of 4 · Investigate')

    fireEvent.click(screen.getByRole('listitem', { name: /^Step 4:/ }))
    rerender(<WatchRun d={hunt('running', [move(5), move(4), move(3), move(2), move(1)])} onBack={vi.fn()} />)
    expect(caption()).toBe('Step 5 of 5 · Expand')

    fireEvent.click(screen.getByRole('listitem', { name: /^Step 4:/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Play the replay' }))
    await tick(STEP_MS)
    rerender(<WatchRun d={hunt('running', [move(6), move(5), move(4), move(3), move(2), move(1)])} onBack={vi.fn()} />)
    expect(caption()).toBe('Step 6 of 6 · Expand')
  })

  it('leaves a hunt step done when a call failed, and its line reads timed out', () => {
    const calls = [{ question: 'q', tool: 'virustotal', result_length: 40, cost_usd: 0, duration_ms: 30000, iteration: 1, failed: 'timeout' }]
    render(<WatchRun d={hunt('completed', [move(1, { duration_ms: 900 })], { calls })} onBack={vi.fn()} />)
    expect(screen.queryByRole('img', { name: 'Failed' })).not.toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'Done' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Thought for 900ms/ }))
    expect(screen.getByText(/timed out/)).toBeInTheDocument()
  })

  it('says when older steps were dropped, and counts only what is shown', () => {
    const d = hunt('completed', [move(52), move(51)], { iteration: 52 })
    render(<WatchRun d={d} onBack={vi.fn()} />)
    expect(screen.getByText('Showing the last 2 steps')).toBeInTheDocument()
    expect(caption()).toBe('Step 1 of 2 · Expand')
  })

  it('ties calls to the move that asked for them, and does not claim none when it cannot tell', () => {
    const calls = [
      { question: 'q', tool: 'telemetry_search', result_length: 1234, cost_usd: 0, duration_ms: 80, iteration: 2 },
    ]
    const { unmount } = render(<WatchRun d={hunt('completed', [move(2, { duration_ms: 1300 }), move(1, { duration_ms: 900 })], { calls })} onBack={vi.fn()} />)
    fireEvent.click(screen.getByRole('listitem', { name: 'Step 2: Expand' }))
    fireEvent.click(screen.getByRole('button', { name: /Thought for 1\.3s/ }))
    expect(screen.getByText('telemetry_search')).toBeInTheDocument()
    expect(screen.getByText(/1,234 chars/)).toBeInTheDocument()
    expect(screen.getByText(/80ms/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('listitem', { name: 'Step 1: Investigate' }))
    fireEvent.click(screen.getByRole('button', { name: /Thought for 900ms/ }))
    expect(screen.getByText('No calls followed.')).toBeInTheDocument()
    unmount()

    // an agent service that does not stamp the iteration
    const old = [{ ...calls[0], iteration: undefined }]
    render(<WatchRun d={hunt('completed', [move(1, { duration_ms: 900 })], { calls: old })} onBack={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: /Thought for 900ms/ }))
    expect(screen.queryByText('No calls followed.')).not.toBeInTheDocument()
    expect(screen.getByText(/isn’t recorded/)).toBeInTheDocument()
  })

  it('says so in one line for a run with no steps, and shows no player', () => {
    render(<WatchRun d={hunt('completed', [])} onBack={vi.fn()} />)
    expect(screen.getByText('No steps were recorded for this run.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /replay/i })).not.toBeInTheDocument()
  })

  it('does not double the period when a stop reason already ends in one', () => {
    render(<WatchRun d={{ ...hunt('failed', []), error: 'ran out of turns, or abort.' } as unknown as WfRunDetail} onBack={vi.fn()} />)
    expect(screen.getByText('Stopped · ran out of turns, or abort. No steps were recorded for this run.')).toBeInTheDocument()
  })

  it('stops its timer when the page goes away', async () => {
    const { unmount } = render(<WatchRun d={hunt('completed')} onBack={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Play the replay' }))
    unmount()
    expect(vi.getTimerCount()).toBe(0)
  })
})

describe('an investigate run', () => {
  // findBy* polls on real timers
  beforeEach(() => { vi.useRealTimers() })
  const investigate = (status = 'completed') =>
    ({ run_id: 'run-2', status, workflow_name: 'Alert triage', projection: { run_kind: 'investigate' } }) as unknown as WfRunDetail

  it('reads its decisions from the replay; a step whose call failed stays done and the failure shows on the call', async () => {
    vi.mocked(workflowApi.replayRun).mockResolvedValue({
      data: {
        run_kind: 'investigate',
        decisions: [
          { iteration: 1, action: 'validate', worker: 'check-reputation', at: '2026-10-06T13:26:00Z', rationale: 'check it', cost_usd: 0.002, duration_ms: 900,
            calls: [{ tool: 'virustotal', arguments: '{}', result: '<vigil:tool_result tool="virustotal">\nfailed: timeout -- after 30000ms\n</vigil:tool_result>', duration_ms: 30000 }] },
          { iteration: 2, action: 'stop', rationale: 'done', cost_usd: 0, calls: [] },
        ],
      },
    } as never)
    render(<WatchRun d={investigate()} onBack={vi.fn()} />)

    expect(await screen.findByText('Step 1 of 2 · Validate')).toBeInTheDocument()
    expect(screen.queryByRole('img', { name: 'Failed' })).not.toBeInTheDocument()
    expect(screen.getByText('check-reputation')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Thought for 900ms/ }))
    const line = screen.getByText('virustotal').parentElement as HTMLElement
    expect(within(line).getByText(/timed out/)).toBeInTheDocument()
    expect(line).toHaveTextContent('30.0s')
  })

  it('falls back to the call count when the record has no thinking time', async () => {
    vi.mocked(workflowApi.replayRun).mockResolvedValue({
      data: { run_kind: 'investigate', decisions: [{ iteration: 1, action: 'search', rationale: 'r', cost_usd: 0, calls: [{ tool: 'a' }, { tool: 'b' }] }] },
    } as never)
    render(<WatchRun d={investigate()} onBack={vi.fn()} />)
    expect(await screen.findByRole('button', { name: /2 calls/ })).toBeInTheDocument()
    expect(screen.queryByText(/Thought for/)).not.toBeInTheDocument()
  })

  it('says the replay is not there when the agent has none', async () => {
    vi.mocked(workflowApi.replayRun).mockRejectedValue({ response: { status: 404 } })
    render(<WatchRun d={investigate()} onBack={vi.fn()} />)
    expect(await screen.findByText(UNSUPPORTED)).toBeInTheDocument()
  })
})

describe('a root-cause run', () => {
  beforeEach(() => { vi.useRealTimers() })
  const trace = (status = 'completed') => ({ run_id: 'run-3', status, workflow_name: 'Root cause', projection: { run_kind: 'root_cause' } }) as unknown as WfRunDetail

  it('plays its searches, steps and notices as cards, and marks a failed search', async () => {
    vi.mocked(workflowApi.replayRun).mockResolvedValue({
      data: {
        run_kind: 'root_cause',
        steps: [
          { kind: 'search', recorded_at: '2026-10-06T13:01:00Z', tool: 'splunk', args: 'index=a | head', rows: 3, failed: false },
          { kind: 'search', recorded_at: '2026-10-06T13:02:00Z', tool: 'splunk', args: 'index=b', rows: 0, failed: true, failure: 'timeout' },
          { kind: 'step', recorded_at: '2026-10-06T13:03:00Z', step_id: 'step-1', event: 'beacon out', who: 'host-a', at: '2026-10-06T12:00:00Z', link: '', cause_id: 'step-0', origin: false, link_status: 'unproven', origin_status: 'none' },
          { kind: 'notice', recorded_at: '2026-10-06T13:04:00Z', text: 'No flow logs.' },
          { kind: 'mystery', recorded_at: 'x' },
        ],
      },
    } as never)
    render(<WatchRun d={trace()} onBack={vi.fn()} />)

    expect(await screen.findByText('Step 1 of 4 · Search')).toBeInTheDocument()
    expect(screen.queryByText(UNSUPPORTED)).not.toBeInTheDocument()
    expect(screen.getByText('What the lead agent did')).toBeInTheDocument()
    expect(screen.getByText('index=a | head')).toBeInTheDocument()
    expect(screen.getByText(/3 rows/)).toBeInTheDocument()
    // the cursor step is done, the rest say nothing yet
    expect(screen.getAllByRole('img', { name: 'Not started' })).toHaveLength(3)
    expect(screen.queryByText('Recorded step-1 · beacon out')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('listitem', { name: 'Step 2: Search' }))
    expect(screen.getByRole('img', { name: 'Failed' })).toBeInTheDocument()
    expect(screen.getByText(/timed out/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('listitem', { name: 'Step 3: Record' }))
    expect(screen.getByText('Recorded step-1 · beacon out')).toBeInTheDocument()
    expect(screen.getByText('host-a')).toBeInTheDocument()
    expect(screen.getByText('caused by step-0')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('listitem', { name: 'Step 4: Notice' }))
    expect(screen.getAllByText('No flow logs.').length).toBeGreaterThan(0)
  })

  it('says so when the record has no steps, and falls back to the run as it stands when there is no replay', async () => {
    vi.mocked(workflowApi.replayRun).mockResolvedValue({ data: { run_kind: 'root_cause', steps: [] } } as never)
    const { unmount } = render(<WatchRun d={trace()} onBack={vi.fn()} />)
    expect(await screen.findByText('No steps were recorded for this run.')).toBeInTheDocument()
    unmount()

    vi.mocked(workflowApi.replayRun).mockRejectedValue({ response: { status: 404 } })
    render(<WatchRun d={trace()} onBack={vi.fn()} />)
    expect(await screen.findByText(UNSUPPORTED)).toBeInTheDocument()
    expect(screen.getByText('Limits used')).toBeInTheDocument()
    expect(screen.queryByText('What the lead agent did')).not.toBeInTheDocument()
  })
})

describe('a run that is neither', () => {
  it('opens with the header and the limits, and one line where the player would be', () => {
    render(<WatchRun d={{ run_id: 'run-4', status: 'completed', workflow_name: 'Compose', projection: { results: [] } } as unknown as WfRunDetail} onBack={vi.fn()} />)
    expect(screen.getByText(PLAYBOOK)).toBeInTheDocument()
    expect(screen.getByText('Limits used')).toBeInTheDocument()
    expect(screen.queryByText('What the lead agent did')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /replay/i })).not.toBeInTheDocument()
  })
})

describe('the kind of a run', () => {
  const started = (extra = {}) => ({ run_id: 'run-5', status: 'running', workflow_name: 'threat-hunt', trigger_context: { run_kind: 'hunt', hypothesis: 'A\nB' }, ...extra }) as unknown as WfRunDetail

  it('is a hunt from the run row alone: Starting…, the hypothesis under a named heading, and Live', () => {
    render(<WatchRun d={started()} onBack={vi.fn()} />)
    expect(screen.getByText('Starting…')).toBeInTheDocument()
    expect(screen.queryByText(PLAYBOOK)).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Watch it run · Threat hunt' })).toBeInTheDocument()
    expect(screen.getByText('A · B')).toBeInTheDocument()
    expect(screen.getByText(/Live\./)).toBeInTheDocument()
    expect(screen.queryByText(/Replayed step by step/)).not.toBeInTheDocument()
  })

  it('treats adjudicate as hunt-like, and a run with no recorded kind and no projection as a playbook', () => {
    const { unmount } = render(<WatchRun d={started({ trigger_context: { run_kind: 'adjudicate' } })} onBack={vi.fn()} />)
    expect(screen.getByText('Starting…')).toBeInTheDocument()
    unmount()
    render(<WatchRun d={started({ trigger_context: {} })} onBack={vi.fn()} />)
    expect(screen.getByText(PLAYBOOK)).toBeInTheDocument()
  })

  it('says replayed once the run has ended, and falls back to the beliefs put up when no hypothesis was typed', () => {
    const d = hunt('completed', [move(1)], { hypotheses: [{ hypothesis_id: 'h1', statement: 'The same operator moved', status: 'active', provenance: 'operator' }, { hypothesis_id: 'h2', statement: 'A scanner', status: 'active', provenance: 'base_rate' }] })
    render(<WatchRun d={d} onBack={vi.fn()} />)
    expect(screen.getByText(/Replayed step by step from the record\./)).toBeInTheDocument()
    expect(screen.getByRole('heading').nextElementSibling).toHaveTextContent('The same operator moved')
    expect(screen.getByRole('heading').nextElementSibling).not.toHaveTextContent('A scanner')
  })
})

describe('a playbook run', () => {
  it('says why there is nothing to watch and where to look, and does not claim a replay', () => {
    render(<WatchRun d={{ run_id: 'run-4', status: 'completed', workflow_name: 'Triage playbook' } as WfRunDetail} onBack={vi.fn()} />)
    expect(screen.getByText(PLAYBOOK)).toBeInTheDocument()
    expect(screen.queryByText(UNSUPPORTED)).not.toBeInTheDocument()
    expect(screen.queryByText(/Replayed step by step/)).not.toBeInTheDocument()
  })
})

describe('reading a failed call', () => {
  it('reads the kind from the wrapped result and nowhere else', () => {
    const wrapped = (body: string) => ({ result: `<vigil:tool_result tool="t">\n${body}\n</vigil:tool_result>` })
    expect(callFailure(wrapped('failed: timeout -- after 30000ms'))).toBe('timeout')
    expect(callFailure(wrapped('failed: backend_error -- 500'))).toBe('backend_error')
    expect(callFailure(wrapped('3 row(s) from splunk\n[]'))).toBeNull()
    // a row that merely says "failed:" is not a failure
    expect(callFailure(wrapped('1 row(s) from x\n[{"msg":"failed: timeout -- x"}]'))).toBeNull()
    expect(callFailure({ tool: 'x' })).toBeNull()
    expect(callFailure(null)).toBeNull()
  })
})
