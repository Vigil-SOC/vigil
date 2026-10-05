/* The catalog is a table of today's runs and cost, and each skill names the
   workflows whose agents are granted the library. */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import WorkflowsScreen from './WorkflowsScreen'
import { skillsApi } from '../../services/skillsApi'

vi.mock('../../services/api', () => ({
  workflowApi: {
    listAll: vi.fn(() => Promise.resolve({
      data: {
        workflows: [
          {
            id: 'beacon',
            name: 'Beacon hunt',
            description: 'Look for beacons',
            agents: ['ghost-agent', 'triage'],
            source: 'file',
            runs_today: 0,
            mean_cost_usd: null,
            tools_used: ['read_skill'],
          },
          {
            id: 'ransom',
            name: 'Ransom reply',
            description: 'Contain it',
            agents: ['reporter'],
            source: 'custom',
            runs_today: 2,
            mean_cost_usd: 0,
            updated_at: '2026-10-01T12:00:00+00:00',
          },
          {
            id: 'threat-hunt',
            name: 'Threat hunt',
            description: 'Hunt',
            agents: ['hunt_lead', 'threat_hunter'],
            source: 'file',
            run_kind: 'hunt',
            hunt_like: true,
            runs_today: 4,
            mean_cost_usd: 1.5,
          },
          {
            id: 'cloud-incident',
            name: 'Cloud incident',
            description: 'One agent',
            agents: [],
            source: 'file',
            run_kind: 'investigate',
            hunt_like: false,
            runs_today: 0,
            mean_cost_usd: null,
          },
          {
            id: 'phase-tools',
            name: 'Phase tools only',
            description: 'Tools on the phase are not the grant',
            agents: ['reporter'],
            source: 'file',
            tools_used: ['read_skill'],
            runs_today: 1,
            mean_cost_usd: null,
          },
          {
            id: 'orphan',
            name: 'Orphan flow',
            description: 'Agent missing from the list',
            agents: ['missing-agent'],
            source: 'file',
            runs_today: 0,
            mean_cost_usd: null,
          },
        ],
      },
    })),
    get: vi.fn((id: string) => {
      if (id === 'threat-hunt') {
        return Promise.resolve({
          data: {
            hunt_like: true,
            run_kind: 'hunt',
            objectives: ['State a hypothesis'],
            checkpoints: {},
            phases: [
              { id: 'threat_hunter', agent: 'threat_hunter', name: 'Behavioural hunting', tools: ['findings_search', 'telemetry_search', 'entity_recall'] },
              { id: 'threat_intel', agent: 'threat_intel', tools: ['indicator_lookup', 'entity_recall'] },
            ],
          },
        })
      }
      if (id === 'cloud-incident') {
        return Promise.resolve({
          data: {
            hunt_like: false,
            run_kind: 'investigate',
            objectives: ['Establish blast radius'],
            checkpoints: {},
            phases: [],
          },
        })
      }
      if (id === 'ransom') {
        return Promise.resolve({
          data: {
            hunt_like: false,
            run_kind: 'compose',
            objectives: [],
            checkpoints: { hypothesis_approval: 'ask' },
            phases: [
              { agent_id: 'reporter', name: 'Write', tools: ['get_case'] },
              { agent_id: 'triage', name: 'Check', tools: ['get_finding'], approval_required: true },
            ],
          },
        })
      }
      return Promise.resolve({
        data: { hunt_like: false, run_kind: 'compose', objectives: [], checkpoints: {}, phases: [] },
      })
    }),
    listRuns: vi.fn(() => Promise.resolve({ data: { runs: [] } })),
    preflight: vi.fn(() => Promise.resolve({ data: {} })),
    getRun: vi.fn(() => new Promise(() => undefined)),
  },
  agentsApi: {
    listAgents: vi.fn(() => Promise.resolve({
      data: {
        agents: [
          { id: 'triage', name: 'Triage', recommended_tools: ['get_finding', 'read_skill'] },
          { id: 'reporter', name: 'Reporter', recommended_tools: ['search'] },
          { id: 'hunt_lead', name: 'Hunt Lead', recommended_tools: ['read_skill'] },
        ],
      },
    })),
  },
  findingsApi: { getAll: vi.fn(() => Promise.resolve({ data: { findings: [] } })) },
  casesApi: { getAll: vi.fn(() => Promise.resolve({ data: { cases: [] } })) },
}))
vi.mock('../../services/skillsApi', () => ({
  skillsApi: {
    list: vi.fn(() => Promise.resolve([
      { name: 'executive-summary', description: 'Write the brief.', source_path: 'skills/executive-summary', bundled: true },
    ])),
    get: vi.fn(() => Promise.resolve({
      name: 'executive-summary',
      description: 'Write the brief.',
      source_path: 'skills/executive-summary',
      bundled: true,
      body: '# Brief\n',
      operator_root_set: false,
    })),
    save: vi.fn(() => Promise.resolve({
      name: 'executive-summary-copy',
      description: 'Write the brief.',
      source_path: 'skills/executive-summary-copy',
      bundled: false,
    })),
    delete: vi.fn(() => Promise.resolve({ deleted: 'desk-check' })),
  },
}))

