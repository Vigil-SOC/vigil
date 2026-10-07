import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import SetupProviderStep from './SetupProviderStep'
import { aiConfigApi } from '../../services/api'
import { bifrostApi, keyRefusal } from '../../services/bifrostApi'

vi.mock('../../services/bifrostApi', () => ({
  bifrostApi: {
    listProviders: vi.fn(),
    listKeys: vi.fn(),
    routability: vi.fn(),
    createProvider: vi.fn(() => Promise.resolve({ data: { name: 'anthropic' } })),
    createKey: vi.fn(() => Promise.resolve({ data: { id: 'new-key' } })),
    updateKey: vi.fn(() => Promise.resolve({ data: { id: 'k1' } })),
    removeKey: vi.fn(),
    removeProvider: vi.fn(),
    providerModels: vi.fn(),
    modelParameters: vi.fn(),
  },
  secretText: (v: unknown) =>
    typeof v === 'string' ? v : ((v as { value?: string } | undefined)?.value ?? ''),
  isMasked: () => false,
  keyRefusal: vi.fn(() => Promise.resolve(null)),
  secretEnvRef: () => '',
  COMMON_PROVIDERS: ['anthropic', 'openai', 'ollama', 'gemini'],
}))

vi.mock('../../services/api', () => ({
  llmProviderApi: { list: vi.fn() },
  aiConfigApi: { getConfig: vi.fn() },
}))

const models = (...names: string[]) => ({ data: { models: names.map((name) => ({ name })), total: names.length } })

/** Bifrost holds `held`: provider -> its keys. */
function gateway(held: Record<string, unknown[]>, routable: Record<string, boolean> | null = {}) {
  vi.mocked(bifrostApi.listProviders).mockResolvedValue({ data: { providers: Object.keys(held).map((name) => ({ name })) } } as never)
  vi.mocked(bifrostApi.listKeys).mockImplementation(((name: string) =>
    Promise.resolve({ data: { keys: held[name] || [], total: 0 } })) as never)
  if (routable) vi.mocked(bifrostApi.routability).mockResolvedValue({ data: { providers: routable, keys: {} } } as never)
  else vi.mocked(bifrostApi.routability).mockRejectedValue(new Error('down'))
}

const key = (over = {}) => ({ id: 'k1', name: 'anthropic-key', models: ['*'], weight: 1, enabled: true, ...over })
const paste = (label: string, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } })

