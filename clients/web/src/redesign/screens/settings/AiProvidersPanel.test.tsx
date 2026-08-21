/* The allow-list picker. Bifrost's /api/models?provider=X returns the *routable*
   set, not the catalogue: unfenced that is everything, but once a key is fenced it
   is only the fence. So a picker that trusted it alone could narrow a fence and
   never widen one, and would silently drop models it was not shown. */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import AiProvidersPanel from './AiProvidersPanel'

const listProviders = vi.fn()
const listKeys = vi.fn()
const providerModels = vi.fn()
const modelParameters = vi.fn()
const createKey = vi.fn(() => Promise.resolve({ data: { id: 'k1', status: 'success' } }))
const updateKey = vi.fn(() => Promise.resolve({ data: { id: 'k1', status: 'success' } }))

vi.mock('../../../services/bifrostApi', () => ({
  bifrostApi: {
    listProviders: () => listProviders(),
    listKeys: (...a: unknown[]) => listKeys(...(a as [])),
    providerModels: (...a: unknown[]) => providerModels(...(a as [])),
    modelParameters: (...a: unknown[]) => modelParameters(...(a as [])),
    createKey: (...a: unknown[]) => createKey(...(a as [])),
    updateKey: (...a: unknown[]) => updateKey(...(a as [])),
    createProvider: vi.fn(),
    removeProvider: vi.fn(),
    removeKey: vi.fn(),
  },
  secretText: (v: unknown) => (typeof v === 'string' ? v : ((v as { value?: string })?.value ?? '')),
}))

// Fenced to two of the four the provider actually has.
const FENCED = {
  id: 'k1',
  name: 'anthropic-key',
  value: { value: 'sk-a****key', env_var: '', from_env: false },
  models: ['claude-sonnet-5', 'claude-opus-5'],
  weight: 1,
  enabled: true,
  status: 'success',
}

const mount = async (keys: unknown[], routable: string[]) => {
  listProviders.mockResolvedValue({ data: { providers: [{ name: 'anthropic' }] } })
  listKeys.mockResolvedValue({ data: { keys, total: keys.length } })
  providerModels.mockResolvedValue({
    data: { models: routable.map((name) => ({ name, provider: 'anthropic' })), total: routable.length },
  })
  render(<AiProvidersPanel notify={() => {}} />)
  await screen.findByText('anthropic')
}

const openEditor = async () => {
  fireEvent.click(screen.getByTitle('Expand'))
  fireEvent.click(await screen.findByTitle('Edit'))
  await screen.findByText('Key name')
}

describe('the models a key is fenced to', () => {
  it('offers the catalogue as checkboxes rather than a field to type ids into', async () => {
    // Unfenced provider: the routable set is the whole catalogue.
    await mount([{ ...FENCED, models: ['*'] }], ['claude-sonnet-5', 'claude-opus-5', 'claude-haiku-4-5-20251001'])
    await openEditor()

    // Allow-all is on for a `*` key, so the picker is not shown until it is off.
    fireEvent.click(screen.getByRole('switch', { name: 'Allow all models' }))

    const boxes = await screen.findAllByRole('checkbox')
    expect(boxes).toHaveLength(3)
    expect(screen.getByText('claude-haiku-4-5-20251001')).toBeInTheDocument()
  })

  it('keeps a fenced key’s own models even when the gateway stops listing them', async () => {
    // The trap: this key is fenced to two, and the routable call echoes only one of
    // them back. The other must not vanish just because it was not in the response.
    await mount([FENCED], ['claude-sonnet-5'])
    await openEditor()

    expect(await screen.findByText('claude-opus-5')).toBeInTheDocument()
    expect(screen.getByText(/2 selected/)).toBeInTheDocument()
  })

  it('accepts a model outside the fence once the gateway confirms it exists', async () => {
    modelParameters.mockResolvedValue({ data: { input_cost_per_token: 0.000002 } })
    await mount([FENCED], ['claude-sonnet-5'])
    await openEditor()

    fireEvent.change(screen.getByPlaceholderText(/Add a model the list does not show/), {
      target: { value: 'claude-opus-4-1' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))

    await waitFor(() => expect(modelParameters).toHaveBeenCalledWith('claude-opus-4-1', 'anthropic'))
    expect(await screen.findByText('claude-opus-4-1')).toBeInTheDocument()
    expect(screen.getByText(/3 selected/)).toBeInTheDocument()
  })

  it('refuses a model the gateway does not know rather than saving a dead id', async () => {
    modelParameters.mockRejectedValue({ response: { status: 404 } })
    await mount([FENCED], ['claude-sonnet-5'])
    await openEditor()

    fireEvent.change(screen.getByPlaceholderText(/Add a model the list does not show/), {
      target: { value: 'claude-imaginary-9' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))

    expect(await screen.findByText(/has no model called "claude-imaginary-9"/)).toBeInTheDocument()
    expect(screen.getByText(/2 selected/)).toBeInTheDocument()
  })

  it('saves the ticked ids, not a parsed blob of text', async () => {
    await mount([FENCED], ['claude-sonnet-5', 'claude-opus-5'])
    await openEditor()

    fireEvent.click(screen.getByLabelText('claude-opus-5'))
    fireEvent.click(screen.getByRole('button', { name: /Save/ }))

    await waitFor(() => expect(updateKey).toHaveBeenCalled())
    const [, , payload] = updateKey.mock.calls[0] as unknown as [string, string, { models: string[]; value?: string }]
    expect(payload.models).toEqual(['claude-sonnet-5'])
    // No credential was retyped, so none is sent — the proxy substitutes the stored one.
    expect(payload.value).toBeUndefined()
  })
})
