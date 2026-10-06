/* The catalog is the board's card column with each workflow's week of runs, and
   each skill names the workflows whose agents are granted the library. */
import { describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, within } from '@testing-library/react'
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
            run_kind: 'compose',
            runs_7d: 0,
            success_rate: null,
            success_level: null,
            mean_cost_usd: null,
            triggers: [],
            enabled: false,
            tools_used: ['read_skill'],
          },
          {
            id: 'ransom',
            name: 'Ransom reply',
            description: 'Contain it',
            agents: ['reporter'],
            source: 'custom',
            run_kind: 'compose',
            runs_7d: 1,
            success_rate: 1,
            success_level: 'good',
            mean_cost_usd: 0,
            triggers: ['shadow'],
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
            runs_7d: 61,
            success_rate: 0.913,
            success_level: 'fair',
            mean_cost_usd: 1.5,
            triggers: ['schedule'],
          },
          {
            id: 'cloud-incident',
            name: 'Cloud incident',
            description: 'One agent',
            agents: [],
            source: 'file',
            run_kind: 'investigate',
            hunt_like: false,
            runs_7d: 2,
            success_rate: null,
            success_level: null,
            mean_cost_usd: null,
            triggers: ['alerts'],
          },
          {
            id: 'phase-tools',
            name: 'Phase tools only',
            description: 'Tools on the phase are not the grant',
            agents: ['reporter'],
            source: 'file',
            tools_used: ['read_skill'],
            runs_7d: 1,
            mean_cost_usd: null,
          },
          {
            id: 'orphan',
            name: 'Orphan flow',
            description: 'Agent missing from the list',
            agents: ['missing-agent'],
            source: 'file',
            runs_7d: 0,
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
            agent: { role: 'Lead analyst', model: 'Claude Sonnet', model_source: 'assignment' },
            body: 'Scope the cloud account first.',
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
      { name: 'executive-summary', description: 'Write the brief.', source_path: 'skills/executive-summary', bundled: true, file_count: 1 },
    ])),
    get: vi.fn(() => Promise.resolve({
      name: 'executive-summary',
      description: 'Write the brief.',
      source_path: 'skills/executive-summary',
      bundled: true,
      file_count: 2,
      body: '# Brief\n',
      operator_root_set: false,
      version: 3,
      files: [
        { path: 'SKILL.md', size: 120 },
        { path: 'assets/board-brief.md', size: 40 },
      ],
    })),
    file: vi.fn(() => Promise.resolve({ path: 'assets/board-brief.md', content: 'Board brief template' })),
    save: vi.fn(() => Promise.resolve({
      name: 'executive-summary-copy',
      description: 'Write the brief.',
      source_path: 'skills/executive-summary-copy',
      bundled: false,
      file_count: 2,
    })),
    delete: vi.fn(() => Promise.resolve({ deleted: 'desk-check' })),
  },
}))

const card = (name: string) => screen.getByText(name).closest('.wfk') as HTMLElement

