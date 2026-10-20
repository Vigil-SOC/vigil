/* The allow-list picker. Bifrost's /api/models?provider=X returns the *routable*
   set, not the catalogue: unfenced that is everything, but once a key is fenced it
   is only the fence. So a picker that trusted it alone could narrow a fence and
   never widen one, and would silently drop models it was not shown. */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import AiProvidersPanel from './AiProvidersPanel'

const listProviders = vi.fn()
const listKeys = vi.fn()
const providerModels = vi.fn()
const modelParameters = vi.fn()
const createKey = vi.fn(() => Promise.resolve({ data: { id: 'k1', status: 'success' } }))
const updateKey = vi.fn(() => Promise.resolve({ data: { id: 'k1', status: 'success' } }))
const routability = vi.fn()
const createProvider = vi.fn(() => Promise.resolve({ data: { name: 'openai' } }))
const setProviderBaseUrl = vi.fn(() => Promise.resolve({ data: {} }))
const removeProvider = vi.fn(() => Promise.resolve({ data: {} }))

// This mock replaces the module wholesale, so anything the panel reaches for and
// does not find here is `undefined` at the call site rather than a missing-export
// error — which surfaces as the panel's generic failure state and says nothing
// about the cause. Keep it in step with what AiProvidersPanel and useBifrost
// import.
vi.mock('../../services/bifrostApi', () => ({
  bifrostApi: {
    listProviders: () => listProviders(),
    listKeys: (...a: unknown[]) => listKeys(...(a as [])),
    providerModels: (...a: unknown[]) => providerModels(...(a as [])),
    modelParameters: (...a: unknown[]) => modelParameters(...(a as [])),
    createKey: (...a: unknown[]) => createKey(...(a as [])),
    updateKey: (...a: unknown[]) => updateKey(...(a as [])),
    routability: () => routability(),
    createProvider: (...a: unknown[]) => createProvider(...(a as [])),
    setProviderBaseUrl: (...a: unknown[]) => setProviderBaseUrl(...(a as [])),
    removeProvider: (...a: unknown[]) => removeProvider(...(a as [])),
    removeKey: vi.fn(),
  },
  secretText: (v: unknown) => (typeof v === 'string' ? v : ((v as { value?: string })?.value ?? '')),
  isMasked: (v: unknown) =>
    (typeof v === 'string' ? v : ((v as { value?: string })?.value ?? '')).includes('*'),
  keyRefusal: () => Promise.resolve(null),
  secretEnvRef: (v: unknown) =>
    typeof v === 'object' && (v as { from_env?: boolean })?.from_env
      ? ((v as { env_var?: string }).env_var ?? '')
      : '',
  COMMON_PROVIDERS: ['anthropic', 'openai', 'ollama', 'vertex'],
  BASE_URL_PROVIDERS: ['openai', 'anthropic'],
  CUSTOM_PROVIDER_NAME: /^[a-z0-9][a-z0-9-]{0,39}$/,
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

const mount = async (keys: unknown[], routable: string[], health = 'healthy') => {
  listProviders.mockResolvedValue({ data: { providers: [{ name: 'anthropic' }] } })
  listKeys.mockResolvedValue({ data: { keys, total: keys.length } })
  providerModels.mockResolvedValue({
    data: { models: routable.map((name) => ({ name, provider: 'anthropic' })), total: routable.length },
  })
  routability.mockResolvedValue({
    data: {
      providers: { anthropic: keys.length > 0 },
      keys: Object.fromEntries(
        (keys as { id?: string }[])
          .filter((k) => k.id)
          .map((k) => [k.id, { provider: 'anthropic', routable: health === 'healthy', health }]),
      ),
    },
  })
  render(<AiProvidersPanel notify={() => {}} />)
  await screen.findByText(keys.length ? 'anthropic-key' : /No key/)
}

const openEditor = async () => {
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

/* Bifrost omits `enabled` from its read shape entirely. That is not an
   omitempty elision -- the same payload serialises `use_for_batch_api: false`
   and `use_anthropic_endpoints: false` -- so `!k.enabled` was true for every
   key the gateway returned, and every key rendered as "Disabled". */
describe('the “Disabled” badge', () => {
  const expand = () => screen.findByText('anthropic-key')

  it('stays hidden for a key Bifrost returns without an `enabled` field', async () => {
    const withoutEnabled: Record<string, unknown> = { ...FENCED }
    delete withoutEnabled.enabled

    await mount([withoutEnabled], ['claude-sonnet-5'])
    await expand()

    expect(screen.queryByText(/Disabled/)).not.toBeInTheDocument()
  })

  it('still shows for a key that was explicitly disabled', async () => {
    await mount([{ ...FENCED, enabled: false }], ['claude-sonnet-5'])
    await expand()

    expect(screen.getByText(/anthropic · Disabled/)).toBeInTheDocument()
  })
})

describe('the keys table', () => {
  it('lists the board’s columns with a Good badge and the key’s share of its provider', async () => {
    await mount([FENCED, { ...FENCED, id: 'k2', name: 'anthropic-backup', weight: 3, models: ['*'] }], [])
    for (const h of ['Key', 'Credential', 'Share', 'Models allowed', 'Health']) {
      expect(screen.getByRole('columnheader', { name: h })).toBeInTheDocument()
    }
    expect(screen.getByText('25%')).toBeInTheDocument()
    expect(screen.getByText('75%')).toBeInTheDocument()
    expect(screen.getByText('2 allowed')).toBeInTheDocument()
    expect(screen.getByText('All models')).toBeInTheDocument()
    expect(screen.getAllByText('Good')).toHaveLength(2)
  })

  it('shows a rejected key as Poor, and never invents Fair', async () => {
    await mount([FENCED], [], 'rejected')
    expect(screen.getByText('Poor')).toBeInTheDocument()
    expect(screen.queryByText('Fair')).not.toBeInTheDocument()
  })

  it('draws no health at all when the verdicts could not be read', async () => {
    listProviders.mockResolvedValue({ data: { providers: [{ name: 'anthropic' }] } })
    listKeys.mockResolvedValue({ data: { keys: [FENCED], total: 1 } })
    providerModels.mockResolvedValue({ data: { models: [], total: 0 } })
    routability.mockRejectedValue(new Error('down'))
    render(<AiProvidersPanel notify={() => {}} />)
    await screen.findByText('anthropic-key')
    expect(screen.getByText('Couldn’t check whether these keys can route')).toBeInTheDocument()
    expect(screen.queryByText('Poor')).not.toBeInTheDocument()
    expect(screen.queryByText('Good')).not.toBeInTheDocument()
    expect(screen.queryByText('Unverified')).not.toBeInTheDocument()
  })

  it('says so when the gateway cannot be reached, and when there are no providers', async () => {
    listProviders.mockRejectedValue(new Error('refused'))
    listKeys.mockResolvedValue({ data: { keys: [], total: 0 } })
    routability.mockResolvedValue({ data: { providers: {}, keys: {} } })
    const { unmount } = render(<AiProvidersPanel notify={() => {}} />)
    expect(await screen.findByText('Couldn’t reach the Bifrost gateway')).toBeInTheDocument()
    unmount()

    listProviders.mockResolvedValue({ data: { providers: [] } })
    render(<AiProvidersPanel notify={() => {}} />)
    expect(await screen.findByText('No providers configured')).toBeInTheDocument()
  })

  it('leaves out a provider whose only key the backend calls a placeholder', async () => {
    listProviders.mockResolvedValue({ data: { providers: [{ name: 'openai' }] } })
    listKeys.mockResolvedValue({
      data: { keys: [{ id: 's1', name: 'default-openai-key', models: ['*'], weight: 1, value: { value: '', ref: 'env.OPENAI_API_KEY', type: 'env' } }], total: 1 },
    })
    routability.mockResolvedValue({ data: { providers: { openai: false }, keys: { s1: { provider: 'openai', routable: false, health: 'placeholder' } } } })
    render(<AiProvidersPanel notify={() => {}} />)
    expect(await screen.findByText('No providers configured')).toBeInTheDocument()
    expect(screen.queryByText('default-openai-key')).not.toBeInTheDocument()
  })

  it('lists a provider with no key and offers to add one', async () => {
    await mount([], [])
    expect(screen.getByText(/No key — this provider cannot route/)).toBeInTheDocument()
    expect(screen.getByTitle('Delete provider')).toBeInTheDocument()
  })
})

describe('the page head’s Add provider', () => {
  it('opens the dialog once when asked, and the Keys card keeps Add key but not Add provider', async () => {
    listProviders.mockResolvedValue({ data: { providers: [{ name: 'anthropic' }] } })
    listKeys.mockResolvedValue({ data: { keys: [], total: 0 } })
    providerModels.mockResolvedValue({ data: { models: [], total: 0 } })
    routability.mockResolvedValue({ data: { providers: {}, keys: {} } })
    const opened = vi.fn()
    render(<AiProvidersPanel notify={() => {}} addProviderRequested onAddProviderOpened={opened} />)
    expect(await screen.findByRole('dialog', { name: 'Add provider' })).toBeInTheDocument()
    expect(opened).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('button', { name: 'Add provider' })).not.toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Add key' }).length).toBeGreaterThan(0)
  })
})

describe('a provider’s base URL', () => {
  const mountProviders = async (providers: unknown[]) => {
    listProviders.mockResolvedValue({ data: { providers } })
    listKeys.mockResolvedValue({ data: { keys: [], total: 0 } })
    routability.mockResolvedValue({ data: { providers: {}, keys: {} } })
    render(<AiProvidersPanel notify={notify} addProviderRequested onAddProviderOpened={() => {}} />)
    await screen.findByRole('dialog', { name: 'Add provider' })
  }
  const notify = vi.fn()

  it('is written right after the provider is created', async () => {
    await mountProviders([{ name: 'anthropic' }])
    fireEvent.change(screen.getByPlaceholderText('anthropic'), { target: { value: 'openai' } })
    fireEvent.change(screen.getByPlaceholderText(/llm\.internal\.example/), {
      target: { value: ' http://10.0.0.5:8000/v1 ' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))

    await waitFor(() => expect(setProviderBaseUrl).toHaveBeenCalledWith('openai', 'http://10.0.0.5:8000/v1'))
    expect(createProvider).toHaveBeenCalledWith('openai')
  })

  it('is not offered for a provider whose URL lives on its key', async () => {
    await mountProviders([{ name: 'anthropic' }])
    fireEvent.change(screen.getByPlaceholderText('anthropic'), { target: { value: 'ollama' } })
    expect(screen.queryByPlaceholderText(/llm\.internal\.example/)).not.toBeInTheDocument()
  })

  it('shows the stored host on the provider and edits it in place', async () => {
    listProviders.mockResolvedValue({
      data: { providers: [{ name: 'openai', network_config: { base_url: 'http://10.0.0.5:8000' } }] },
    })
    listKeys.mockResolvedValue({ data: { keys: [], total: 0 } })
    routability.mockResolvedValue({ data: { providers: {}, keys: {} } })
    render(<AiProvidersPanel notify={() => {}} />)
    expect(await screen.findByText(/http:\/\/10\.0\.0\.5:8000/)).toBeInTheDocument()

    fireEvent.click(screen.getByTitle('Edit provider'))
    const field = await screen.findByDisplayValue('http://10.0.0.5:8000')
    fireEvent.change(field, { target: { value: 'http://10.0.0.6:9000' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(setProviderBaseUrl).toHaveBeenCalledWith('openai', 'http://10.0.0.6:9000'))
  })
})

describe('a custom OpenAI-compatible provider', () => {
  const custom = (name: string, url: string, keyLess = false) => ({
    name,
    network_config: { base_url: url },
    custom_provider_config: { base_provider_type: 'openai', is_key_less: keyLess },
  })

  it('is created with its own name, host and key-less flag in one write', async () => {
    listProviders.mockResolvedValue({ data: { providers: [{ name: 'anthropic' }] } })
    listKeys.mockResolvedValue({ data: { keys: [], total: 0 } })
    routability.mockResolvedValue({ data: { providers: {}, keys: {} } })
    render(<AiProvidersPanel notify={() => {}} addProviderRequested onAddProviderOpened={() => {}} />)
    await screen.findByRole('dialog', { name: 'Add provider' })

    fireEvent.click(screen.getByRole('button', { name: 'Custom (OpenAI-compatible)' }))
    const add = screen.getByRole('button', { name: 'Add' })
    expect(add).toBeDisabled()
    fireEvent.change(screen.getByPlaceholderText('lan-vllm'), { target: { value: 'lan-vllm' } })
    expect(add).toBeDisabled() // a custom provider has no host to fall back to
    fireEvent.change(screen.getByPlaceholderText(/llm\.internal\.example/), {
      target: { value: ' http://10.0.0.5:8000/v1 ' },
    })
    fireEvent.click(screen.getByRole('switch', { name: 'No API key' }))
    fireEvent.click(add)

    await waitFor(() =>
      expect(createProvider).toHaveBeenCalledWith('lan-vllm', {
        network_config: { base_url: 'http://10.0.0.5:8000/v1' },
        custom_provider_config: { base_provider_type: 'openai', is_key_less: true },
      }),
    )
    // the host rides on the create, not on a second write
    expect(setProviderBaseUrl).not.toHaveBeenCalledWith('lan-vllm', expect.anything())
  })

  it('refuses a name that is not lowercase letters, digits and dashes', async () => {
    listProviders.mockResolvedValue({ data: { providers: [] } })
    listKeys.mockResolvedValue({ data: { keys: [], total: 0 } })
    routability.mockResolvedValue({ data: { providers: {}, keys: {} } })
    render(<AiProvidersPanel notify={() => {}} addProviderRequested onAddProviderOpened={() => {}} />)
    await screen.findByRole('dialog', { name: 'Add provider' })
    fireEvent.click(screen.getByRole('button', { name: 'Custom (OpenAI-compatible)' }))
    fireEvent.change(screen.getByPlaceholderText('lan-vllm'), { target: { value: 'My Server' } })
    fireEvent.change(screen.getByPlaceholderText(/llm\.internal\.example/), { target: { value: 'http://10.0.0.5:8000' } })
    expect(screen.getByRole('button', { name: 'Add' })).toBeDisabled()
  })

  it('keeps two coexisting providers apart: each edits its own URL and deletes alone', async () => {
    const providers = [
      { name: 'openai' },
      custom('lan-vllm', 'http://10.0.0.5:8000'),
      custom('lab-gw', 'http://10.0.0.6:9000', true),
    ]
    listProviders.mockResolvedValue({ data: { providers } })
    listKeys.mockImplementation((name: string) =>
      Promise.resolve({
        data: {
          keys: name === 'lan-vllm' ? [{ id: 'k9', name: 'vllm-key', value: 'sk-****', models: ['*'], weight: 1 }] : [],
          total: name === 'lan-vllm' ? 1 : 0,
        },
      }),
    )
    routability.mockResolvedValue({
      data: {
        providers: { 'lan-vllm': true, 'lab-gw': true },
        keys: { k9: { provider: 'lan-vllm', routable: true, health: 'healthy' } },
      },
    })
    render(<AiProvidersPanel notify={() => {}} />)
    await screen.findByText('vllm-key')

    // the key-less one routes without a key, so there is none to add to it
    expect(screen.getByText(/No API key needed/)).toBeInTheDocument()
    expect(screen.getAllByTitle('Edit provider')).toHaveLength(3) // openai, lan-vllm, lab-gw

    fireEvent.click(screen.getAllByTitle('Edit provider')[2])
    const field = await screen.findByDisplayValue('http://10.0.0.6:9000')
    // a custom provider's host cannot be cleared
    fireEvent.change(field, { target: { value: '' } })
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
    fireEvent.change(field, { target: { value: 'http://10.0.0.7:9000' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(setProviderBaseUrl).toHaveBeenCalledWith('lab-gw', 'http://10.0.0.7:9000'))

    fireEvent.click(within(screen.getByText('lab-gw').closest('tr')!).getByTitle('Delete provider'))
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(removeProvider).toHaveBeenCalledWith('lab-gw'))
    expect(removeProvider).toHaveBeenCalledTimes(1)
  })
})
