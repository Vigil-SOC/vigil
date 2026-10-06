/* The Watch a run page: what each kind of run reduces to, how the player steps,
   and which mark a step wears from where the cursor is. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { WatchRun, STEP_MS, UNSUPPORTED } from './WatchRun'
import { callFailure, type WfRunDetail } from './runRead'
import { workflowApi } from '../../services/api'

vi.mock('../../services/api', () => ({
  workflowApi: { replayRun: vi.fn() },
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

  it('reads its decisions from the replay, and marks a step whose call failed', async () => {
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
    expect(screen.getByRole('img', { name: 'Failed' })).toBeInTheDocument()
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

describe('a run that is neither', () => {
  it('opens with the header and the limits, and one line where the player would be', () => {
    render(<WatchRun d={{ run_id: 'run-3', status: 'completed', workflow_name: 'Root cause', projection: { run_kind: 'root_cause' } } as WfRunDetail} onBack={vi.fn()} />)
    expect(screen.getByText(UNSUPPORTED)).toBeInTheDocument()
    expect(screen.getByText('Limits used')).toBeInTheDocument()
    expect(screen.queryByText('What the lead agent did')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /replay/i })).not.toBeInTheDocument()
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
