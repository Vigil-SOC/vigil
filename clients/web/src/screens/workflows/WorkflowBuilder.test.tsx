/* Generate marks the canvas an unsaved AI draft. A blank workflow stays a draft. */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import WorkflowBuilder from './WorkflowBuilder'

vi.mock('@xyflow/react', () => ({
  ReactFlow: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  Background: () => null,
  Controls: () => null,
  MiniMap: () => null,
  addEdge: (_c: unknown, eds: unknown[]) => eds,
  applyNodeChanges: (_c: unknown, nds: unknown[]) => nds,
  applyEdgeChanges: (_c: unknown, eds: unknown[]) => eds,
  MarkerType: { ArrowClosed: 'arrow' },
}))
vi.mock('@xyflow/react/dist/style.css', () => ({}))

const generate = vi.fn(() => Promise.resolve({
  data: {
    draft: {
      name: 'Generated hunt',
      description: 'Look',
      phases: [],
    },
  },
}))

vi.mock('../../services/api', () => ({
  workflowApi: { generate: (...a: unknown[]) => generate(...(a as [])) },
  agentsApi: {
    listAgents: vi.fn(() => Promise.resolve({ data: { agents: [] } })),
    getAvailableTools: vi.fn(() => Promise.resolve({ data: { tools: [] } })),
  },
}))

describe('workflow builder subtitle', () => {
  it('stays a draft until generate fills the canvas', async () => {
    render(<WorkflowBuilder onClose={vi.fn()} onSaved={vi.fn()} />)
    expect(screen.getByText('Draft — not yet saved')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Generate' }))
    fireEvent.change(screen.getByPlaceholderText(/Investigate and contain/), { target: { value: 'Hunt a beacon' } })
    fireEvent.click(screen.getAllByRole('button', { name: 'Generate' })[1])

    expect(await screen.findByText('AI draft — not saved')).toBeInTheDocument()
    expect(screen.queryByText('Draft — not yet saved')).toBeNull()
  })
})