describe('SetupProviderStep', () => {
  beforeEach(async () => {
    vi.clearAllMocks()
    const { llmProviderApi } = await import('../../services/api')
    vi.mocked(llmProviderApi.list).mockResolvedValue({ data: [] } as never)
    vi.mocked(aiConfigApi.getConfig).mockResolvedValue({ data: { components: [], assignments: {} } } as never)
    vi.mocked(keyRefusal).mockResolvedValue(null)
    vi.mocked(bifrostApi.providerModels).mockResolvedValue(models() as never)
  })

  it('states where data goes on each card, and selects nothing when Bifrost holds nothing', async () => {
    gateway({})
    render(<SetupProviderStep onRoutable={() => {}} />)
    expect(await screen.findByRole('button', { name: /Anthropic/ })).toBeInTheDocument()
    expect(screen.getAllByText('Hosted: data leaves your site')).toHaveLength(2)
    expect(screen.getByText('Data stays in your cloud')).toBeInTheDocument()
    expect(screen.getByText('Local: data stays on site')).toBeInTheDocument()
    expect(screen.queryByText('Recommended')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('API key')).not.toBeInTheDocument()
    expect(screen.getByText(/Pick where AI runs/)).toBeInTheDocument()
  })

  it('pre-selects the first held provider and shows its routability, none when it could not be asked', async () => {
    gateway({ openai: [key({ name: 'openai-key' })], ollama: [] }, { openai: false, ollama: true })
    const { unmount } = render(<SetupProviderStep onRoutable={() => {}} />)
    expect(await screen.findByLabelText('API key')).toHaveAttribute('placeholder', '•••••••• (unchanged)')
    expect(screen.getByRole('button', { name: /OpenAI/ })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByText('Poor')).toBeInTheDocument()
    expect(screen.getByText('Good')).toBeInTheDocument()
    unmount()

    gateway({ openai: [key()] }, null)
    render(<SetupProviderStep onRoutable={() => {}} />)
    await screen.findByLabelText('API key')
    expect(screen.queryByText('Poor')).not.toBeInTheDocument()
    expect(screen.getByText(/Couldn’t check whether the gateway’s keys can route/)).toBeInTheDocument()
  })

  it('creates the provider and key, shows models from providerModels, never echoes the key', async () => {
    gateway({})
    vi.mocked(bifrostApi.providerModels).mockResolvedValue(models('Opus', 'Sonnet', 'Haiku', 'Mini', 'Nano') as never)
    render(<SetupProviderStep onRoutable={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /Anthropic/ }))
    paste('API key', 'sk-secret')
    fireEvent.click(screen.getByRole('button', { name: 'Test key' }))
    expect(await screen.findByText('Key works. Opus, Sonnet, Haiku +2 are available.')).toBeInTheDocument()
    expect(bifrostApi.createProvider).toHaveBeenCalledWith('anthropic')
    expect(bifrostApi.createKey).toHaveBeenCalledWith('anthropic', expect.objectContaining({ value: 'sk-secret' }))
    expect(screen.getByLabelText('API key')).toHaveValue('')
    expect(screen.queryByText(/sk-secret/)).not.toBeInTheDocument()
  })

  it('shows Bifrost’s refusal in the poor banner', async () => {
    gateway({})
    vi.mocked(keyRefusal).mockResolvedValue('invalid x-api-key')
    render(<SetupProviderStep onRoutable={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /Anthropic/ }))
    paste('API key', 'bad')
    fireEvent.click(screen.getByRole('button', { name: 'Test key' }))
    const banner = await screen.findByRole('alert')
    expect(banner).toHaveTextContent('Key stored, but it cannot route: invalid x-api-key')
    expect(banner).toHaveClass('poor')
  })

  it('retests a held key without creating a second one or rewriting the secret', async () => {
    gateway({ anthropic: [key()] })
    render(<SetupProviderStep onRoutable={() => {}} />)
    await screen.findByLabelText('API key')
    fireEvent.click(screen.getByRole('button', { name: 'Test key' }))
    expect(await screen.findByText('Key works.')).toBeInTheDocument()
    expect(keyRefusal).toHaveBeenCalledWith('k1')
    expect(bifrostApi.createKey).not.toHaveBeenCalled()
    expect(bifrostApi.updateKey).not.toHaveBeenCalled()

    paste('API key', 'sk-new')
    fireEvent.click(screen.getByRole('button', { name: 'Test again' }))
    await waitFor(() => expect(bifrostApi.updateKey).toHaveBeenCalledWith('anthropic', 'k1', expect.objectContaining({ value: 'sk-new' })))
    expect(bifrostApi.createKey).not.toHaveBeenCalled()
  })

  it('writes the Ollama URL as ollama_key_config.url and words the pass for Ollama', async () => {
    gateway({})
    vi.mocked(bifrostApi.providerModels).mockResolvedValue(models('llama3.1:70b', 'llama3.1:8b') as never)
    render(<SetupProviderStep onRoutable={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /Local \(Ollama\)/ }))
    paste('Ollama server URL', 'http://host.docker.internal:11434')
    fireEvent.click(screen.getByRole('button', { name: 'Test server' }))
    expect(await screen.findByText('Ollama is reachable. llama3.1:70b and llama3.1:8b are installed.')).toBeInTheDocument()
    expect(bifrostApi.createKey).toHaveBeenCalledWith(
      'ollama',
      expect.objectContaining({ ollama_key_config: { url: 'http://host.docker.internal:11434' }, value: '' }),
    )
  })

  it('opens the key dialog for a cloud account, defaulting to Bedrock', async () => {
    gateway({})
    render(<SetupProviderStep onRoutable={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: /Your cloud account/ }))
    expect(screen.getByRole('button', { name: 'AWS Bedrock' })).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(screen.getByRole('button', { name: 'Google Vertex' }))
    fireEvent.click(screen.getByRole('button', { name: /Add key/ }))
    expect(await screen.findByText('Add key · vertex')).toBeInTheDocument()
    expect(bifrostApi.createProvider).toHaveBeenCalledWith('vertex')
  })

  it('keeps Retry on a Bifrost read error and hides the cards', async () => {
    vi.mocked(bifrostApi.listProviders).mockRejectedValue(new Error('gateway down'))
    render(<SetupProviderStep onRoutable={() => {}} />)
    expect(await screen.findByText('gateway down')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Retry/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Anthropic/ })).not.toBeInTheDocument()
  })

  it('tells the parent once any provider can route', async () => {
    gateway({ ollama: [] }, { ollama: true })
    const onRoutable = vi.fn()
    render(<SetupProviderStep onRoutable={onRoutable} />)
    await waitFor(() => expect(onRoutable).toHaveBeenCalled())
  })

  it('lists legacy providers quietly with the new residency words', async () => {
    gateway({})
    const { llmProviderApi } = await import('../../services/api')
    vi.mocked(llmProviderApi.list).mockResolvedValue({
      data: [{ provider_id: 'l1', provider_type: 'anthropic', name: 'Claude', base_url: 'https://api.anthropic.com' }],
    } as never)
    render(<SetupProviderStep onRoutable={() => {}} />)
    expect(await screen.findByText('Claude')).toBeInTheDocument()
    expect(screen.getByText(/\(Hosted: data leaves your site\)/)).toBeInTheDocument()
  })

  describe('What Vigil will use', () => {
    it('renders a row per component with its model, "Provider default" and the description', async () => {
      gateway({})
      vi.mocked(aiConfigApi.getConfig).mockResolvedValue({
        data: {
          components: ['chat_default', 'triage', 'mystery'],
          assignments: { triage: { component: 'triage', provider_id: 'anthropic', model_id: 'claude-haiku-4-5' } },
        },
      } as never)
      render(<SetupProviderStep onRoutable={() => {}} />)
      expect(await screen.findByText('Triage Agent')).toBeInTheDocument()
      expect(screen.getByText('claude-haiku-4-5')).toBeInTheDocument()
      expect(screen.getAllByText('Provider default')).toHaveLength(2)
      expect(screen.getByText('mystery')).toBeInTheDocument()
      expect(screen.getByText('Change any of them in Settings › AI models.')).toBeInTheDocument()
    })

    it('says so when nothing is assigned yet', async () => {
      gateway({})
      render(<SetupProviderStep onRoutable={() => {}} />)
      expect(await screen.findByText('No model assigned yet')).toBeInTheDocument()
    })

    it('shows its own error and Retry without hiding the provider cards', async () => {
      gateway({})
      vi.mocked(aiConfigApi.getConfig).mockRejectedValueOnce(new Error('config down'))
      render(<SetupProviderStep onRoutable={() => {}} />)
      expect(await screen.findByText('config down')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: /Anthropic/ })).toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: /Retry/ }))
      expect(await screen.findByText('No model assigned yet')).toBeInTheDocument()
    })
  })
})
