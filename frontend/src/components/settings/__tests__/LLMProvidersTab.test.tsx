import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

vi.mock('../../../services/api', () => ({
  llmProviderApi: {
    list: vi.fn(),
    test: vi.fn(),
    remove: vi.fn(),
    setDefault: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    listModels: vi.fn(),
  },
}))

import LLMProvidersTab from '../LLMProvidersTab'
import { llmProviderApi } from '../../../services/api'

const providers = [
  {
    provider_id: 'anthropic-default',
    provider_type: 'anthropic',
    name: 'Anthropic (default)',
    base_url: null,
    has_api_key: true,
    default_model: 'claude-sonnet-4-5-20250929',
    is_active: true,
    is_default: true,
    config: {},
    last_test_at: null,
    last_test_success: true,
    last_error: null,
    created_at: null,
    updated_at: null,
  },
  {
    provider_id: 'ollama-local',
    provider_type: 'ollama',
    name: 'Local Ollama',
    base_url: 'http://localhost:11434',
    has_api_key: false,
    default_model: 'llama3.1:8b',
    is_active: true,
    is_default: false,
    config: {},
    last_test_at: null,
    last_test_success: null,
    last_error: null,
    created_at: null,
    updated_at: null,
  },
  {
    provider_id: 'openai-prod',
    provider_type: 'openai',
    name: 'OpenAI',
    base_url: 'https://api.openai.com/v1',
    has_api_key: true,
    default_model: 'gpt-4o-mini',
    is_active: true,
    is_default: false,
    config: {},
    last_test_at: null,
    last_test_success: false,
    last_error: 'bad key',
    created_at: null,
    updated_at: null,
  },
]

describe('LLMProvidersTab', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(llmProviderApi.list as any).mockResolvedValue({ data: providers })
    ;(llmProviderApi.test as any).mockResolvedValue({
      data: { success: true, provider_id: 'ollama-local', error: null },
    })
  })

  it('renders each provider with status chips and default badge', async () => {
    render(<LLMProvidersTab setMessage={() => {}} />)

    await waitFor(() => expect(llmProviderApi.list).toHaveBeenCalled())

    expect(await screen.findByText('Anthropic (default)')).toBeInTheDocument()
    expect(screen.getByText('Local Ollama')).toBeInTheDocument()
    expect(screen.getByText('OpenAI')).toBeInTheDocument()

    // Default chip (filled star) should appear for anthropic-default only
    const defaultButtons = screen.getAllByLabelText(/default/i)
    expect(defaultButtons.length).toBeGreaterThan(0)

    // One provider reports an error, another is active, another untested
    expect(screen.getByText('Active')).toBeInTheDocument()
    expect(screen.getByText('Untested')).toBeInTheDocument()
    expect(screen.getByText('Error')).toBeInTheDocument()
  })

  it('invokes the test API when the beaker button is clicked', async () => {
    render(<LLMProvidersTab setMessage={() => {}} />)
    await screen.findByText('Local Ollama')

    // The beaker/test icons are IconButtons with title "Test connection"
    const testButtons = await screen.findAllByLabelText(/test connection/i)
    fireEvent.click(testButtons[1]) // click the second (ollama) test button

    await waitFor(() =>
      expect(llmProviderApi.test).toHaveBeenCalledWith('ollama-local')
    )
  })
})
