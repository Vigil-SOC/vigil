/* The Watch a run page's right-hand columns: what each panel says as of the step the player
   holds, the line each kind of run gives where it has nothing, and the checkpoint slot. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { WatchRun } from './WatchRun'
import type { WfRunDetail } from './runRead'
import { workflowApi } from '../../services/api'

vi.mock('../../services/api', () => ({
  workflowApi: { replayRun: vi.fn(), getReplay: vi.fn(), steer: vi.fn() },
}))

afterEach(() => { vi.mocked(workflowApi.replayRun).mockReset(); vi.mocked(workflowApi.getReplay).mockReset() })

const move = (iteration: number) => ({
  decision_id: `dec-${iteration}`, iteration, action: 'EXPAND', rationale: `why ${iteration}`, cost_usd: 0.01,
  created_at: `2026-10-06T13:${10 + iteration}:00Z`,
})
const link = (hypothesis_id: string, relation: string) => ({ bears_on: [{ hypothesis_id, relation }] })
const record = (iteration: number, extra = {}) => ({ evidence_id: `ev-${iteration}`, iteration, source_system: 's', summary: `found ${iteration}`, ...extra })

/** A hunt of three steps: evidence for h1 at 1 and 2, against it at 3 (a gap too), a critic verdict at 2. */
const hunt = (extra = {}) => ({
  run_id: 'run-1', status: 'completed', workflow_name: 'Hypothesis hunt',
  hunt: {
    status: 'completed', iteration: 3, evidence_count: 3, cost_usd: 4.4,
    budgets: { max_iterations: 8, max_cost_usd: 15 },
    scope: { tenant: 'A' },
    moves: [move(3), move(2), move(1)],
    hypotheses: [
      { hypothesis_id: 'h2', statement: 'A vendor scanner', status: 'disproven', provenance: 'base_rate', supports: 0, weakens: 2 },
      { hypothesis_id: 'h1', statement: 'The same operator moved', status: 'proven', provenance: 'operator', supports: 2, weakens: 1 },
    ],
    evidence: [
      record(3, { is_gap: true, gap_detail: 'VirusTotal timed out', ...link('h1', 'weakens') }),
      record(2, link('h1', 'supports')),
      record(1, link('h1', 'supports')),
    ],
    reviews: [{ iteration: 2, hypothesis_id: 'h1', survives: false, strongest_benign_explanation: 'A nightly backup job', model_id: 'm' }],
    ...extra,
  },
}) as unknown as WfRunDetail

// what the lead was shown before decision N: its beliefs still open, N steps of 8 and $N of $15 gone
beforeEach(() => {
  vi.mocked(workflowApi.getReplay).mockImplementation((_run, id) => {
    const n = Number(String(id).slice(4))
    return Promise.resolve({
      data: { decisions: [{
        decision_id: id,
        recorded: {
          hypotheses: [{ hypothesis_id: 'h1', statement: 'The same operator moved', status: n === 1 ? 'active' : 'inconclusive' }],
          budget_remaining: { iterations: 8 - n, cost_usd: 15 - n },
        },
      }] },
    } as never)
  })
})

const step = (n: number) => fireEvent.click(screen.getByRole('listitem', { name: new RegExp(`^Step ${n}:`) }))
/** A Limits row reads as one line: label then value. */
const limit = (label: string) => (screen.getByText(label).parentElement as HTMLElement).textContent

