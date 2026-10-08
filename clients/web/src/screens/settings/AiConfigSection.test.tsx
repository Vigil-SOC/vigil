import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import AiConfigSection from './AiConfigSection'

vi.mock('./AiProvidersPanel', () => ({ default: () => <div>Providers panel</div> }))
vi.mock('./AiModelsPanel', () => ({ default: () => <div>Models panel</div> }))
vi.mock('./AiBudgetsPanel', () => ({ default: () => <div>Keys panel</div> }))
vi.mock('./useSettings', async (orig) => ({
  ...(await orig<typeof import('./useSettings')>()),
  useModelAssignment: () => ({
    components: [], assignments: {}, models: [], phase: 'loading', error: null,
    reload: vi.fn(), assign: vi.fn(), clearAssign: vi.fn(),
  }),
}))
vi.mock('../../services/api', () => ({ agentsApi: { listCustom: vi.fn(() => new Promise(() => {})) } }))

function renderAt(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <AiConfigSection notify={vi.fn()} />
    </MemoryRouter>,
  )
}

describe('AiConfigSection tab from query', () => {
  it('opens Model Assignment for ?tab=assignment', () => {
    renderAt('/settings?section=ai-config&tab=assignment')
    expect(screen.getByRole('button', { name: 'Model Assignment' })).toHaveClass('active')
    expect(screen.queryByText('Providers panel')).not.toBeInTheDocument()
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
