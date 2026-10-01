/* The catalog is a table of today's runs and cost, and each skill names the
   workflows whose agents are granted the library. */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import WorkflowsScreen from './WorkflowsScreen'

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
            agents: ['hunt_lead'],
            source: 'file',
            run_kind: 'hunt',
            hunt_like: true,
            runs_today: 4,
            mean_cost_usd: 1.5,
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
      { name: 'executive-summary', description: 'Write the brief.', source_path: 'skills/executive-summary' },
    ])),
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

  it('names workflows whose listed agents recommend read_skill, and disables import and edit', async () => {
    render(
      <MemoryRouter>
        <WorkflowsScreen openChat={vi.fn()} go={vi.fn()} goSettings={vi.fn()} setViewFull={vi.fn()} />
      </MemoryRouter>,
    )

    fireEvent.click(screen.getByRole('tab', { name: 'Skills' }))
    expect(await screen.findByText('executive-summary')).toBeInTheDocument()
    expect(await screen.findByText('Beacon hunt, Threat hunt')).toBeInTheDocument()
    expect(screen.queryByText('Ransom reply')).toBeNull()
    expect(screen.queryByText('Phase tools only')).toBeNull()
    expect(screen.queryByText('Orphan flow')).toBeNull()
    expect(screen.getByRole('button', { name: 'The grant offers the whole library.' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Import' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Edit' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Import' })).toHaveAttribute('title', 'Coming in a later release')
  })
})