describe('a hunt, as of the selected step', () => {
  it('counts the evidence and reads the status the lead was shown, then the totals at the last step', async () => {
    render(<WatchRun d={hunt()} onBack={vi.fn()} />)

    // step 1: one belief on the board, one record for it, nothing asked, nothing missed
    expect(await screen.findByText('active')).toBeInTheDocument()
    expect(screen.getByText('1 for · 0 against')).toBeInTheDocument()
    expect(screen.queryByText('A vendor scanner')).not.toBeInTheDocument()
    expect(limit('Budget')).toBe('Budget$1.00 of $15.00')
    expect(screen.getByText('Not asked yet.')).toBeInTheDocument()
    expect(screen.getByText('None so far.')).toBeInTheDocument()

    // step 2: a second record, the status moved with the digest, the critic has spoken
    step(2)
    expect(await screen.findByText('inconclusive')).toBeInTheDocument()
    expect(screen.getByText('2 for · 0 against')).toBeInTheDocument()
    expect(screen.queryByText('Not asked yet.')).not.toBeInTheDocument()
    expect(screen.getByText(/A nightly backup job/)).toBeInTheDocument()
    expect(screen.getByText('Did not stand')).toBeInTheDocument()
    expect(within(screen.getByText('Reviewer').parentElement as HTMLElement).getByText('model text')).toBeInTheDocument()
    expect(screen.getByText('None so far.')).toBeInTheDocument()

    // the last step: the projection's totals, both beliefs, their provenance, and the gap that came at step 3
    step(3)
    expect(await screen.findByText('proven')).toBeInTheDocument()
    expect(screen.getByText('2 for · 1 against')).toBeInTheDocument()
    expect(screen.getByText('0 for · 2 against')).toBeInTheDocument()
    expect(screen.getByText('yours')).toBeInTheDocument()
    expect(screen.getByText('the claim to beat')).toBeInTheDocument()
    expect(screen.getByText('found 3')).toBeInTheDocument()
    expect(screen.getByText('VirusTotal timed out')).toBeInTheDocument()
    expect(limit('Budget')).toBe('Budget$4.40 of $15.00')
    expect(limit('Steps')).toBe('Steps3 of 8')
  })

  it('reads Scope from the scope_extension checkpoints, and asks nothing of the record it lacks', async () => {
    const checkpoint = (answer?: string) => ({ checkpoint_id: 'c1', class: 'scope_extension', raised_iteration: 2, question: 'q', ...(answer ? { resolution: { answer, actor: 'me' } } : {}) })
    const scopeOf = () => within(screen.getByText('Scope').parentElement as HTMLElement).getByText(/tenant A|Not recorded/)

    const { unmount } = render(<WatchRun d={hunt()} onBack={vi.fn()} />)
    step(3)
    expect(scopeOf()).toHaveTextContent(/^tenant A$/)
    unmount()

    const raised = render(<WatchRun d={hunt({ report: { gaps: [], checkpoints: [checkpoint()], hypotheses: [] } })} onBack={vi.fn()} />)
    step(3)
    expect(scopeOf()).toHaveTextContent('tenant A (at limit)')
    raised.unmount()

    const approved = render(<WatchRun d={hunt({ report: { gaps: [], checkpoints: [checkpoint('approve')], hypotheses: [] } })} onBack={vi.fn()} />)
    step(3)
    expect(scopeOf()).toHaveTextContent('tenant A (extended)')
    step(2)
    expect(scopeOf()).toHaveTextContent('tenant A (at limit)')
    approved.unmount()

    // a hunt with no scope on record shows no scope, and a run before the budgets were recorded no ceiling
    render(<WatchRun d={hunt({ scope: undefined, budgets: undefined, reviews: undefined })} onBack={vi.fn()} />)
    step(3)
    expect(scopeOf()).toHaveTextContent('Not recorded')
    expect(limit('Budget')).toBe('Budget$4.40 spent')
    expect(screen.getByText('Not asked yet.')).toBeInTheDocument()
  })

  it('renders the existing checkpoint in the explanations column when one is open', () => {
    const open = { checkpoint_id: 'c1', checkpoint_class: 'scope_extension', question: 'Let the hunt search tenant B?' }
    render(<WatchRun d={hunt({ status: 'parked', open_checkpoint: open })} onBack={vi.fn()} />)
    step(3)
    const column = screen.getByText('Explanations being tested').parentElement as HTMLElement
    expect(within(column).getByText('Waiting on you · scope_extension')).toBeInTheDocument()
    expect(within(column).getByText('Let the hunt search tenant B?')).toBeInTheDocument()
    expect(within(column).getByRole('button', { name: 'approve' })).toBeInTheDocument()
    expect(screen.getByText(/tenant A \(at limit\)/)).toBeInTheDocument()
  })
})

