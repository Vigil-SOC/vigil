/* The reader pane: every kind's shape, a stage filtering the panels, the enable
   switch, and a draft that has no payload. */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import WorkflowReaderPane from './WorkflowReaderPane'
import type { Workflow } from '../../data/appData'

const api = vi.hoisted(() => ({
  get: vi.fn(),
  preflight: vi.fn(),
  setEnabled: vi.fn(),
}))
vi.mock('../../services/api', () => ({
  workflowApi: api,
  agentsApi: {
    listAgents: vi.fn(() => Promise.resolve({ data: { agents: [
      { id: 'threat_hunter', name: 'Threat hunter', color: '#0a0' },
      { id: 'reporter', name: 'Reporter' },
      { id: 'triage', name: 'Triage' },
    ] } })),
  },
}))

const row = (over: Partial<Workflow>): Workflow => ({
  id: 'x', icon: 'flow', name: 'X', desc: '', agents: [], cmds: [], source: 'file', useCase: '',
  runKind: 'compose', huntLike: false, runs7d: 0, successRate: null, successLevel: null, meanCostUsd: null, enabled: true, canDisable: true, ...over,
})
const actions = { onBack: vi.fn(), onWatch: vi.fn(), onRun: vi.fn(), onEdit: vi.fn(), onDelete: vi.fn(), onToggled: vi.fn() }

const checkpoints = { hypothesis_approval: 'ask', scope_extension: 'auto', verdict_review: 'auto', budget_anomaly: 'ask' }
const hunt = {
  definition: { run_kind: 'hunt', hunt_like: true, version: 3, description: 'Hunt it', objectives: ['An explanation is proven'], phases: [] },
  preflight: {
    roles: {
      lead: { name: 'Hunt lead', tools: ['expand'] },
      helpers: [{ agent: 'threat_hunter', name: 'Behavioural hunting', tools: ['findings_search', 'telemetry_search'], approval_required: false }],
      reviewer: { name: 'Critic', tools: [] },
    },
    roles_note: null,
    model: 'Claude Sonnet 5',
    model_source: 'assignment',
    skills: ['query-splunk'],
    skills_note: null,
    permissions: [
      { name: 'expand', changes: 'on_its_own' },
      { name: 'findings_search', changes: 'on_its_own' },
      { name: 'telemetry_search', changes: 'on_its_own', bound: false },
      { name: 'HANDOFF_IR', label: 'Start incident response on a proven explanation', changes: 'asks_you' },
    ],
    permissions_note: null,
    budgets: { max_iterations: 12, max_cost_usd: 15, max_wall_ms: 5_400_000 },
    checkpoints,
    checkpoints_note: null,
  },
}

function serve(kind: { definition: object; preflight: object }) {
  api.get.mockResolvedValue({ data: kind.definition })
  api.preflight.mockResolvedValue({ data: kind.preflight })
}
const panel = (name: string) => screen.getByRole('region', { name })

beforeEach(() => {
  Object.values(api).forEach((m) => m.mockReset())
  Object.values(actions).forEach((m) => m.mockReset())
})

