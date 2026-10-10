/* The overview's states (loading, empty, error, populated) and the one rule that
   matters: only the chat_default row asks before it saves. */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import AiModelsOverview from './AiModelsOverview'

const bifrost = vi.fn()
const assignment = vi.fn()
const listCustom = vi.fn()
const assign = vi.fn(() => Promise.resolve())
const clearAssign = vi.fn<(component: string) => Promise<void>>(() => Promise.resolve())

const toast = vi.fn()
vi.mock('../../shell/toast', () => ({ useToast: () => toast() }))
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

const rowOf = (row: string) => screen.getByText(row).closest('tr') as HTMLElement
const notify = vi.fn()
const notifyUndoable = vi.fn<(o: { key: string; commit: () => Promise<unknown> }) => void>()
const mount = () => render(<AiModelsOverview notify={notify} />)

beforeEach(() => {
  vi.clearAllMocks()
  bifrost.mockReturnValue(providersReady())
  assignment.mockReturnValue(assignmentsReady())
  toast.mockReturnValue({ notify, notifyUndoable, pending: [], settled: 0 })
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
    expect(screen.getAllByText('Hosted: data leaves your site')).toHaveLength(2)
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
    const cell = rowOf(row)
    fireEvent.click(within(cell).getByRole('button', { name: 'Change' }))
    // the second select in the editing row is the model
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
    await waitFor(() => expect(assign).toHaveBeenCalledWith('triage', 'anthropic-default', 'haiku', {}))
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
    await waitFor(() => expect(assign).toHaveBeenCalledWith('chat_default', 'anthropic-default', 'haiku', {}))
  })

  const pickEffort = (row: string, effort: string) => {
    const cell = screen.getByText(row).closest('tr') as HTMLElement
    // outside edit mode the row's selects are the fallback, then the reasoning effort
    fireEvent.click(cell.querySelectorAll('button.field-select')[1])
    fireEvent.click(screen.getByRole('option', { name: effort }))
  }

  it('sets a row\'s reasoning effort, keeping its other settings, and drops the key for Model default', async () => {
    assignment.mockReturnValue(
      assignmentsReady({
        assignments: {
          chat_default: { component: 'chat_default', provider_id: 'anthropic-default', model_id: 'sonnet' },
          triage: { component: 'triage', provider_id: 'anthropic-default', model_id: 'sonnet', settings: { temperature: 0.2, effort: 'low' } },
        },
      }),
    )
    mount()
    const cell = screen.getByText('Triage Agent').closest('tr') as HTMLElement
    expect(cell.textContent).toContain('Low')
    pickEffort('Triage Agent', 'High')
    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith('triage', 'anthropic-default', 'sonnet', { temperature: 0.2, effort: 'high' }),
    )
    pickEffort('Triage Agent', 'Model default')
    await waitFor(() => expect(assign).toHaveBeenLastCalledWith('triage', 'anthropic-default', 'sonnet', { temperature: 0.2 }))
  })

  it('shows the saved model as text with its provider, and a default chip on a row that inherits', () => {
    assignment.mockReturnValue(
      assignmentsReady({
        assignments: {
          chat_default: { component: 'chat_default', provider_id: 'anthropic-default', model_id: 'sonnet' },
          triage: { component: 'triage', provider_id: 'anthropic-default', model_id: 'haiku' },
        },
        components: ['chat_default', 'triage', 'reporting'],
      }),
    )
    mount()
    expect(within(rowOf('Triage Agent')).getByText('Haiku')).toBeTruthy()
    expect(within(rowOf('Triage Agent')).queryByText('default')).toBeNull()
    expect(within(rowOf('Chat (Default)')).queryByText('default')).toBeNull()
    expect(rowOf('Chat (Default)').querySelector('button.field-select')).not.toBeNull()
    expect(within(rowOf('Report Generation')).getByText('Sonnet')).toBeTruthy()
    expect(within(rowOf('Report Generation')).getByText('default')).toBeTruthy()
  })

  it('Change swaps the cell into selects with Use default first, and Escape returns to text', () => {
    assignment.mockReturnValue(
      assignmentsReady({
        assignments: {
          chat_default: { component: 'chat_default', provider_id: 'anthropic-default', model_id: 'sonnet' },
          triage: { component: 'triage', provider_id: 'anthropic-default', model_id: 'haiku' },
        },
      }),
    )
    mount()
    const row = rowOf('Triage Agent')
    fireEvent.click(within(row).getByRole('button', { name: 'Change' }))
    expect(row.querySelectorAll('button.field-select')).toHaveLength(4)
    fireEvent.click(row.querySelectorAll('button.field-select')[0])
    expect(screen.getAllByRole('option')[0].textContent).toBe('Use default')
    fireEvent.keyDown(document.body, { key: 'Escape' }) // closes the open menu
    expect(row.querySelector('.aim-model')).not.toBeNull()
    fireEvent.keyDown(document.body, { key: 'Escape' })
    expect(row.querySelector('.aim-model')).toBeNull()
    expect(within(row).getByText('Haiku')).toBeTruthy()
    expect(assign).not.toHaveBeenCalled()
    expect(within(row).getByRole('button', { name: 'Change' })).toBeTruthy()
  })

  it('offers no Use default on Chat (Default)', () => {
    mount()
    const row = rowOf('Chat (Default)')
    fireEvent.click(within(row).getByRole('button', { name: 'Change' }))
    fireEvent.click(row.querySelectorAll('button.field-select')[0])
    expect(screen.queryByRole('option', { name: 'Use default' })).toBeNull()
  })

  it('Use default clears the row\'s assignment', async () => {
    assignment.mockReturnValue(
      assignmentsReady({
        assignments: {
          chat_default: { component: 'chat_default', provider_id: 'anthropic-default', model_id: 'sonnet' },
          triage: { component: 'triage', provider_id: 'anthropic-default', model_id: 'haiku' },
        },
      }),
    )
    mount()
    const row = rowOf('Triage Agent')
    fireEvent.click(within(row).getByRole('button', { name: 'Change' }))
    fireEvent.click(row.querySelectorAll('button.field-select')[0])
    fireEvent.click(screen.getByRole('option', { name: 'Use default' }))
    await waitFor(() => expect(clearAssign).toHaveBeenCalledWith('triage'))
  })

  it('offers no effort on a row that uses the default', () => {
    mount()
    const cell = screen.getByText('Triage Agent').closest('tr') as HTMLElement
    expect(cell.querySelectorAll('button.field-select')).toHaveLength(1)
  })
})

