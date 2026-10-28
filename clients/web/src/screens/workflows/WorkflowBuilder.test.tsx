/* The workflow builder: build one by hand, reorder, gate, check, edit a saved one. */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import WorkflowBuilder from './WorkflowBuilder'
import { checkDraft, draftFrom, toPayload } from './workflowDraft'
import { forgetToolCatalog } from './toolCatalog'
import { workflowApi } from '../../services/api'

vi.mock('../../services/api', () => ({
  agentsApi: {
    listAgents: vi.fn(() => Promise.resolve({ data: { agents: [
      { id: 'triage', name: 'Triage agent', description: 'Scores alerts', color: '#f00', recommended_tools: ['get_finding', 'read_skill'], enabled: true },
      { id: 'responder', name: 'Response agent', description: 'Plans containment', color: '#0f0', recommended_tools: ['update_case'], enabled: true },
      { id: 'forensics', name: 'Forensics agent', description: 'Evidence', color: '#00f', recommended_tools: [], enabled: false },
    ] } })),
    getAvailableTools: vi.fn(() => Promise.resolve({ data: { catalog: [
      { name: 'get_finding', server: 'vigil', group: 'Findings', description: 'Get a finding by id.', changes: 'read_only' },
      { name: 'update_case', server: 'vigil', group: 'Cases', description: 'Update a case.', changes: 'read_only' },
      { name: 'crowdstrike_contain_host', server: 'crowdstrike', group: 'crowdstrike', description: 'Network-contain a host.', changes: 'on_its_own' },
    ] } })),
  },
  workflowApi: {
    createCustom: vi.fn(() => Promise.resolve({ data: { workflow_id: 'wf-mine' } })),
    updateCustom: vi.fn(() => Promise.resolve({ data: {} })),
    get: vi.fn(),
  },
}))

const onSaved = vi.fn()
const onCancel = vi.fn()
const mount = (props: Partial<React.ComponentProps<typeof WorkflowBuilder>> = {}) =>
  render(<WorkflowBuilder onSaved={onSaved} onCancel={onCancel} {...props} />)
const stepNames = () => Array.from(document.querySelectorAll('.wb-node.step .wb-node-name')).map((n) => n.textContent)

beforeEach(() => {
  vi.clearAllMocks()
  forgetToolCatalog()
})