describe('workflow reader pane', () => {
  it('draws a hunt with its loop and lets a stage filter the panels', async () => {
    serve(hunt)
    render(<WorkflowReaderPane wf={row({ id: 'threat-hunt', name: 'Threat hunt', runKind: 'hunt', huntLike: true })} {...actions} />)

    const strip = await screen.findByRole('region', { name: 'How it runs' })
    expect(within(strip).getAllByRole('button', { pressed: false }).map((b) => b.textContent)).toEqual([
      expect.stringContaining('Start'), expect.stringContaining('Frame the case'), expect.stringContaining('Gather evidence'),
      expect.stringContaining('Weigh and review'), expect.stringContaining('Decide'), expect.stringContaining('Hand off'),
    ])
    expect(within(strip).getByText(/The loop: repeats until/)).toBeInTheDocument()
    expect(within(strip).getByText('You approve the explanations')).toBeInTheDocument()
    expect(within(strip).getByText('Asks if spending looks unusual')).toBeInTheDocument()
    expect(within(strip).queryByText('Asks before widening scope')).toBeNull()

    // whole workflow: every role, row and class, the one model with its source, the budget as sentences
    expect(within(panel('Who does it')).getByText('Hunt lead')).toBeInTheDocument()
    expect(within(panel('Who does it')).getByText('Critic')).toBeInTheDocument()
    expect(within(panel('Who does it')).getByText('Claude Sonnet 5')).toBeInTheDocument()
    expect(within(panel('Who does it')).getByText('Investigation default')).toBeInTheDocument()
    expect(within(panel('What it may do on its own')).getByText('Not connected')).toBeInTheDocument()
    expect(within(panel('Stops when')).getByText('An explanation is proven')).toBeInTheDocument()
    expect(within(panel('Stops when')).getByText('The budget ($15) or step limit (12) is reached')).toBeInTheDocument()
    expect(within(panel('Stops when')).getByText('Per-stage stops · Not measured yet')).toBeInTheDocument()
    expect(within(panel('Stops when')).getByText('Review the verdict before closing')).toBeInTheDocument()

    // Weigh and review: only the reviewer, no tools, no checkpoint
    fireEvent.click(within(strip).getByRole('button', { name: /Weigh and review/ }))
    expect(within(panel('Who does it')).getByText('Critic')).toBeInTheDocument()
    expect(within(panel('Who does it')).queryByText('Hunt lead')).toBeNull()
    expect(within(panel('What it may do on its own')).queryByText('findings_search')).toBeNull()
    expect(within(panel('Stops when')).getByText('No checkpoint at this stage.')).toBeInTheDocument()

    // Gather evidence: lead and helpers, their tools, scope and spend
    fireEvent.click(within(strip).getByRole('button', { name: /Gather evidence/ }))
    expect(await within(panel('Who does it')).findByText('Threat hunter')).toBeInTheDocument()
    expect(within(panel('What it may do on its own')).getByText('findings_search')).toBeInTheDocument()
    expect(within(panel('What it may do on its own')).queryByText('Start incident response on a proven explanation')).toBeNull()
    expect(within(panel('Stops when')).getByText('Ask before widening scope')).toBeInTheDocument()
    expect(within(panel('Stops when')).queryByText('Review the verdict before closing')).toBeNull()

    // Hand off: the handoff row, and it is the same stage again that clears the filter
    fireEvent.click(within(strip).getByRole('button', { name: /Hand off/ }))
    expect(within(panel('What it may do on its own')).getByText('Start incident response on a proven explanation')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Show the whole workflow' }))
    expect(within(panel('Who does it')).getByText('Critic')).toBeInTheDocument()
  })

  it('says a turned-off phase agent in Who does it even when every role is present', async () => {
    const note = 'Its phases cannot run as written: phase names agent threat_hunter is turned off'
    serve({ ...hunt, preflight: { ...hunt.preflight, roles_note: note } })
    render(<WorkflowReaderPane wf={row({ id: 'threat-hunt', name: 'Threat hunt', runKind: 'hunt', huntLike: true })} {...actions} />)
    await screen.findByRole('region', { name: 'How it runs' })
    expect(within(panel('Who does it')).getByText('Hunt lead')).toBeInTheDocument()
    expect(within(panel('Who does it')).getByText(note)).toBeInTheDocument()
  })

  it('says an adjudication hands off nothing, and draws one stage per phase for a compose', async () => {
    serve({ definition: { ...hunt.definition, run_kind: 'adjudicate' }, preflight: { ...hunt.preflight, permissions: [] , permissions_note: 'It holds no tools.' } })
    const { unmount } = render(<WorkflowReaderPane wf={row({ id: 'shadow-adjudication', runKind: 'adjudicate', huntLike: true })} {...actions} />)
    expect(await screen.findByText('Records a verdict and the workflow it would have run. It starts nothing.')).toBeInTheDocument()
    unmount()

    serve({
      definition: { run_kind: 'compose', hunt_like: false, objectives: [], phases: [] },
      preflight: {
        roles: { lead: null, reviewer: null, helpers: [
          { agent: 'reporter', name: 'Write', tools: ['get_case'], approval_required: false },
          { agent: 'triage', name: 'Check', tools: ['get_finding'], approval_required: true },
        ] },
        roles_note: null, model: null, model_source: null, skills: [], skills_note: 'No phase can read a skill.',
        permissions: [{ name: 'get_case', changes: 'on_its_own' }, { name: 'get_finding', changes: 'on_its_own' }],
        permissions_note: null, budgets: { max_calls: 20, max_cost_usd: 5, max_wall_ms: 1_800_000 },
        checkpoints: {}, checkpoints_note: 'It pauses only at phases marked approval_required.',
      },
    })
    render(<WorkflowReaderPane wf={row({ id: 'ransom', name: 'Ransom reply' })} {...actions} />)
    const strip = await screen.findByRole('region', { name: 'How it runs' })
    expect(within(strip).getAllByRole('button').map((b) => b.textContent)).toEqual([
      expect.stringContaining('Write'), expect.stringContaining('Check'),
    ])
    expect(within(strip).queryByText(/The loop/)).toBeNull()
    expect(within(strip).getByText('Approval required')).toBeInTheDocument()
    expect(within(panel('Who does it')).queryByText('Claude Sonnet 5')).toBeNull()
    expect(within(panel('Stops when')).getByText('Approval required · Check')).toBeInTheDocument()

    fireEvent.click(within(strip).getByRole('button', { name: /Write/ }))
    expect(within(panel('What it may do on its own')).queryByText('get_finding')).toBeNull()
    expect(within(panel('Stops when')).getByText('No pause at this stage.')).toBeInTheDocument()
  })

  it('shows a single-agent kind as one agent with its instructions and no helper panels', async () => {
    const body = '# Cloud\n\nOne.\n\nTwo.\n\nThree.\n\nFour.\n\nFive.'
    serve({
      definition: { run_kind: 'investigate', hunt_like: false, objectives: ['Establish blast radius'], body, phases: [] },
      preflight: {
        roles: { lead: { name: 'Lead analyst', tools: ['get_finding'] }, helpers: [], reviewer: null },
        roles_note: null, model: 'Claude Sonnet 5', model_source: 'default', skills: [], skills_note: 'The lead has no skill tool.',
        permissions: [{ name: 'get_finding', changes: 'on_its_own' }], permissions_note: null,
        budgets: { max_calls: 10, max_cost_usd: 5, max_wall_ms: 1_800_000 },
        checkpoints: {}, checkpoints_note: 'This workflow never pauses to ask.',
      },
    })
    render(<WorkflowReaderPane wf={row({ id: 'cloud-incident', name: 'Cloud incident', runKind: 'investigate' })} {...actions} />)
    const one = await screen.findByRole('region', { name: 'Runs as one agent' })
    expect(within(one).getByText('Lead analyst')).toBeInTheDocument()
    expect(within(one).getByText('Claude Sonnet 5')).toBeInTheDocument()
    expect(within(one).getByText('Establish blast radius')).toBeInTheDocument()
    expect(within(one).getByText('Five.')).toBeInTheDocument()
    expect(within(one).getByRole('button', { name: 'Show all' })).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(within(one).getByRole('button', { name: 'Show all' }))
    expect(within(one).getByRole('button', { name: 'Show less' })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'How it runs' })).toBeNull()
    expect(screen.queryByRole('region', { name: 'Who does it' })).toBeNull()
    expect(within(panel('What it may do on its own')).getByText('get_finding')).toBeInTheDocument()
    expect(within(panel('Stops when')).getByText('This workflow never pauses to ask.')).toBeInTheDocument()
    // the objectives are the agent's own, not repeated as stops
    expect(within(panel('Stops when')).queryByText('Establish blast radius')).toBeNull()
  })

  it('renders no model for a payload that carries none', async () => {
    serve({ definition: { run_kind: 'root_cause', hunt_like: false, objectives: [], phases: [] }, preflight: { ...hunt.preflight, roles: { lead: { name: 'Lead analyst', tools: [] }, helpers: [], reviewer: null }, model: null, model_source: null, budgets: { max_turns: 1024 } } })
    render(<WorkflowReaderPane wf={row({ id: 'root-cause-analysis', runKind: 'root_cause' })} {...actions} />)
    const one = await screen.findByRole('region', { name: 'Runs as one agent' })
    expect(within(one).getByText('Lead analyst')).toBeInTheDocument()
    expect(within(one).queryByText('Default')).toBeNull()
  })
})

