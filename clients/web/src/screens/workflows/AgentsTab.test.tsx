/* The Agents tab is a table of agent, what it does, and model with its source. */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import WorkflowsScreen from './WorkflowsScreen'

vi.mock('../../services/api', () => ({
  workflowApi: { listAll: vi.fn(() => Promise.resolve({ data: { workflows: [] } })) },
  agentsApi: {
    listAgents: vi.fn(() => Promise.resolve({
      data: {
        agents: [
          { id: 'triage', name: 'Triage Agent', specialization: 'Scores alerts', model: 'haiku-x', model_source: 'triage' },
          { id: 'reporter', name: 'Reporter', specialization: 'Writes briefs', model: 'sonnet-x', model_source: 'chat_default' },
          { id: 'custom-1', name: 'Mine', specialization: 'Does mine', model: 'my-model', model_source: 'agent' },
          { id: 'forensics', name: 'Forensics', specialization: 'Evidence', model: null, model_source: null },
        ],
      },
    })),
    getAvailableTools: vi.fn(() => Promise.resolve({ data: { tools: [] } })),
  },
}))

describe('Agents tab', () => {
  it('lists name and origin, what it does, and model with its source', async () => {
    render(<MemoryRouter><WorkflowsScreen openChat={vi.fn()} go={vi.fn()} goSettings={vi.fn()} setViewFull={vi.fn()} /></MemoryRouter>)
    fireEvent.click(screen.getByRole('tab', { name: 'Agents' }))

    expect(await screen.findByText('3 built-in agents plus your own. Each can use its own model.')).toBeTruthy()
    const row = (name: string) => screen.getByText(name).closest('tr') as HTMLElement
    expect(within(row('Triage Agent')).getByText('Built in')).toBeTruthy()
    expect(within(row('Triage Agent')).getByText('haiku-x')).toBeTruthy()
    expect(within(row('Triage Agent')).getByText('Triage default')).toBeTruthy()
    expect(within(row('Reporter')).getByText('Chat default')).toBeTruthy()
    expect(within(row('Mine')).getByText('Yours')).toBeTruthy()
    expect(within(row('Mine')).getByText('Set for this agent')).toBeTruthy()
    // no provider or assignment: a dash, no source line
    expect(within(row('Forensics')).queryByText(/default/)).toBeNull()
    expect(screen.getByRole('button', { name: /Describe a new agent/ })).toBeTruthy()
    expect(screen.queryByText('SOC Agents')).toBeNull()
  })
})