describe('building a workflow by hand', () => {
  it('adds agents as steps, takes their usual tools, reorders, gates and saves exactly that', async () => {
    mount()
    expect(screen.getByText('No steps yet.')).toBeInTheDocument()
    fireEvent.click(await screen.findByRole('button', { name: 'Add a Triage agent step' }))
    fireEvent.click(screen.getByRole('button', { name: 'Add a Response agent step' }))
    expect(stepNames()).toEqual(['Triage', 'Response'])
    // the usual tools come with the agent; read_skill is the agent's own business
    expect(screen.getByText('update_case', { selector: '.tp-chip-name' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Move step 2 up' }))
    expect(stepNames()).toEqual(['Response', 'Triage'])

    fireEvent.click(screen.getByRole('button', { name: /^Step 1: Response/ }))
    fireEvent.change(screen.getByLabelText('Goal'), { target: { value: 'Contain it' } })
    fireEvent.change(screen.getByLabelText('Instruction 1'), { target: { value: 'Isolate the host' } })
    fireEvent.change(screen.getByLabelText('Search tools this step may use'), { target: { value: 'contain a host' } })
    await screen.findByRole('option', { name: /contain_host/ })
    fireEvent.keyDown(screen.getByLabelText('Search tools this step may use'), { key: 'Enter' })
    // a tool that changes things without approval is flagged until approval is on
    fireEvent.click(screen.getByRole('button', { name: /to fix|to review/ }))
    expect(screen.getByText(/can change things \(crowdstrike_contain_host\) without asking/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /^Step 1: Response/ }))
    fireEvent.click(screen.getByLabelText(/A person approves before this step runs/))

    const save = screen.getByRole('button', { name: 'Save workflow' })
    expect(save).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Workflow name'), { target: { value: 'Contain first' } })
    fireEvent.change(screen.getByLabelText('Workflow description'), { target: { value: 'Contain, then triage' } })
    await waitFor(() => expect(save).toBeEnabled())
    fireEvent.click(save)
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('wf-mine'))
    expect(workflowApi.createCustom).toHaveBeenCalledWith({
      name: 'Contain first',
      description: 'Contain, then triage',
      use_case: '',
      trigger_examples: [],
      phases: [
        { phase_id: 'phase-1', order: 1, agent_id: 'responder', name: 'Response', purpose: 'Contain it', steps: ['Isolate the host'], tools: ['update_case', 'crowdstrike_contain_host'], expected_output: '', timeout_seconds: 300, approval_required: true },
        { phase_id: 'phase-2', order: 2, agent_id: 'triage', name: 'Triage', purpose: '', steps: [], tools: ['get_finding'], expected_output: '', timeout_seconds: 300, approval_required: false },
      ],
    })
  })

  it('inserts a blank step between two, duplicates and removes', async () => {
    mount({ initial: draftFrom({ name: 'x', description: 'y', phases: [{ agent_id: 'triage', name: 'A' }, { agent_id: 'triage', name: 'B' }] }) })
    fireEvent.click(screen.getByRole('button', { name: 'Add a step at position 2' }))
    expect(stepNames()).toEqual(['A', 'Unnamed step', 'B'])
    fireEvent.click(screen.getByRole('button', { name: /^Step 1: A/ }))
    fireEvent.click(screen.getByRole('button', { name: /Duplicate/ }))
    expect(stepNames()).toEqual(['A', 'A (copy)', 'Unnamed step', 'B'])
    fireEvent.click(screen.getByRole('button', { name: 'Remove step 3' }))
    expect(stepNames()).toEqual(['A', 'A (copy)', 'B'])
  })

  it('edits a saved workflow and saves only when something changed', async () => {
    vi.mocked(workflowApi.get).mockResolvedValue({ data: {
      workflow_id: 'wf-old', name: 'Old', description: 'Old one', use_case: '', trigger_examples: [],
      phases: [{ phase_id: 'p-1', agent_id: 'triage', name: 'Look', tools: ['get_finding'], steps: ['Read it'], purpose: 'p', timeout_seconds: 600 }],
    } } as never)
    mount({ workflowId: 'wf-old' })
    expect(await screen.findByDisplayValue('Old')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Saved' })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Workflow name'), { target: { value: 'Newer' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(workflowApi.updateCustom).toHaveBeenCalledWith('wf-old', expect.objectContaining({
      name: 'Newer',
      phases: [expect.objectContaining({ phase_id: 'p-1', timeout_seconds: 600, steps: ['Read it'] })],
    })))
    expect(onSaved).toHaveBeenCalledWith('wf-old')
  })

  it('asks before throwing away unsaved changes', async () => {
    mount()
    fireEvent.change(screen.getByLabelText('Workflow name'), { target: { value: 'Half done' } })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onCancel).not.toHaveBeenCalled()
    fireEvent.click(within(screen.getByRole('alert')).getByRole('button', { name: 'Discard changes' }))
    expect(onCancel).toHaveBeenCalled()
  })

  it('imports a pasted definition from the Definition view', async () => {
    mount()
    fireEvent.click(screen.getByRole('tab', { name: /Definition/ }))
    fireEvent.change(screen.getByLabelText('Workflow definition'), { target: { value: JSON.stringify({ name: 'Pasted', description: 'd', phases: [{ agent_id: 'triage', name: 'One' }] }) } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
    expect(screen.getByDisplayValue('Pasted')).toBeInTheDocument()
    expect(stepNames()).toEqual(['One'])
  })

  it('does not drop a Definition edit: switching to Flow applies it, Save waits for it', async () => {
    mount({ initial: draftFrom({ name: 'x', description: 'y', phases: [{ agent_id: 'triage', name: 'One' }] }) })
    fireEvent.click(screen.getByRole('tab', { name: /Definition/ }))
    fireEvent.change(screen.getByLabelText('Workflow definition'), { target: { value: JSON.stringify({ name: 'x', description: 'y', phases: [{ agent_id: 'triage', name: 'Two' }] }) } })
    fireEvent.click(screen.getByRole('button', { name: 'Save workflow' }))
    expect(workflowApi.createCustom).not.toHaveBeenCalled()
    expect(screen.getByText(/Definition edit is not applied yet/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: /Flow/ }))
    expect(stepNames()).toEqual(['Two'])
    // a definition that cannot be read keeps the Definition view open with the reason
    fireEvent.click(screen.getByRole('tab', { name: /Definition/ }))
    fireEvent.change(screen.getByLabelText('Workflow definition'), { target: { value: '{ not json' } })
    fireEvent.click(screen.getByRole('tab', { name: /Flow/ }))
    expect(screen.getByLabelText('Workflow definition')).toBeInTheDocument()
    expect(screen.getByText(/Not applied:/)).toBeInTheDocument()
  })
})

describe('checks', () => {
  const agents = [{ id: 'triage', name: 'Triage agent', description: '', color: null, recommended_tools: [], enabled: true }, { id: 'off', name: 'Off agent', description: '', color: null, recommended_tools: [], enabled: false }]
  const tools = new Map([['get_finding', { name: 'get_finding', server: 'vigil', group: 'Findings', description: '', changes: 'read_only' as const }]])

  it('blocks a save with no name, description or steps', () => {
    const levels = checkDraft(draftFrom({}), agents, tools).map((c) => c.level)
    expect(levels).toEqual(['error', 'error', 'error'])
  })

  it('names the step with a missing or unknown agent, an agent that is off, and a tool not connected', () => {
    const d = draftFrom({ name: 'n', description: 'd', phases: [
      { name: 'A', agent_id: '', purpose: 'p' },
      { name: 'B', agent_id: 'ghost', purpose: 'p' },
      { name: 'C', agent_id: 'off', purpose: 'p', tools: ['get_finding', 'okta_reset'] },
    ] })
    const texts = checkDraft(d, agents, tools).map((c) => `${c.level}: ${c.text}`)
    expect(texts).toEqual([
      'error: Step 1 (A) needs an agent to run it.',
      'error: Step 2 (B): no agent called “ghost” exists.',
      'warn: Step 3 (C): Off agent is turned off, so Run is refused until it is on.',
      'warn: Step 3 (C): okta_reset is not connected here, so the step runs without it.',
    ])
  })

  it('gives every step its own phase id, keeping saved ones', () => {
    // a new step put before a saved "phase-1" would take "phase-1" from the server's position numbering
    const d = draftFrom({ name: 'x', description: 'y', phases: [{ agent_id: 'triage', name: 'New' }, { phase_id: 'phase-1', agent_id: 'triage', name: 'Old' }, { phase_id: 'phase-1', agent_id: 'triage', name: 'Pasted twice' }] })
    expect(toPayload(d).phases.map((p) => p.phase_id)).toEqual(['phase-2', 'phase-1', 'phase-3'])
  })

  it('reads a pasted definition of the wrong shape without breaking', () => {
    const d = draftFrom({ name: 42, description: null, trigger_examples: 'one', phases: [{ name: 7, agent_id: 'triage', steps: 'Look up hashes', tools: 'splunk_search', approval_required: 'yes' }, 'junk', null] as never })
    expect(d.name).toBe('42')
    expect(d.description).toBe('')
    expect(d.trigger_examples).toEqual(['one'])
    expect(d.phases).toHaveLength(1)
    expect(d.phases[0]).toMatchObject({ name: '7', steps: ['Look up hashes'], tools: ['splunk_search'], approval_required: false })
  })

  it('sends trimmed text, numbered order and no empty instructions', () => {
    const body = toPayload(draftFrom({ name: ' n ', description: ' d ', trigger_examples: [' a ', ''], phases: [{ agent_id: 'triage', name: ' A ', steps: ['one', ' ', 'two'] }] }))
    expect(body).toEqual({
      name: 'n', description: 'd', use_case: '', trigger_examples: ['a'],
      phases: [{ phase_id: 'phase-1', order: 1, agent_id: 'triage', name: 'A', purpose: '', steps: ['one', 'two'], tools: [], expected_output: '', timeout_seconds: 300, approval_required: false }],
    })
  })
})
