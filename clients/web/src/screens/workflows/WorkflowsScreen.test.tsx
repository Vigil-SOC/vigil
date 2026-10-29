/* The page header and tab strip: heading, the header's New workflow, and a chip
   per tab that is absent (never 0) until its list has loaded. The address names
   the open workflow, agent or skill, so search and the Overview can link to one. */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom'
import WorkflowsScreen from './WorkflowsScreen'
import { COMMANDS } from '../../shell/commandBarModel'

const h = vi.hoisted(() => ({
  listAll: vi.fn(),
  listAgents: vi.fn(),
  listSkills: vi.fn(),
  getAgent: vi.fn(),
  getSkill: vi.fn(),
}))

vi.mock('../../services/api', () => ({
  // the reader pane opens on the first workflow; these tests never let it load
  workflowApi: { listAll: h.listAll, get: () => new Promise(() => {}), preflight: () => new Promise(() => {}) },
  agentsApi: { listAgents: h.listAgents, getAgent: h.getAgent, getAvailableTools: () => new Promise(() => {}) },
  aiConfigApi: { listModels: () => new Promise(() => {}) },
  findingsApi: {},
  casesApi: {},
  approvalsApi: {},
}))
vi.mock('../../services/skillsApi', () => ({ skillsApi: { list: h.listSkills, get: h.getSkill } }))
vi.mock('../../contexts/AuthContext', () => ({ useAuth: () => ({ hasPermission: () => true }) }))

const wf = (id: string) => ({ id, name: id, description: '', agents: [], source: 'file', runs_7d: 0, mean_cost_usd: null })

/** Shows the address, and follows a link the way the command bar does while the screen is open. */
function Address() {
  const { search } = useLocation()
  const navigate = useNavigate()
  return (
    <>
      <output aria-label="address">{search}</output>
      <button onClick={() => navigate({ search: '?tab=agents&agent=y' })}>follow link</button>
    </>
  )
}

function mount(url = '/') {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <WorkflowsScreen openChat={vi.fn()} go={vi.fn()} goSettings={vi.fn()} openCase={vi.fn()} setViewFull={vi.fn()} />
      <Address />
    </MemoryRouter>,
  )
}
const address = () => screen.getByRole('status', { name: 'address' })

beforeEach(() => {
  h.listAll.mockResolvedValue({ data: { workflows: [wf('a'), wf('b')] } })
  h.listAgents.mockResolvedValue({ data: { agents: [{ id: 'x', name: 'X' }, { id: 'y', name: 'Y' }, { id: 'z', name: 'Z' }] } })
  h.listSkills.mockResolvedValue([])
  h.getAgent.mockReturnValue(new Promise(() => {}))
  h.getSkill.mockReturnValue(new Promise(() => {}))
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

  it('opens on the tab named by ?tab=, and on Workflows for anything else', async () => {
    const { unmount } = mount('/workflows?tab=skills')
    expect(await screen.findByRole('tab', { name: /^Skills/ })).toHaveAttribute('aria-selected', 'true')
    unmount()
    mount('/workflows?tab=bogus')
    expect(await screen.findByRole('tab', { name: /^Workflows/ })).toHaveAttribute('aria-selected', 'true')
  })

  it('opens the create flow from the header on a tab other than Workflows', async () => {
    mount()
    fireEvent.click(await screen.findByRole('tab', { name: 'Agents 3' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'New workflow' }))
    expect(screen.getByRole('dialog')).toHaveTextContent('Generate with AI')
  })
})

describe('deep links', () => {
  it('opens the workflow named by ?workflow=, and a card click names its own', async () => {
    // the card shows the name the list maps from the id
    const card = (name: string) => screen.getAllByTitle(name).find((el) => el.matches('.wfk-name'))!.closest('[role="button"]')!
    mount('/workflows?workflow=b')
    await screen.findAllByTitle('B')
    expect(card('B')).toHaveAttribute('aria-pressed', 'true')
    expect(card('A')).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(card('A'))
    expect(address()).toHaveTextContent('?workflow=a')
    expect(card('A')).toHaveAttribute('aria-pressed', 'true')
  })

  it('opens the agent named by ?tab=agents&agent=, and Close drops it from the address', async () => {
    mount('/workflows?tab=agents&agent=y')
    expect(screen.getByRole('tab', { name: /^Agents/ })).toHaveAttribute('aria-selected', 'true')
    const drawer = await screen.findByRole('dialog', { name: 'Edit agent' })
    expect(h.getAgent).toHaveBeenCalledWith('y')
    fireEvent.click(within(drawer).getByRole('button', { name: 'Close' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(address()).toHaveTextContent(/^\?tab=agents$/)
  })

  it('opens the skill named by ?tab=skills&skill=', async () => {
    mount('/workflows?tab=skills&skill=phishing-triage')
    expect(screen.getByRole('tab', { name: /^Skills/ })).toHaveAttribute('aria-selected', 'true')
    expect(await screen.findByRole('dialog', { name: 'Edit phishing-triage' })).toBeInTheDocument()
    expect(h.getSkill).toHaveBeenCalledWith('phishing-triage')
  })

  it('follows a link while the screen is already open on another tab', async () => {
    mount('/workflows')
    await screen.findByRole('tab', { name: 'Workflows 2' })
    fireEvent.click(screen.getByRole('button', { name: 'follow link' }))
    expect(screen.getByRole('tab', { name: /^Agents/ })).toHaveAttribute('aria-selected', 'true')
    expect(await screen.findByRole('dialog', { name: 'Edit agent' })).toBeInTheDocument()
    expect(h.getAgent).toHaveBeenCalledWith('y')
  })
})
