/* The page header and tab strip: heading, the header's New workflow, and a chip
   per tab that is absent (never 0) until its list has loaded. */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import WorkflowsScreen from './WorkflowsScreen'
import { COMMANDS } from '../../shell/commandBar'

const h = vi.hoisted(() => ({
  listAll: vi.fn(),
  listAgents: vi.fn(),
  listSkills: vi.fn(),
}))

vi.mock('../../services/api', () => ({
  workflowApi: { listAll: h.listAll },
  agentsApi: { listAgents: h.listAgents },
  findingsApi: {},
  casesApi: {},
  approvalsApi: {},
}))
vi.mock('../../services/skillsApi', () => ({ skillsApi: { list: h.listSkills } }))
// the builder is its own screen; here only whether the header opens it matters
vi.mock('./WorkflowBuilder', () => ({ default: () => <div role="dialog">Builder</div> }))

const wf = (id: string) => ({ id, name: id, description: '', agents: [], source: 'file', runs_today: 0, mean_cost_usd: null })

function mount() {
  return render(
    <MemoryRouter>
      <WorkflowsScreen openChat={vi.fn()} go={vi.fn()} goSettings={vi.fn()} setViewFull={vi.fn()} />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  h.listAll.mockResolvedValue({ data: { workflows: [wf('a'), wf('b')] } })
  h.listAgents.mockResolvedValue({ data: { agents: [{ id: 'x', name: 'X' }, { id: 'y', name: 'Y' }, { id: 'z', name: 'Z' }] } })
  h.listSkills.mockResolvedValue([])
})

describe('Agents & workflows header', () => {
  it('has one level-1 heading with the board description', () => {
    mount()
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Agents & workflows')
    expect(screen.getByText(/^How Vigil works a case\./)).toBeInTheDocument()
  })

  it('counts each tab from its list; a real zero shows 0', async () => {
    mount()
    expect(await screen.findByRole('tab', { name: 'Workflows 2' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Agents 3' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Skills 0' })).toHaveTextContent('0')
    expect(screen.getByRole('tab', { name: `Commands ${COMMANDS.length}` })).toBeInTheDocument()
  })

  it('shows no chip while a list loads or after it fails', async () => {
    h.listAgents.mockReturnValue(new Promise(() => undefined))
    h.listSkills.mockRejectedValue(new Error('down'))
    mount()
    await screen.findByRole('tab', { name: 'Workflows 2' })
    await waitFor(() => expect(h.listSkills).toHaveBeenCalled())
    for (const name of ['Agents', 'Skills']) {
      const tab = screen.getByRole('tab', { name })
      expect(tab).toHaveTextContent(new RegExp(`^${name}$`))
    }
  })

  it('opens the create flow from the header on a tab other than Workflows', async () => {
    mount()
    fireEvent.click(await screen.findByRole('tab', { name: 'Agents 3' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'New workflow' }))
    expect(screen.getByRole('dialog')).toHaveTextContent('Builder')
  })
})
