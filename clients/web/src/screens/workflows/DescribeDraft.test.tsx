/* Describe it: the description opens Vigil's draft in the builder, unsaved, where
   Save creates the custom workflow and a refusal stays on screen. */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import WorkflowsScreen from './WorkflowsScreen'

const h = vi.hoisted(() => ({
  listAll: vi.fn(),
  generate: vi.fn(),
  createCustom: vi.fn(),
}))

vi.mock('../../services/api', () => ({
  workflowApi: { listAll: h.listAll, generate: h.generate, createCustom: h.createCustom },
  agentsApi: {
    listAgents: vi.fn(() => Promise.resolve({ data: { agents: [{ id: 'triage', name: 'Triage agent', description: 'Scores alerts', recommended_tools: ['get_finding'], enabled: true }] } })),
    getAvailableTools: vi.fn(() => Promise.resolve({ data: { catalog: [{ name: 'get_finding', server: 'vigil', group: 'Findings', description: 'Get a finding.', changes: 'read_only' }] } })),
  },
  findingsApi: {},
  casesApi: {},
  approvalsApi: {},
}))
vi.mock('../../services/skillsApi', () => ({ skillsApi: { list: vi.fn(() => Promise.resolve([])) } }))

const draft = {
  name: 'Beacon hunt',
  description: 'Look for a beacon',
  use_case: 'hunting',
  trigger_examples: ['hunt a beacon'],
  phases: [{ phase_id: 'phase-1', order: 1, agent_id: 'triage', name: 'Look', purpose: 'Find it', tools: ['get_finding'], steps: ['Read the alert'] }],
}

async function describeIt() {
  render(
    <MemoryRouter>
      <WorkflowsScreen openChat={vi.fn()} go={vi.fn()} goSettings={vi.fn()} openCase={vi.fn()} setViewFull={vi.fn()} />
    </MemoryRouter>,
  )
  fireEvent.click(await screen.findByRole('button', { name: 'New workflow' }))
  fireEvent.click(screen.getByRole('button', { name: /Describe it/ }))
  fireEvent.change(screen.getByPlaceholderText(/Investigate and contain/), { target: { value: 'Hunt a beacon' } })
  fireEvent.click(screen.getByRole('button', { name: 'Generate' }))
}

beforeEach(() => {
  h.listAll.mockResolvedValue({ data: { workflows: [] } })
  h.generate.mockResolvedValue({ data: { draft } })
})

describe('AI draft in the builder', () => {
  it('opens the draft unsaved, and Save creates it and returns to the list', async () => {
    h.createCustom.mockResolvedValue({ data: { workflow_id: 'wf-beacon-hunt' } })
    await describeIt()
    expect(await screen.findByDisplayValue('Beacon hunt')).toBeInTheDocument()
    expect(screen.getByText(/Drafted by Vigil from your description/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Step 1: Look/ })).toBeInTheDocument()
    expect(h.createCustom).not.toHaveBeenCalled()

    const save = screen.getByRole('button', { name: 'Save workflow' })
    await waitFor(() => expect(save).toBeEnabled())
    fireEvent.click(save)
    await waitFor(() => expect(h.createCustom).toHaveBeenCalledWith({
      name: 'Beacon hunt',
      description: 'Look for a beacon',
      use_case: 'hunting',
      trigger_examples: ['hunt a beacon'],
      phases: [{
        phase_id: 'phase-1', order: 1, agent_id: 'triage', name: 'Look', purpose: 'Find it', steps: ['Read the alert'],
        tools: ['get_finding'], expected_output: '', approval_required: false,
      }],
    }))
    await waitFor(() => expect(screen.queryByLabelText('Workflow builder')).toBeNull())
    expect(h.listAll).toHaveBeenCalledTimes(2)
  })

  it('keeps the draft and shows what the server refused on Save', async () => {
    h.createCustom.mockRejectedValue({ response: { data: { detail: 'Unknown agent_id: ghost' } } })
    await describeIt()
    const save = await screen.findByRole('button', { name: 'Save workflow' })
    await waitFor(() => expect(save).toBeEnabled())
    fireEvent.click(save)
    expect(await screen.findByRole('alert')).toHaveTextContent('Unknown agent_id: ghost')
    expect(screen.getByDisplayValue('Beacon hunt')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save workflow' })).toBeEnabled()
  })
})