describe('workflow catalog cards', () => {
  it('draws kind, triggers, command and the week\'s numbers, and keeps the actions', async () => {
    render(
      <MemoryRouter>
        <WorkflowsScreen openChat={vi.fn()} go={vi.fn()} goSettings={vi.fn()} openCase={vi.fn()} setViewFull={vi.fn()} />
      </MemoryRouter>,
    )

    await screen.findByText('Beacon hunt')
    expect(screen.queryByRole('table')).toBeNull()
    expect(screen.queryByText('Not measured yet')).toBeNull()

    // switched off: no stats line, no badge, and no trigger words to show
    const beacon = card('Beacon hunt')
    expect(beacon).toHaveTextContent('Playbook')
    expect(beacon).toHaveTextContent('Started by hand')
    expect(beacon).toHaveTextContent('Off · not running')
    expect(beacon.querySelector('.level-pill')).toBeNull()

    // a finished run that cost nothing is a real zero
    const ransom = card('Ransom reply')
    expect(ransom).toHaveTextContent('Runs alongside')
    expect(ransom).toHaveTextContent('Ran 1 time this week · 100.0% succeeded · $0.00 per run')
    expect(ransom.querySelector('.level-pill.good')).toHaveTextContent('Good')
    expect(within(ransom).getByTitle('Edit workflow')).toBeInTheDocument()

    const hunt = card('Threat hunt')
    expect(hunt).toHaveTextContent('Hunt')
    expect(hunt).toHaveTextContent('Nightly')
    expect(hunt).toHaveTextContent('/hunt')
    expect(hunt).toHaveTextContent('Ran 61 times this week · 91.3% succeeded · $1.50 per run')
    expect(hunt.querySelector('.level-pill.fair')).toHaveTextContent('Fair')
    expect(within(hunt).queryByTitle('Edit workflow')).toBeNull()

    // runs but none finished: em dashes and no badge; only a command's own workflow shows its chip
    const cloud = card('Cloud incident')
    expect(cloud).toHaveTextContent('Investigation')
    expect(cloud).toHaveTextContent('On alerts')
    expect(cloud).toHaveTextContent('Ran 2 times this week · — succeeded · — per run')
    expect(cloud).not.toHaveTextContent('/investigate')
    expect(cloud.querySelector('.level-pill')).toBeNull()

    // an older backend that sends no triggers or enabled draws no trigger chip and is on
    const orphan = card('Orphan flow')
    expect(orphan.querySelector('.wfk-chip.acc')).toBeNull()
    expect(orphan).toHaveTextContent('Ran 0 times this week')

    fireEvent.click(within(beacon).getByRole('button', { name: 'History' }))
    expect(await screen.findByText('No runs yet')).toBeInTheDocument()
  })

  it('names workflows whose listed agents recommend read_skill, and marks built-in skills read-only', async () => {
    vi.mocked(skillsApi.list).mockResolvedValueOnce([
      { name: 'executive-summary', description: 'Write the brief.', source_path: 'skills/executive-summary', bundled: true, file_count: 1 },
      { name: 'desk-check', description: 'A copy.', source_path: 'skills/desk-check', bundled: false, file_count: 3 },
    ])
    render(
      <MemoryRouter>
        <WorkflowsScreen openChat={vi.fn()} go={vi.fn()} goSettings={vi.fn()} openCase={vi.fn()} setViewFull={vi.fn()} />
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
    expect(screen.getByText('Yours')).toBeInTheDocument()
    expect(screen.queryByText('Custom')).toBeNull()
    expect(screen.getByText('1 file')).toBeInTheDocument()
    expect(screen.getByText('3 files')).toBeInTheDocument()
    // usage is not recorded yet: a placeholder per card, with its explanation
    expect(screen.getAllByText('Used by · Not measured yet')).toHaveLength(2)
    expect(screen.getAllByRole('button', { name: 'Skill reads are not recorded yet.' })).toHaveLength(2)
    expect(screen.getByText('Read-only')).toBeInTheDocument()
    expect(screen.queryByText('skills/executive-summary')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Import' })).toBeNull()
    expect(screen.getAllByRole('button', { name: 'Delete' })).toHaveLength(1)

    fireEvent.click(screen.getByRole('button', { name: 'Edit executive-summary' }))
    expect(await screen.findByText(/path is unset/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save new version' })).toBeDisabled()
  })

  it('saves a bundled skill under a new name and deletes an operator skill after confirm', async () => {
    vi.mocked(skillsApi.get).mockResolvedValueOnce({
      name: 'executive-summary',
      description: 'Write the brief.',
      source_path: 'skills/executive-summary',
      bundled: true,
      file_count: 2,
      body: '# Brief\n',
      operator_root_set: true,
      version: 3,
      files: [
        { path: 'SKILL.md', size: 120 },
        { path: 'assets/board-brief.md', size: 40 },
      ],
    })
    vi.mocked(skillsApi.list)
      .mockResolvedValueOnce([
        { name: 'executive-summary', description: 'Write the brief.', source_path: 'skills/executive-summary', bundled: true, file_count: 1 },
      ])
      .mockResolvedValueOnce([
        { name: 'executive-summary', description: 'Write the brief.', source_path: 'skills/executive-summary', bundled: true, file_count: 1 },
        { name: 'desk-check', description: 'A copy.', source_path: 'skills/desk-check', bundled: false, file_count: 1 },
      ])

    render(
      <MemoryRouter>
        <WorkflowsScreen openChat={vi.fn()} go={vi.fn()} goSettings={vi.fn()} openCase={vi.fn()} setViewFull={vi.fn()} />
      </MemoryRouter>,
    )

    fireEvent.click(screen.getByRole('tab', { name: 'Skills' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Edit executive-summary' }))
    const name = await screen.findByDisplayValue('executive-summary')
    const editor = screen.getByRole('dialog', { name: 'Edit executive-summary' })
    expect(within(editor).getByText('Skill · executive-summary')).toBeInTheDocument()
    expect(within(editor).getByText('Built in · version 3')).toBeInTheDocument()
    expect(within(editor).getByLabelText('Steps (SKILL.md)')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save new version' })).toBeDisabled()

    // a file opens read-only in place of the steps, with a way back
    fireEvent.click(within(editor).getByRole('button', { name: 'assets/board-brief.md' }))
    expect(await within(editor).findByDisplayValue('Board brief template')).toHaveAttribute('readonly')
    expect(skillsApi.file).toHaveBeenCalledWith('executive-summary', 'assets/board-brief.md')
    expect(within(editor).queryByLabelText('Steps (SKILL.md)')).toBeNull()
    fireEvent.click(within(editor).getByRole('button', { name: 'Back to steps' }))
    expect(within(editor).getByLabelText('Steps (SKILL.md)')).toBeInTheDocument()

    fireEvent.change(name, { target: { value: 'desk-check' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save new version' }))
    expect(skillsApi.save).toHaveBeenCalledWith({
      name: 'desk-check',
      description: 'Write the brief.',
      body: '# Brief\n',
      source: 'executive-summary',
    })

    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }))
    const dialog = await screen.findByRole('dialog', { name: 'Delete skill' })
    const hold = within(dialog).getByRole('button', { name: 'Delete. Press and hold to confirm; this cannot be undone.' })

    // a click or an early release deletes nothing
    vi.useFakeTimers()
    try {
      fireEvent.click(hold)
      fireEvent.pointerDown(hold)
      act(() => void vi.advanceTimersByTime(800))
      fireEvent.pointerUp(hold)
      act(() => void vi.advanceTimersByTime(1600))
      expect(skillsApi.delete).not.toHaveBeenCalled()

      fireEvent.pointerDown(hold)
      act(() => void vi.advanceTimersByTime(1600))
      expect(skillsApi.delete).toHaveBeenCalledWith('desk-check')
    } finally {
      vi.useRealTimers()
    }
  })

  it('sends the opened version when saving a custom skill and keeps the edits on a stale 409', async () => {
    vi.mocked(skillsApi.get).mockResolvedValueOnce({
      name: 'desk-check',
      description: 'A copy.',
      source_path: 'skills/desk-check',
      bundled: false,
      file_count: 1,
      body: '# Steps\n',
      operator_root_set: true,
      version: 2,
      files: [{ path: 'SKILL.md', size: 10 }],
    })
    vi.mocked(skillsApi.list).mockResolvedValueOnce([
      { name: 'desk-check', description: 'A copy.', source_path: 'skills/desk-check', bundled: false, file_count: 1 },
    ])
    vi.mocked(skillsApi.save).mockRejectedValueOnce({
      response: { data: { detail: 'This skill changed since you opened it. Reopen it to see the latest.' } },
    })

    render(
      <MemoryRouter>
        <WorkflowsScreen openChat={vi.fn()} go={vi.fn()} goSettings={vi.fn()} openCase={vi.fn()} setViewFull={vi.fn()} />
      </MemoryRouter>,
    )

    fireEvent.click(screen.getByRole('tab', { name: 'Skills' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Edit desk-check' }))
    const editor = await screen.findByRole('dialog', { name: 'Edit desk-check' })
    const steps = await within(editor).findByLabelText('Steps (SKILL.md)')
    fireEvent.change(steps, { target: { value: '# My edit\n' } })
    fireEvent.click(within(editor).getByRole('button', { name: 'Save new version' }))
    expect(skillsApi.save).toHaveBeenCalledWith({
      name: 'desk-check',
      description: 'A copy.',
      body: '# My edit\n',
      version: 2,
    })
    expect(await within(editor).findByText(/changed since you opened it/)).toBeInTheDocument()
    expect(within(editor).getByLabelText('Steps (SKILL.md)')).toHaveValue('# My edit\n')
    expect(within(editor).getByRole('button', { name: 'Save new version' })).toBeEnabled()
  })

  it('builds a skill from a blank editor and refuses a name already in the list', async () => {
    render(
      <MemoryRouter>
        <WorkflowsScreen openChat={vi.fn()} go={vi.fn()} goSettings={vi.fn()} openCase={vi.fn()} setViewFull={vi.fn()} />
      </MemoryRouter>,
    )

    vi.mocked(skillsApi.get).mockResolvedValueOnce({
      name: 'executive-summary',
      description: 'Write the brief.',
      source_path: 'skills/executive-summary',
      bundled: true,
      file_count: 2,
      body: '',
      operator_root_set: true,
      version: 1,
      files: [],
    })
    fireEvent.click(screen.getByRole('tab', { name: 'Skills' }))
    await screen.findByText('executive-summary')
    fireEvent.click(screen.getByRole('button', { name: /Build a skill/ }))
    const dialog = await screen.findByRole('dialog', { name: 'Build a skill' })
    const name = within(dialog).getByLabelText('Name')
    const description = within(dialog).getByLabelText('When to use it')
    expect(within(dialog).getByText('Yours')).toBeInTheDocument()
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

  it('opens a reader for the run kind', async () => {
    render(
      <MemoryRouter>
        <WorkflowsScreen openChat={vi.fn()} go={vi.fn()} goSettings={vi.fn()} openCase={vi.fn()} setViewFull={vi.fn()} />
      </MemoryRouter>,
    )

    await screen.findByText('Beacon hunt')
    fireEvent.click(screen.getByText('Threat hunt'))
    const hunt = await screen.findByRole('dialog')
    expect(within(hunt).getByText('The lead dispatches among these.')).toBeInTheDocument()
    expect(within(hunt).getByText('findings_search')).toBeInTheDocument()
    expect(within(hunt).getByText('This definition declares no pause.')).toBeInTheDocument()
    expect(within(hunt).getByText('Per-stage stops')).toBeInTheDocument()
    expect(within(hunt).getByText('Not measured yet')).toBeInTheDocument()
    expect(within(hunt).queryByText('This workflow runs as one agent.')).toBeNull()
    expect(within(hunt).queryByText('Instructions')).toBeNull()
    expect(within(hunt).queryByText(/\$/)).toBeNull()
    expect(within(hunt).queryByText(/iteration/i)).toBeNull()
    fireEvent.click(within(hunt).getByRole('button', { name: 'Close' }))

    fireEvent.click(screen.getByText('Cloud incident'))
    const one = await screen.findByRole('dialog')
    expect(within(one).getByText('This workflow runs as one agent.')).toBeInTheDocument()
    expect(within(one).getByText('Establish blast radius')).toBeInTheDocument()
    expect(within(one).getByText('Lead analyst')).toBeInTheDocument()
    expect(within(one).getByText('Claude Sonnet')).toBeInTheDocument()
    expect(within(one).getByText(/Investigation default/)).toBeInTheDocument()
    expect(within(one).getByText('Scope the cloud account first.')).toBeInTheDocument()
    // fits in the clamp: no toggle
    expect(within(one).queryByRole('button', { name: 'Show all' })).toBeNull()
    expect(within(one).queryByText('findings_search')).toBeNull()
    expect(within(one).queryByText('The lead dispatches among these.')).toBeNull()
    fireEvent.click(within(one).getByRole('button', { name: 'Close' }))

    fireEvent.click(screen.getByText('Ransom reply'))
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

  it('collapses long instructions behind Show all', async () => {
    const heights = vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(400)
    render(
      <MemoryRouter>
        <WorkflowsScreen openChat={vi.fn()} go={vi.fn()} goSettings={vi.fn()} openCase={vi.fn()} setViewFull={vi.fn()} />
      </MemoryRouter>,
    )
    fireEvent.click(await screen.findByText('Cloud incident'))
    const one = await screen.findByRole('dialog')
    fireEvent.click(await within(one).findByRole('button', { name: 'Show all' }))
    expect(within(one).getByRole('button', { name: 'Show less' })).toHaveAttribute('aria-expanded', 'true')
    fireEvent.click(within(one).getByRole('button', { name: 'Show less' }))
    expect(within(one).getByRole('button', { name: 'Show all' })).toBeInTheDocument()
    heights.mockRestore()
  })

  it('lists every command, marks the later rows, and runs nothing', () => {
    render(
      <MemoryRouter>
        <WorkflowsScreen openChat={vi.fn()} go={vi.fn()} goSettings={vi.fn()} openCase={vi.fn()} setViewFull={vi.fn()} />
      </MemoryRouter>,
    )

    fireEvent.click(screen.getByRole('tab', { name: 'Commands 9' }))
    const table = screen.getByRole('table')
    const row = (name: string) => within(table).getByText(name).closest('[role="row"]') as HTMLElement

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
    expect(within(row('Custom commands')).getAllByRole('cell')[3]).toHaveTextContent('')
    expect(screen.getByText(/^Type a command in search or in Ask Vigil\. Each command runs a workflow or an action;/)).toBeInTheDocument()
    expect(within(table).getAllByRole('columnheader').map((h) => h.textContent)).toEqual(['Command', 'What it does', 'Runs', 'Arguments', 'Status'])
    // what each live command starts, as the bar's run() does; Later rows start nothing
    const cells = (name: string) => within(row(name)).getAllByRole('cell').map((c) => c.textContent)
    expect(cells('/investigate')).toEqual(['/investigate', expect.stringContaining('incident response'), 'Incident response workflow', '<finding or context>', ''])
    expect(cells('/hunt')[2]).toBe('Threat hunt workflow')
    expect(cells('/ticket')[2]).toBe('Exports the case to Jira')
    expect(cells('/isolate')[2]).toBe('—')
    expect(within(table).queryByText(/Asks first|Who can use it|Used, 7 days/)).toBeNull()
    expect(within(table).queryByRole('button')).toBeNull()
    expect(within(table).queryByRole('link')).toBeNull()
    expect(screen.getByRole('tab', { name: 'Workflows' })).toHaveTextContent('Workflows')
    expect(screen.getByRole('tab', { name: 'Agents' })).toHaveTextContent('Agents')
    expect(screen.getByRole('tab', { name: 'Skills' })).toHaveTextContent('Skills')
  })
})