describe('a row that uses the default', () => {
  it('shows the default’s model as text with a chip, or Not set when there is no default', () => {
    const { unmount } = mount()
    expect(within(rowOf('Triage Agent')).getByText('Sonnet')).toBeTruthy()
    expect(within(rowOf('Triage Agent')).getByText('default')).toBeTruthy()
    unmount()
    assignment.mockReturnValue(assignmentsReady({ assignments: {} }))
    mount()
    expect(within(rowOf('Triage Agent')).getByText('Not set')).toBeTruthy()
  })

  it('persists a pick made through Change', async () => {
    mount()
    const row = rowOf('Triage Agent')
    fireEvent.click(within(row).getByRole('button', { name: 'Change' }))
    fireEvent.click(row.querySelectorAll('button.field-select')[0])
    fireEvent.click(screen.getByRole('option', { name: 'anthropic-default' }))
    fireEvent.click(row.querySelectorAll('button.field-select')[1])
    fireEvent.click(screen.getByRole('option', { name: 'Haiku' }))
    await waitFor(() => expect(assign).toHaveBeenCalledWith('triage', 'anthropic-default', 'haiku', {}))
  })
})

describe('fallback for the built-in components', () => {
  const withFallback = (fb?: string) =>
    assignmentsReady({
      assignments: {
        chat_default: {
          component: 'chat_default',
          provider_id: 'anthropic-default',
          model_id: 'sonnet',
          settings: fb ? { fallback_model_id: fb } : {},
        },
      },
    })
  // the first select in a row outside edit mode
  const fallbackTrigger = (row: string) =>
    (screen.getByText(row).closest('tr') as HTMLElement).querySelectorAll('button.field-select')[0] as HTMLButtonElement

  it('shows Stops when unset, disabled for a row on Use default or with no model', () => {
    assignment.mockReturnValue(withFallback())
    mount()
    expect(fallbackTrigger('Chat (Default)').textContent).toBe('Stops')
    expect(fallbackTrigger('Chat (Default)').disabled).toBe(false)
    expect(fallbackTrigger('Triage Agent').textContent).toBe('Stops')
    expect(fallbackTrigger('Triage Agent').disabled).toBe(true)
  })

  it('lists the same provider’s other models, shows the stored one, and a row on Use default shows the default’s', () => {
    assignment.mockReturnValue(withFallback('haiku'))
    mount()
    expect(fallbackTrigger('Chat (Default)').textContent).toBe('Haiku')
    expect(fallbackTrigger('Triage Agent').textContent).toBe('Haiku')
    fireEvent.click(fallbackTrigger('Chat (Default)'))
    expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual(['Stops', 'Haiku'])
  })

  it('saves on select without the default-model confirm, and Stops clears it', async () => {
    assignment.mockReturnValue(withFallback())
    const { unmount } = mount()
    fireEvent.click(fallbackTrigger('Chat (Default)'))
    fireEvent.click(screen.getByRole('option', { name: 'Haiku' }))
    await waitFor(() => expect(assign).toHaveBeenCalledWith('chat_default', 'anthropic-default', 'sonnet', { fallback_model_id: 'haiku' }))
    expect(screen.queryByText('Change the default model?')).toBeNull()
    unmount()

    assignment.mockReturnValue(withFallback('haiku'))
    mount()
    fireEvent.click(fallbackTrigger('Chat (Default)'))
    fireEvent.click(screen.getByRole('option', { name: 'Stops' }))
    await waitFor(() => expect(assign).toHaveBeenCalledWith('chat_default', 'anthropic-default', 'sonnet', { fallback_model_id: null }))
  })

  it('reports a failed save', async () => {
    assignment.mockReturnValue(withFallback())
    assign.mockRejectedValueOnce(new Error('nope'))
    mount()
    fireEvent.click(fallbackTrigger('Chat (Default)'))
    fireEvent.click(screen.getByRole('option', { name: 'Haiku' }))
    await waitFor(() => expect(notify).toHaveBeenCalledWith('err', 'nope'))
  })
})

