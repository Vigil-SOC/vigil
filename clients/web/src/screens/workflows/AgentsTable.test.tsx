/* The Agents tab is one table: model and where it came from, what it may change,
   its last 7 days, and an On switch that updates at once and rolls back on error. */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import WorkflowsScreen from './WorkflowsScreen'
import { agentsApi } from '../../services/api'

vi.mock('../../services/api', () => {
  const agent = (o: Record<string, unknown>) => ({
    description: '', skills: 0, changes: 'read_only', runs_7d: 0, success_rate: null, success_level: null, enabled: true, ...o,
  })
  return {
    workflowApi: { listAll: vi.fn(() => Promise.resolve({ data: { workflows: [] } })), listRuns: vi.fn(() => Promise.resolve({ data: { runs: [] } })) },
    agentsApi: {
      listAgents: vi.fn(() => Promise.resolve({
        data: {
          agents: [
            agent({ id: 'custom-mine', name: 'My agent', description: 'Mine does a thing', model: 'Claude Opus 5.5', model_source: 'agent', component_category: 'investigation', skills: 2, changes: 'on_its_own', runs_7d: 1240, success_rate: 0.912, success_level: 'fair' }),
            agent({ id: 'triage', name: 'Triage agent', specialization: 'Scores alerts', model: 'Claude Haiku 4.5', model_source: 'assignment', component_category: 'triage', skills: 3, changes: 'asks_first', runs_7d: 18240, success_rate: 0.981, success_level: 'good' }),
            agent({ id: 'reporter', name: 'Reporting agent', icon: 'W', description: 'Writes briefs', model: 'Claude Sonnet 5', model_source: 'default', runs_7d: 0 }),
            agent({ id: 'hunter', name: 'Hunter', description: 'Hunts', model: null, model_source: null, enabled: false }),
            agent({ id: 'blind', name: 'Blind agent', icon: 'TI', description: 'Stats down', runs_7d: null }),
          ],
        },
      })),
      setEnabled: vi.fn(() => Promise.resolve({ data: {} })),
      forkAgent: vi.fn(() => Promise.resolve({ data: {} })),
    },
    findingsApi: { getAll: vi.fn(() => Promise.resolve({ data: { findings: [] } })) },
    casesApi: { getAll: vi.fn(() => Promise.resolve({ data: { cases: [] } })) },
  }
})
vi.mock('../../services/skillsApi', () => ({ skillsApi: { list: vi.fn(() => Promise.resolve([])) } }))

const row = (name: string) => screen.getByText(name).closest('tr') as HTMLElement

async function openAgents() {
  render(
    <MemoryRouter>
      <WorkflowsScreen openChat={vi.fn()} go={vi.fn()} goSettings={vi.fn()} openCase={vi.fn()} setViewFull={vi.fn()} />
    </MemoryRouter>,
  )
  fireEvent.click(await screen.findByRole('tab', { name: /^Agents/ }))
  await screen.findByText('Triage agent')
}

describe('agents table', () => {
  beforeEach(() => vi.clearAllMocks())

  it('lists built-ins before customs under the eight columns, with a counted summary', async () => {
    await openAgents()
    const heads = screen.getAllByRole('columnheader').map((h) => h.textContent)
    expect(heads.slice(0, 8)).toEqual(['Agent', 'What it does', 'Model', 'Skills', 'Changes things?', 'Runs, 7 days', 'Success', 'On'])
    const names = screen.getAllByRole('row').slice(1).map((r) => within(r).getAllByRole('cell')[0].textContent)
    expect(names).toEqual(['TATriage agentBuilt in', 'WReporting agentBuilt in', 'HUHunterBuilt in', 'TIBlind agentBuilt in', 'MAMy agentYours'])
    expect(screen.getByText('4 built-in agents plus your own. Each can use its own model.')).toBeInTheDocument()
    expect(screen.queryByText('SOC Agents')).toBeNull()
    expect(screen.queryByText('Template')).toBeNull()
  })

  it('shows model source lines, what it may change, and success', async () => {
    await openAgents()
    expect(row('My agent')).toHaveTextContent('Claude Opus 5.5Set for this agent')
    expect(row('Triage agent')).toHaveTextContent('Claude Haiku 4.5Triage default')
    expect(row('Triage agent')).toHaveTextContent('Scores alerts')
    expect(row('Reporting agent')).toHaveTextContent('Claude Sonnet 5Default')
    expect(row('Hunter')).not.toHaveTextContent(/default|Set for/i)
    expect(within(row('Hunter')).getAllByRole('cell')[2]).toHaveTextContent('—')

    expect(within(row('Triage agent')).getByText('Asks first')).toHaveAttribute('title', expect.stringContaining('wait for you'))
    expect(within(row('My agent')).getByText('On its own')).toBeInTheDocument()
    expect(row('Triage agent')).toHaveTextContent('18,240')
    expect(row('Triage agent')).toHaveTextContent('98.1%Good')
    expect(row('My agent')).toHaveTextContent('91.2%Fair')
  })

  it('shows 0 runs with no rate or badge, and a dash when the stats are missing', async () => {
    await openAgents()
    const empty = within(row('Reporting agent')).getAllByRole('cell')
    expect(empty[5]).toHaveTextContent(/^0$/)
    expect(empty[6]).toHaveTextContent(/^—$/)
    const blind = within(row('Blind agent')).getAllByRole('cell')
    expect(blind[5]).toHaveTextContent(/^—$/)
    expect(blind[6]).toHaveTextContent(/^—$/)
  })

  it('flips the switch at once, keeps it on success and rolls back with an error on failure', async () => {
    await openAgents()
    const sw = within(row('Triage agent')).getByRole('switch')
    expect(sw).toHaveAttribute('aria-checked', 'true')
    fireEvent.click(sw)
    expect(sw).toHaveAttribute('aria-checked', 'false')
    expect(agentsApi.setEnabled).toHaveBeenCalledWith('triage', false)
    expect(agentsApi.forkAgent).not.toHaveBeenCalled() // the switch is not a row click
    expect(row('Triage agent')).toHaveClass('ag-off')

    vi.mocked(agentsApi.setEnabled).mockRejectedValueOnce(new Error('boom'))
    const off = within(row('Hunter')).getByRole('switch')
    expect(off).toHaveAttribute('aria-checked', 'false')
    fireEvent.click(off)
    expect(off).toHaveAttribute('aria-checked', 'true')
    await waitFor(() => expect(off).toHaveAttribute('aria-checked', 'false'))
    expect(await screen.findByRole('alert')).toHaveTextContent('Couldn’t turn Hunter on: boom')
  })

  it('forks a built-in on row click', async () => {
    await openAgents()
    fireEvent.click(screen.getByText('Triage agent'))
    expect(agentsApi.forkAgent).toHaveBeenCalledWith('triage')
  })
})
