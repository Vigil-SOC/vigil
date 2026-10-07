import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import SetupProviderStep from './SetupProviderStep'

vi.mock('../../services/bifrostApi', () => ({
  bifrostApi: {
    listProviders: vi.fn(() =>
      Promise.resolve({
        data: { providers: [{ name: 'ollama' }, { name: 'openai' }] },
      }),
    ),
    listKeys: vi.fn((name: string) =>
      Promise.resolve({
        data: {
          keys:
            name === 'openai'
              ? [{ id: 'k1', name: 'openai', models: ['*'], weight: 1, ollama_key_config: { url: 'http://127.0.0.1:11434' } }]
              : [],
          total: name === 'openai' ? 1 : 0,
        },
      }),
    ),
    routability: vi.fn(() => Promise.resolve({ data: { providers: { ollama: true, openai: false }, keys: {} } })),
    createProvider: vi.fn(),
    createKey: vi.fn(),
    updateKey: vi.fn(),
    removeKey: vi.fn(),
    removeProvider: vi.fn(),
    providerModels: vi.fn(),
    modelParameters: vi.fn(),
  },
  secretText: (v: unknown) =>
    typeof v === 'string' ? v : ((v as { value?: string } | undefined)?.value ?? ''),
  isMasked: () => false,
  keyRefusal: () => Promise.resolve(null),
  secretEnvRef: () => '',
  COMMON_PROVIDERS: ['anthropic', 'openai', 'ollama'],
}))

vi.mock('../../services/api', () => ({
  llmProviderApi: {
    list: vi.fn(() =>
      Promise.resolve({
        data: [
          {
            provider_id: 'legacy-1',
            provider_type: 'anthropic',
            name: 'Claude',
            base_url: 'https://api.anthropic.com',
            is_active: true,
            is_default: true,
          },
        ],
      }),
    ),
  },
}))

describe('SetupProviderStep', () => {
  it('shows on-site copy for Ollama and loopback beside a hosted provider', async () => {
    render(<SetupProviderStep onSaved={() => {}} />)
    expect(await screen.findByText('ollama')).toBeInTheDocument()
    expect(screen.getByText('openai')).toBeInTheDocument()
    expect(screen.getByText('Claude')).toBeInTheDocument()
    expect(screen.getAllByText('data stays on site')).toHaveLength(2)
    expect(screen.getByText('data leaves the site')).toBeInTheDocument()
  })
})