describe('reset to defaults', () => {
  const own = {
    chat_default: { component: 'chat_default', provider_id: 'anthropic-default', model_id: 'sonnet' },
    triage: { component: 'triage', provider_id: 'anthropic-default', model_id: 'haiku' },
    reporting: { component: 'reporting', provider_id: 'anthropic-default', model_id: 'haiku' },
  }
  const withOwn = () =>
    assignment.mockReturnValue(assignmentsReady({ assignments: own, components: ['chat_default', 'triage', 'reporting'] }))
  const resetButton = () => screen.getByRole('button', { name: 'Reset to defaults' }) as HTMLButtonElement

  it('is disabled when no row has its own assignment', () => {
    mount()
    expect(resetButton().disabled).toBe(true)
  })

  it('starts one undo fuse whose commit clears every own row but Chat (Default)', async () => {
    withOwn()
    mount()
    fireEvent.click(resetButton())
    expect(notifyUndoable).toHaveBeenCalledTimes(1)
    const opts = notifyUndoable.mock.calls[0][0]
    expect(opts.key).toBe('ai-model-reset')
    expect(clearAssign).not.toHaveBeenCalled()
    await opts.commit()
    expect(clearAssign.mock.calls.map((c) => c[0]).sort()).toEqual(['reporting', 'triage'])
  })

  it('shows the pending rows as Use default while the fuse runs, and reloads once it settles', () => {
    withOwn()
    const reload = vi.fn()
    assignment.mockReturnValue(assignmentsReady({ assignments: own, components: ['chat_default', 'triage', 'reporting'], reload }))
    toast.mockReturnValue({ notify, notifyUndoable, pending: ['ai-model-reset'], settled: 0 })
    const { rerender } = mount()
    expect(within(rowOf('Triage Agent')).getByText('default')).toBeTruthy()
    expect(within(rowOf('Chat (Default)')).queryByText('default')).toBeNull()
    expect(resetButton().disabled).toBe(true)
    expect(reload).not.toHaveBeenCalled()

    toast.mockReturnValue({ notify, notifyUndoable, pending: [], settled: 1 })
    rerender(<AiModelsOverview notify={notify} />)
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('closes an open editor on a row about to be reset, so it cannot save into the reset', () => {
    withOwn()
    mount()
    fireEvent.click(within(rowOf('Triage Agent')).getByRole('button', { name: 'Change' }))
    expect(rowOf('Triage Agent').querySelector('.aim-model')).not.toBeNull()
    fireEvent.click(resetButton())
    expect(rowOf('Triage Agent').querySelector('.aim-model')).toBeNull()
  })
})