const cells = (name: string) => within(screen.getByText(name).closest('tr') as HTMLElement).getAllByRole('cell')

describe('workflow catalog table', () => {
  it('shows today\'s runs, a real zero cost, an em dash for a missing mean, and trust', async () => {
    render(
      <MemoryRouter>
        <WorkflowsScreen openChat={vi.fn()} go={vi.fn()} goSettings={vi.fn()} setViewFull={vi.fn()} />
      </MemoryRouter>,
    )

    await screen.findByText('Beacon hunt')
    expect(screen.getByRole('table')).toBeInTheDocument()

    const beacon = cells('Beacon hunt')
    expect(beacon[2]).toHaveTextContent('0')
    expect(beacon[3]).toHaveTextContent('—')
    expect(beacon[3]).not.toHaveTextContent('not priced')
    expect(beacon[4]).toHaveTextContent('—')
    expect(beacon[5]).toHaveTextContent('Not measured yet')

    const ransom = cells('Ransom reply')
    expect(ransom[2]).toHaveTextContent('2')
    expect(ransom[3]).toHaveTextContent('$0.00')
    expect(ransom[3]).not.toHaveTextContent('not priced')
    expect(ransom[4].textContent).not.toBe('—')
    expect(within(ransom[6]).getByTitle('Edit workflow')).toBeInTheDocument()

    const hunt = cells('Threat hunt')
    expect(hunt[2]).toHaveTextContent('4')
    expect(hunt[3]).toHaveTextContent('$1.50')
    expect(within(hunt[6]).queryByTitle('Edit workflow')).toBeNull()

    fireEvent.click(within(beacon[6]).getByRole('button', { name: 'History' }))
    expect(await screen.findByText('No runs yet')).toBeInTheDocument()
  })

  it('names workflows whose listed agents recommend read_skill, and marks built-in skills read-only', async () => {
    vi.mocked(skillsApi.list).mockResolvedValueOnce([
      { name: 'executive-summary', description: 'Write the brief.', source_path: 'skills/executive-summary', bundled: true },
      { name: 'desk-check', description: 'A copy.', source_path: 'skills/desk-check', bundled: false },
    ])
    render(
      <MemoryRouter>
        <WorkflowsScreen openChat={vi.fn()} go={vi.fn()} goSettings={vi.fn()} setViewFull={vi.fn()} />
      </MemoryRouter>,
    )

    fireEvent.click(screen.getByRole('tab', { name: 'Skills' }))
    expect(await screen.findByText('executive-summary')).toBeInTheDocument()
    expect(screen.getByText('desk-check')).toBeInTheDocument()
    expect(await screen.findByText('Beacon hunt, Threat hunt')).toBeInTheDocument()
    expect(screen.queryByText('Ransom reply')).toBeNull()
    expect(screen.queryByText('Phase tools only')).toBeNull()
    expect(screen.queryByText('Orphan flow')).toBeNull()
    // once above the grid, not on every card
    expect(screen.getAllByText('Offered to')).toHaveLength(1)
    expect(screen.getAllByRole('button', { name: 'The grant offers the whole library.' })).toHaveLength(1)
    expect(screen.getByText('Built in')).toBeInTheDocument()
    expect(screen.getByText('Read-only')).toBeInTheDocument()
    expect(screen.queryByText('skills/executive-summary')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Import' })).toBeNull()
    expect(screen.getAllByRole('button', { name: 'Delete' })).toHaveLength(1)

    fireEvent.click(screen.getByRole('button', { name: 'Edit executive-summary' }))
    expect(await screen.findByText(/path is unset/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('saves a bundled skill under a new name and deletes an operator skill after confirm', async () => {
    vi.mocked(skillsApi.get).mockResolvedValueOnce({
      name: 'executive-summary',
      description: 'Write the brief.',
      source_path: 'skills/executive-summary',
      bundled: true,
      body: '# Brief\n',
      operator_root_set: true,
    })
    vi.mocked(skillsApi.list)
      .mockResolvedValueOnce([
        { name: 'executive-summary', description: 'Write the brief.', source_path: 'skills/executive-summary', bundled: true },
      ])
      .mockResolvedValueOnce([
        { name: 'executive-summary', description: 'Write the brief.', source_path: 'skills/executive-summary', bundled: true },
        { name: 'desk-check', description: 'A copy.', source_path: 'skills/desk-check', bundled: false },
      ])

    render(
      <MemoryRouter>
        <WorkflowsScreen openChat={vi.fn()} go={vi.fn()} goSettings={vi.fn()} setViewFull={vi.fn()} />
      </MemoryRouter>,
    )

    fireEvent.click(screen.getByRole('tab', { name: 'Skills' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Edit executive-summary' }))
    const name = await screen.findByDisplayValue('executive-summary')
    const editor = screen.getByRole('dialog', { name: 'Edit executive-summary' })
    expect(within(editor).getByText('Skill · executive-summary')).toBeInTheDocument()
    expect(within(editor).getByText('Built in')).toBeInTheDocument()
    expect(within(editor).getByLabelText('Steps (SKILL.md)')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
    fireEvent.change(name, { target: { value: 'desk-check' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(skillsApi.save).toHaveBeenCalledWith({
      name: 'desk-check',
      description: 'Write the brief.',
      body: '# Brief\n',
    })

    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }))
    const dialog = await screen.findByRole('dialog', { name: 'Delete skill' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }))
    expect(skillsApi.delete).toHaveBeenCalledWith('desk-check')
  })

  it('builds a skill from a blank editor and refuses a name already in the list', async () => {
    render(
      <MemoryRouter>
        <WorkflowsScreen openChat={vi.fn()} go={vi.fn()} goSettings={vi.fn()} setViewFull={vi.fn()} />
      </MemoryRouter>,
    )

    vi.mocked(skillsApi.get).mockResolvedValueOnce({
      name: 'executive-summary',
      description: 'Write the brief.',
      source_path: 'skills/executive-summary',
      bundled: true,
      body: '',
      operator_root_set: true,
    })
    fireEvent.click(screen.getByRole('tab', { name: 'Skills' }))
    await screen.findByText('executive-summary')
    fireEvent.click(screen.getByRole('button', { name: /Build a skill/ }))
    const dialog = await screen.findByRole('dialog', { name: 'Build a skill' })
    const name = within(dialog).getByLabelText('Name')
    const description = within(dialog).getByLabelText('When to use it')
    expect(within(dialog).getByText('Custom')).toBeInTheDocument()
    expect(within(dialog).getByText('Lower case and hyphens, 64 characters at most')).toBeInTheDocument()
    expect(name).toHaveValue('')
    expect(name).toBeEnabled()
    fireEvent.change(description, { target: { value: 'Does a thing.' } })
    expect(within(dialog).getByRole('button', { name: 'Save' })).toBeDisabled()

    fireEvent.change(name, { target: { value: 'executive-summary' } })
    expect(within(dialog).getByText(/already exists/)).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Save' })).toBeDisabled()

    fireEvent.change(name, { target: { value: 'new-skill' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }))
    expect(skillsApi.save).toHaveBeenCalledWith({ name: 'new-skill', description: 'Does a thing.', body: '' })
  })

  it('opens a reader for the run kind and draws arrows only when the roster is an order', async () => {
    render(
      <MemoryRouter>
        <WorkflowsScreen openChat={vi.fn()} go={vi.fn()} goSettings={vi.fn()} setViewFull={vi.fn()} />
      </MemoryRouter>,
    )

    await screen.findByText('Beacon hunt')
    expect(cells('Beacon hunt')[1].querySelector('.seq-arrow')).not.toBeNull()
    expect(cells('Threat hunt')[1].querySelector('.seq-arrow')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Threat hunt' }))
    const hunt = await screen.findByRole('dialog')
    expect(within(hunt).getByText('The lead dispatches among these.')).toBeInTheDocument()
    expect(within(hunt).getByText('findings_search')).toBeInTheDocument()
    expect(within(hunt).getByText('This definition declares no pause.')).toBeInTheDocument()
    expect(within(hunt).getByText('Per-stage stops')).toBeInTheDocument()
    expect(within(hunt).getByText('Not measured yet')).toBeInTheDocument()
    expect(within(hunt).queryByText('This workflow runs as one agent.')).toBeNull()
    expect(within(hunt).queryByText(/\$/)).toBeNull()
    expect(within(hunt).queryByText(/iteration/i)).toBeNull()
    fireEvent.click(within(hunt).getByRole('button', { name: 'Close' }))

    fireEvent.click(screen.getByRole('button', { name: 'Cloud incident' }))
    const one = await screen.findByRole('dialog')
    expect(within(one).getByText('This workflow runs as one agent.')).toBeInTheDocument()
    expect(within(one).getByText('Establish blast radius')).toBeInTheDocument()
    expect(within(one).queryByText('findings_search')).toBeNull()
    expect(within(one).queryByText('The lead dispatches among these.')).toBeNull()
    fireEvent.click(within(one).getByRole('button', { name: 'Close' }))

    fireEvent.click(screen.getByRole('button', { name: 'Ransom reply' }))
    const compose = await screen.findByRole('dialog')
    expect(within(compose).getByText('Phases run in this order.')).toBeInTheDocument()
    expect(within(compose).getByText(/Approval required/)).toBeInTheDocument()
    expect(within(compose).getByText('hypothesis_approval')).toBeInTheDocument()
    expect(within(compose).queryByText('This definition declares no pause.')).toBeNull()
    const order = within(compose).getByRole('list')
    expect(within(order).getAllByRole('listitem')[0]).toHaveTextContent('Write')
    expect(within(order).getAllByRole('listitem')[1]).toHaveTextContent('Check')
    expect(within(compose).queryByText(/\$/)).toBeNull()
  })

  it('lists every command, marks the later rows, and runs nothing', () => {
    render(
      <MemoryRouter>
        <WorkflowsScreen openChat={vi.fn()} go={vi.fn()} goSettings={vi.fn()} setViewFull={vi.fn()} />
      </MemoryRouter>,
    )

    fireEvent.click(screen.getByRole('tab', { name: 'Commands 9' }))
    const table = screen.getByRole('table')
    const row = (name: string) => within(table).getByText(name).closest('tr') as HTMLElement

    for (const name of ['/investigate', '/hunt', '/replay', '/ask', '/ticket']) {
      const live = row(name)
      expect(live).not.toHaveAttribute('aria-disabled')
      expect(within(live).queryByText('Later')).toBeNull()
      live.focus()
      expect(document.activeElement).not.toBe(live)
    }
    for (const name of ['/hold', '/isolate', '/phish', 'Custom commands']) {
      const later = row(name)
      expect(later).toHaveAttribute('aria-disabled', 'true')
      expect(later).not.toHaveAttribute('tabindex')
      expect(within(later).getByText('Later')).toBeInTheDocument()
      later.focus()
      expect(document.activeElement).not.toBe(later)
    }
    expect(within(row('Custom commands')).getAllByRole('cell')[1]).toHaveTextContent('')
    expect(within(table).queryByRole('button')).toBeNull()
    expect(within(table).queryByRole('link')).toBeNull()
    expect(screen.getByRole('tab', { name: 'Workflows' })).toHaveTextContent('Workflows')
    expect(screen.getByRole('tab', { name: 'Agents' })).toHaveTextContent('Agents')
    expect(screen.getByRole('tab', { name: 'Skills' })).toHaveTextContent('Skills')
  })
})
