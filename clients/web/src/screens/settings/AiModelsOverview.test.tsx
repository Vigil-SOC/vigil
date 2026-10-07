/* The overview's states (loading, empty, error, populated) and the one rule that
   matters: only the chat_default row asks before it saves. */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import AiModelsOverview from './AiModelsOverview'

const bifrost = vi.fn()
const assignment = vi.fn()
const listCustom = vi.fn()
const assign = vi.fn(() => Promise.resolve())
const clearAssign = vi.fn(() => Promise.resolve())

vi.mock('./useBifrost', () => ({ useBifrostProviders: () => bifrost() }))
vi.mock('./useSettings', () => ({ useModelAssignment: () => assignment() }))
vi.mock('../../services/api', () => ({ agentsApi: { listCustom: () => listCustom(), updateCustom: vi.fn() } }))

const key = (url?: string) => ({ id: 'k', name: 'k', ollama_key_config: url ? { url } : undefined })
const providersReady = (over = {}) => ({
  providers: [{ name: 'anthropic' }, { name: 'ollama' }, { name: 'bedrock' }],
  keys: { anthropic: [key(), key()], ollama: [key('http://localhost:11434')], bedrock: [] },
  verdicts: { providers: { anthropic: true, ollama: false, bedrock: false }, keys: {} },
  phase: 'ready',
  error: null,
  reload: vi.fn(),
  ...over,
})
const MODELS = [
  { model_id: 'sonnet', provider_id: 'anthropic-default', provider_type: 'anthropic', display_name: 'Sonnet' },
  { model_id: 'haiku', provider_id: 'anthropic-default', provider_type: 'anthropic', display_name: 'Haiku' },
]
const assignmentsReady = (over = {}) => ({
  components: ['chat_default', 'triage'],
  assignments: { chat_default: { component: 'chat_default', provider_id: 'anthropic-default', model_id: 'sonnet' } },
  models: MODELS,
  phase: 'ready',
  error: null,
  reload: vi.fn(),
  assign,
  clearAssign,
  ...over,
})

const mount = () => render(<AiModelsOverview notify={vi.fn()} />)

beforeEach(() => {
  vi.clearAllMocks()
  bifrost.mockReturnValue(providersReady())
  assignment.mockReturnValue(assignmentsReady())
  listCustom.mockResolvedValue({ data: { agents: [] } })
})

describe('provider cards', () => {
  it('shows loading, error and empty states', () => {
    bifrost.mockReturnValue(providersReady({ phase: 'loading' }))
    const { unmount } = mount()
    expect(screen.getByText('Loading providers…')).toBeTruthy()
    unmount()

    bifrost.mockReturnValue(providersReady({ phase: 'error', error: 'gateway down' }))
    const second = mount()
    expect(screen.getByText('gateway down')).toBeTruthy()
    second.unmount()

    bifrost.mockReturnValue(providersReady({ providers: [], keys: {} }))
    mount()
    expect(screen.getByText('No providers yet')).toBeTruthy()
  })

  it('marks the chat_default provider, badges Good or Poor, and says where data goes', () => {
    mount()
    expect(screen.getByText('· default')).toBeTruthy()
    expect(screen.getAllByText('Good')).toHaveLength(1)
    expect(screen.getAllByText('Poor')).toHaveLength(2)
    expect(screen.getByText('2 keys · via the gateway')).toBeTruthy()
    expect(screen.getByText('Not set up')).toBeTruthy()
    expect(screen.getAllByText('Hosted: data leaves the site')).toHaveLength(2)
    expect(screen.getByText('Local: data stays on site')).toBeTruthy()
    expect(screen.getByText('Where your data goes:').parentElement?.textContent).toMatch(/Changing the default model asks you to confirm and is logged\.$/)
  })

  it('gives no verdict, not Poor, when routability could not be asked', () => {
    bifrost.mockReturnValue(providersReady({ verdicts: null }))
    mount()
    expect(screen.queryByText('Poor')).toBeNull()
    expect(screen.queryByText('Good')).toBeNull()
  })
})

describe('model for each agent', () => {
  it('shows loading, error and no-models states', () => {
    assignment.mockReturnValue(assignmentsReady({ phase: 'loading' }))
    const { unmount } = mount()
    expect(screen.getByText('Loading AI config…')).toBeTruthy()
    unmount()

    assignment.mockReturnValue(assignmentsReady({ phase: 'error', error: 'boom' }))
    const second = mount()
    expect(screen.getByText('boom')).toBeTruthy()
    second.unmount()

    assignment.mockReturnValue(assignmentsReady({ models: [] }))
    mount()
    expect(screen.getByText('No assignable models discovered')).toBeTruthy()
  })

  it('lists components and custom agents, with thinking read-only and fallback "Stops" when unset', async () => {
    listCustom.mockResolvedValue({
      data: { agents: [{ id: 'custom-a', name: 'Phishing', model: 'haiku', fallback_model: null, enable_thinking: true }] },
    })
    mount()
    expect(screen.getByText('Chat (Default)')).toBeTruthy()
    expect(screen.getByText('Triage Agent')).toBeTruthy()
    expect(await screen.findByText('Phishing')).toBeTruthy()
    expect(screen.getByPlaceholderText('Stops')).toBeTruthy()
    expect(screen.getByText('On')).toBeTruthy()
    expect(screen.getAllByText('Not measured yet')).toHaveLength(3)
  })

  it('keeps the component rows and offers a retry when custom agents fail to load', async () => {
    listCustom.mockRejectedValue(new Error('nope'))
    mount()
    expect(await screen.findByText('Couldn’t load custom agents')).toBeTruthy()
    expect(screen.getByText('Triage Agent')).toBeTruthy()
  })

  const pickModel = (row: string, model: string) => {
    const cell = screen.getByText(row).closest('tr') as HTMLElement
    // the second select in the row is the model
    fireEvent.click(cell.querySelectorAll('button.field-select')[1])
    fireEvent.click(screen.getByRole('option', { name: model }))
  }

  it('saves a non-default row on select with no dialog', async () => {
    assignment.mockReturnValue(
      assignmentsReady({
        assignments: {
          chat_default: { component: 'chat_default', provider_id: 'anthropic-default', model_id: 'sonnet' },
          triage: { component: 'triage', provider_id: 'anthropic-default', model_id: 'sonnet' },
        },
      }),
    )
    mount()
    pickModel('Triage Agent', 'Haiku')
    await waitFor(() => expect(assign).toHaveBeenCalledWith('triage', 'anthropic-default', 'haiku'))
    expect(screen.queryByText('Change the default model?')).toBeNull()
  })

  it('asks before changing chat_default, and cancelling saves nothing', async () => {
    mount()
    pickModel('Chat (Default)', 'Haiku')
    expect(await screen.findByText('Change the default model?')).toBeTruthy()
    expect(assign).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(assign).not.toHaveBeenCalled()
    expect(screen.queryByText('Change the default model?')).toBeNull()
  })

  it('saves chat_default once confirmed', async () => {
    mount()
    pickModel('Chat (Default)', 'Haiku')
    fireEvent.click(await screen.findByRole('button', { name: 'Change default' }))
    await waitFor(() => expect(assign).toHaveBeenCalledWith('chat_default', 'anthropic-default', 'haiku'))
  })
})
