import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import AiConfigSection from './AiConfigSection'

vi.mock('./AiProvidersPanel', () => ({ default: () => <div>Providers panel</div> }))
vi.mock('./AiModelsPanel', () => ({ default: () => <div>Models panel</div> }))
vi.mock('./AiBudgetsPanel', () => ({ default: () => <div>Keys panel</div> }))
vi.mock('./AiModelsOverview', () => ({
  AGENT_MODEL_TABLE_ID: 'ai-model-for-each-agent',
  default: () => <section id="ai-model-for-each-agent">Model for each agent</section>,
}))

function renderAt(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <AiConfigSection notify={vi.fn()} />
    </MemoryRouter>,
  )
}

describe('AiConfigSection tab from query', () => {
  it('opens the Models tab for ?tab=catalogue', () => {
    renderAt('/settings?section=ai-config&tab=catalogue')
    expect(screen.getByRole('button', { name: 'Models' })).toHaveClass('active')
    expect(screen.getByText('Models panel')).toBeInTheDocument()
  })

  it('scrolls to the per-agent model table for ?tab=assignment', () => {
    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    renderAt('/settings?section=ai-config&tab=assignment')
    expect(screen.getByText('Model for each agent')).toBeInTheDocument()
    expect(scroll).toHaveBeenCalled()
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
