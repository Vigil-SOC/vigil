import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import AiConfigSection from './AiConfigSection'

vi.mock('./AiProvidersPanel', () => ({ default: () => <div>Providers panel</div> }))
vi.mock('./AiModelsPanel', () => ({ default: () => <div>Models panel</div> }))
vi.mock('./AiBudgetsPanel', () => ({ default: () => <div>Keys panel</div> }))
vi.mock('./AiModelsOverview', () => ({ default: () => <div>Model for each agent</div> }))

function renderAt(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <AiConfigSection notify={vi.fn()} />
    </MemoryRouter>,
  )
}

describe('AiConfigSection tab from query', () => {
  it('opens Models for ?tab=catalogue', () => {
    renderAt('/settings?section=ai-config&tab=catalogue')
    expect(screen.getByRole('button', { name: 'Models' })).toHaveClass('active')
    expect(screen.getByText('Models panel')).toBeInTheDocument()
    expect(screen.queryByText('Providers panel')).not.toBeInTheDocument()
  })

  // the per-agent models are the overview above the tabs, so ?tab=assignment
  // (Home's "Pick a model per agent") lands on them whatever tab opens
  it('shows the model for each agent for ?tab=assignment', () => {
    renderAt('/settings?section=ai-config&tab=assignment')
    expect(screen.getByText('Model for each agent')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Providers & Keys' })).toHaveClass('active')
  })

  it.each(['/settings?section=ai-config', '/settings?section=ai-config&tab=nope'])(
    'falls back to Providers & Keys for %s',
    (path) => {
      renderAt(path)
      expect(screen.getByRole('button', { name: 'Providers & Keys' })).toHaveClass('active')
      expect(screen.getByText('Providers panel')).toBeInTheDocument()
    },
  )
})