describe('what each kind of run says where it has nothing', () => {
  const NO_EXPLANATIONS = 'This kind of run tests no explanations.'
  const NO_REVIEWER = 'No reviewer runs on this kind of run yet.'

  it('investigate: spend follows the step, steps have no limit, scope is not tracked', async () => {
    vi.mocked(workflowApi.replayRun).mockResolvedValue({
      data: { run_kind: 'investigate', decisions: [
        { iteration: 1, action: 'search', rationale: 'a', cost_usd: 1, calls: [] },
        { iteration: 2, action: 'stop', rationale: 'b', cost_usd: 2, calls: [] },
      ] },
    } as never)
    const d = {
      run_id: 'run-2', status: 'completed', workflow_name: 'Alert triage',
      projection: { run_kind: 'investigate', budgets: { max_cost_usd: 10 }, cost_usd: 3, gaps: [] },
    } as unknown as WfRunDetail
    render(<WatchRun d={d} onBack={vi.fn()} />)

    expect(await screen.findByText('1 decision · no step limit on this kind')).toBeInTheDocument()
    expect(limit('Budget')).toBe('Budget$1.00 of $10.00')
    expect(screen.getByText(NO_EXPLANATIONS)).toBeInTheDocument()
    expect(screen.getByText('Not tracked for this kind.')).toBeInTheDocument()
    expect(screen.getByText(NO_REVIEWER)).toBeInTheDocument()
    expect(screen.getByText('None so far.')).toBeInTheDocument()
    expect(screen.getByText('Sources this deployment lacks are not recorded for investigations yet.')).toBeInTheDocument()
    step(2)
    expect(screen.getByText('2 decisions · no step limit on this kind')).toBeInTheDocument()
    expect(limit('Budget')).toBe('Budget$3.00 of $10.00')
  })

  it('root cause: budget from its ceiling, notices and failed searches as blind spots', () => {
    const d = {
      run_id: 'run-3', status: 'completed', workflow_name: 'Root cause',
      projection: {
        run_kind: 'root_cause', cost_usd: 0.5, max_cost_usd: 2, notices: ['No flow logs in tenant B.'],
        recent_searches: [{ tool: 'splunk', args: 'index=a', rows: 0, failed: true }, { tool: 'ok', args: '', rows: 3, failed: false }],
      },
    } as unknown as WfRunDetail
    render(<WatchRun d={d} onBack={vi.fn()} />)
    expect(limit('Budget')).toBe('Budget$0.50 of $2.00')
    expect(screen.getByText('No flow logs in tenant B.')).toBeInTheDocument()
    expect(screen.getByText('A search failed: splunk')).toBeInTheDocument()
    expect(screen.queryByText(/A search failed: ok/)).not.toBeInTheDocument()
    expect(screen.getByText(NO_EXPLANATIONS)).toBeInTheDocument()
    expect(screen.getByText(NO_REVIEWER)).toBeInTheDocument()
  })

  it('compose: every panel says it is not recorded or not tracked', () => {
    render(<WatchRun d={{ run_id: 'run-4', status: 'completed', workflow_name: 'Compose', projection: { results: [] } } as unknown as WfRunDetail} onBack={vi.fn()} />)
    expect(screen.getAllByText('Not tracked for this kind.')).toHaveLength(3)
    expect(screen.getByText(NO_EXPLANATIONS)).toBeInTheDocument()
    expect(screen.getByText(NO_REVIEWER)).toBeInTheDocument()
    expect(screen.getByText('Not recorded for this kind of run yet.')).toBeInTheDocument()
  })
})
