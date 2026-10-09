/* Advanced: the retry switch saves on change and gates its two follow-up rows.
   ?tab= opens the catalogue or scrolls to the per-agent table. */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render as rtlRender, screen, waitFor } from '@testing-library/react'
import type { ReactElement } from 'react'
import { MemoryRouter } from 'react-router-dom'
import AiConfigSection from './AiConfigSection'

const { setAIOperations, getAIOperations } = vi.hoisted(() => ({
  setAIOperations: vi.fn(() => Promise.resolve({})),
  getAIOperations: vi.fn(),
}))

vi.mock('../../services/api', async (orig) => ({
  ...(await orig<object>()),
  configApi: { getAIOperations: () => getAIOperations(), setAIOperations: (...a: unknown[]) => setAIOperations(...(a as [])) },
}))
vi.mock('./AiModelsOverview', () => ({
  AGENT_MODEL_TABLE_ID: 'ai-model-for-each-agent',
  default: () => <section id="ai-model-for-each-agent">Model for each agent</section>,
}))
const { keysProps } = vi.hoisted(() => ({ keysProps: vi.fn() }))
vi.mock('./AiProvidersPanel', () => ({ default: (p: object) => { keysProps(p); return <div>keys card</div> } }))
vi.mock('./AiBudgetsPanel', () => ({ default: () => <div>spending card</div> }))
vi.mock('./AiModelsPanel', () => ({
  default: ({ open, onToggle }: { open: boolean; onToggle: () => void }) => (
    <section>
      <h3>Model catalogue</h3>
      <button aria-expanded={open} onClick={onToggle}>{open ? 'Hide' : 'Show'}</button>
      {open && <div>catalogue</div>}
    </section>
  ),
}))

function render(ui: ReactElement, path = '/settings?section=ai-config') {
  return rtlRender(<MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>)
}

const ON = { local_ollama_recovery_enabled: true, local_ollama_recovery_retry_limit: 1, local_ollama_recovery_restart_gateway: true }

beforeEach(() => {
  vi.clearAllMocks()
  getAIOperations.mockResolvedValue({ data: ON })
})

describe('AiConfigSection', () => {
  it('lays out Keys, Spending limit and Advanced on the page, with no Virtual Keys or Operations tab', async () => {
    render(<AiConfigSection notify={() => {}} />)
    expect(await screen.findByText('Advanced')).toBeInTheDocument()
    expect(screen.getByText('keys card')).toBeInTheDocument()
    expect(screen.getByText('spending card')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Virtual Keys' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Operations' })).not.toBeInTheDocument()
  })

  it('saves on toggle and hides the follow-up rows while the retry is off', async () => {
    render(<AiConfigSection notify={() => {}} />)
    expect(await screen.findByText('Restart the local gateway first')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('switch', { name: /Retry a local model/ }))
    await waitFor(() => expect(setAIOperations).toHaveBeenCalledWith({ ...ON, local_ollama_recovery_enabled: false }))
    expect(screen.queryByText('Restart the local gateway first')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Retry attempts')).not.toBeInTheDocument()
  })

  it('saves the retry count on blur and resets to defaults', async () => {
    render(<AiConfigSection notify={() => {}} />)
    const n = await screen.findByLabelText('Retry attempts')
    fireEvent.change(n, { target: { value: '3' } })
    expect(setAIOperations).not.toHaveBeenCalled()
    fireEvent.blur(n)
    await waitFor(() => expect(setAIOperations).toHaveBeenCalledWith({ ...ON, local_ollama_recovery_retry_limit: 3 }))
    fireEvent.click(screen.getByRole('button', { name: /Reset to defaults/ }))
    await waitFor(() => expect(setAIOperations).toHaveBeenLastCalledWith(ON))
  })

  it('has no tab strip, and the Model catalogue is the last card, collapsed', async () => {
    render(<AiConfigSection notify={() => {}} />)
    await screen.findByText('Advanced')
    expect(screen.queryByRole('button', { name: 'Keys & limits' })).not.toBeInTheDocument()
    const toggle = screen.getByRole('button', { name: /Show/ })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('catalogue')).not.toBeInTheDocument()
    const order = ['keys card', 'spending card', 'Advanced', 'Model catalogue'].map((t) => screen.getByText(t))
    order.slice(1).forEach((el, i) => expect(order[i].compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy())
  })

  it('shows and hides the catalogue from its card head', () => {
    render(<AiConfigSection notify={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: /Show/ }))
    expect(screen.getByText('catalogue')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Hide/ }))
    expect(screen.queryByText('catalogue')).not.toBeInTheDocument()
  })

  it('opens the catalogue expanded for ?tab=catalogue', () => {
    render(<AiConfigSection notify={() => {}} />, '/settings?section=ai-config&tab=catalogue')
    expect(screen.getByRole('button', { name: /Hide/ })).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('catalogue')).toBeInTheDocument()
  })

  it('scrolls to the per-agent model table for ?tab=assignment', () => {
    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    render(<AiConfigSection notify={() => {}} />, '/settings?section=ai-config&tab=assignment')
    expect(scroll).toHaveBeenCalled()
  })

  it.each(['/settings?section=ai-config', '/settings?section=ai-config&tab=assignment', '/settings?section=ai-config&tab=nope'])(
    'leaves the catalogue collapsed for %s',
    async (path) => {
      render(<AiConfigSection notify={() => {}} />, path)
      expect(screen.getByRole('button', { name: /Show/ })).toHaveAttribute('aria-expanded', 'false')
      expect(await screen.findByText('Advanced')).toBeInTheDocument()
    },
  )

  it('puts Add provider in the page head and asks the Keys card to open its dialog once', () => {
    render(<AiConfigSection notify={() => {}} />)
    expect(screen.getByRole('heading', { name: 'AI models' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Add provider' }))
    expect(keysProps).toHaveBeenLastCalledWith(expect.objectContaining({ addProviderRequested: true }))
    act(() => keysProps.mock.lastCall![0].onAddProviderOpened())
    expect(keysProps).toHaveBeenLastCalledWith(expect.objectContaining({ addProviderRequested: false }))
  })
})
