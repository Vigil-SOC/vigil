/* Generate with AI: the description opens a draft in the reader, Save in its header
   creates the custom workflow, and a refusal stays on screen. */
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
  agentsApi: { listAgents: vi.fn(() => Promise.resolve({ data: { agents: [] } })) },
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
  phases: [{ phase_id: 'phase-1', order: 1, agent_id: 'triage', name: 'Look', tools: ['get_finding'], steps: [] }],
}

async function describeIt() {
  render(
    <MemoryRouter>
      <WorkflowsScreen openChat={vi.fn()} go={vi.fn()} goSettings={vi.fn()} openCase={vi.fn()} setViewFull={vi.fn()} />
    </MemoryRouter>,
  )
  fireEvent.click(await screen.findByRole('button', { name: 'Generate with AI' }))
  fireEvent.change(screen.getByPlaceholderText(/Investigate and contain/), { target: { value: 'Hunt a beacon' } })
  fireEvent.click(screen.getByRole('button', { name: 'Generate' }))
}

beforeEach(() => {
  h.listAll.mockResolvedValue({ data: { workflows: [] } })
  h.generate.mockResolvedValue({ data: { draft } })
})

describe('AI draft in the reader', () => {
  it('opens the draft unsaved, and Save creates it and returns to the list', async () => {
    h.createCustom.mockResolvedValue({ data: {} })
    await describeIt()
    expect(await screen.findByRole('heading', { name: 'Beacon hunt' })).toBeInTheDocument()
    expect(screen.getByText('AI draft — not saved')).toBeInTheDocument()
    expect(h.createCustom).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: /Save workflow/ }))
    await waitFor(() => expect(h.createCustom).toHaveBeenCalledWith({
      name: 'Beacon hunt',
      description: 'Look for a beacon',
      use_case: 'hunting',
      trigger_examples: ['hunt a beacon'],
      phases: draft.phases,
    }))
    await waitFor(() => expect(screen.queryByText('AI draft — not saved')).toBeNull())
    expect(h.listAll).toHaveBeenCalledTimes(2)
  })

  it('keeps the draft and shows what the server refused on Save', async () => {
    h.createCustom.mockRejectedValue({ response: { data: { detail: 'Unknown agent_id: ghost' } } })
    await describeIt()
    fireEvent.click(await screen.findByRole('button', { name: /Save workflow/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Unknown agent_id: ghost')
    expect(screen.getByText('AI draft — not saved')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Save workflow/ })).toBeEnabled()
  })
})