describe('the enable switch and header', () => {
  it('turns a workflow off optimistically, and puts it back with the reason when the server refuses', async () => {
    serve(hunt)
    api.setEnabled.mockRejectedValueOnce({ response: { data: { detail: 'it cannot be turned off' } } })
    render(<WorkflowReaderPane wf={row({ id: 'threat-hunt', name: 'Threat hunt', runKind: 'hunt', huntLike: true })} {...actions} />)
    const toggle = await screen.findByRole('switch', { name: 'Threat hunt on' })
    expect(screen.getByText('Built in · version 3')).toBeInTheDocument()
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-checked', 'false')
    expect(screen.getByRole('button', { name: /Run workflow/ })).toBeDisabled()
    expect(api.setEnabled).toHaveBeenCalledWith('threat-hunt', false)
    expect(await screen.findByRole('alert')).toHaveTextContent('it cannot be turned off')
    expect(toggle).toHaveAttribute('aria-checked', 'true')
    expect(actions.onToggled).not.toHaveBeenCalled()

    api.setEnabled.mockResolvedValueOnce({ data: {} })
    fireEvent.click(toggle)
    await waitFor(() => expect(actions.onToggled).toHaveBeenCalled())
  })

  it('disables the switch for the workflow alerts fall back to, and offers edit and delete only on a custom row', async () => {
    serve(hunt)
    const { unmount } = render(<WorkflowReaderPane wf={row({ id: 'incident-response', name: 'Incident response', canDisable: false })} {...actions} />)
    expect(await screen.findByRole('switch', { name: 'Incident response on' })).toBeDisabled()
    expect(screen.getByText('Alerts land here when nothing else fits, so it stays on.')).toBeInTheDocument()
    expect(screen.queryByTitle('Edit workflow')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /Watch it run/ }))
    expect(actions.onWatch).toHaveBeenCalled()
    unmount()

    serve({ ...hunt, definition: { ...hunt.definition, updated_at: new Date(Date.now() - 2 * 86_400_000).toISOString() } })
    render(<WorkflowReaderPane wf={row({ id: 'mine', name: 'Mine', source: 'custom' })} {...actions} />)
    expect(await screen.findByText('Edited 2 days ago · version 3')).toBeInTheDocument()
    fireEvent.click(screen.getByTitle('Edit workflow'))
    fireEvent.click(screen.getByTitle('Delete workflow'))
    expect(actions.onEdit).toHaveBeenCalled()
    expect(actions.onDelete).toHaveBeenCalled()
  })

  it('renders an unsaved draft from its own phases, with no read and no switch', async () => {
    render(
      <WorkflowReaderPane
        draft={{ name: 'Draft flow', description: 'Not saved', phases: [
          { agent_id: 'triage', name: 'Look', tools: ['get_finding'] },
          { agent_id: 'reporter', name: 'Tell', approval_required: true },
        ] }}
        onBack={vi.fn()}
      />,
    )
    const strip = await screen.findByRole('region', { name: 'How it runs' })
    expect(within(strip).getAllByRole('button').map((b) => b.textContent)).toEqual([expect.stringContaining('Look'), expect.stringContaining('Tell')])
    expect(screen.getByText('Draft · not saved')).toBeInTheDocument()
    expect(screen.queryByRole('switch')).toBeNull()
    expect(screen.queryByRole('button', { name: /Run workflow/ })).toBeNull()
    expect(within(panel('What it may do on its own')).getByText('Shown once the workflow is saved.')).toBeInTheDocument()
    expect(within(panel('Stops when')).getByText('Approval required · Tell')).toBeInTheDocument()
    expect(api.get).not.toHaveBeenCalled()
    expect(api.preflight).not.toHaveBeenCalled()
  })

  it('still shows the definition, labelled, when the preflight cannot be read', async () => {
    api.get.mockResolvedValue({ data: hunt.definition })
    api.preflight.mockRejectedValue(new Error('down'))
    render(<WorkflowReaderPane wf={row({ id: 'threat-hunt', name: 'Threat hunt', runKind: 'hunt', huntLike: true })} {...actions} />)
    expect(await screen.findByRole('heading', { name: 'Threat hunt' })).toBeInTheDocument()
    expect(within(panel('Who does it')).getByText('Couldn’t load this.')).toBeInTheDocument()
    expect(within(panel('What it may do on its own')).getByText('Couldn’t load this.')).toBeInTheDocument()
  })
})
