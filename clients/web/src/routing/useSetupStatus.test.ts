import { beforeEach, describe, expect, it, vi } from 'vitest'
import { llmProviderApi, type LLMProvider } from '../services/api'
import { anyRoutableBifrostProvider } from '../services/bifrostApi'
import { isProviderReady, readProviderStatus } from './useSetupStatus'

vi.mock('../services/api', () => ({
  llmProviderApi: { list: vi.fn() },
}))

vi.mock('../services/bifrostApi', () => ({
  anyRoutableBifrostProvider: vi.fn(),
}))

const provider = (over: Partial<LLMProvider> = {}): LLMProvider =>
  ({ is_active: true, is_default: true, ...over }) as LLMProvider

describe('readProviderStatus', () => {
  beforeEach(() => {
    vi.mocked(llmProviderApi.list).mockReset()
    vi.mocked(anyRoutableBifrostProvider).mockReset()
  })

  it('is unreachable only when the routability read fails and no legacy provider is ready', async () => {
    vi.mocked(llmProviderApi.list).mockResolvedValue({ data: [] } as never)
    vi.mocked(anyRoutableBifrostProvider).mockRejectedValue(new Error('Bifrost unreachable'))
    await expect(readProviderStatus()).resolves.toBe('unreachable')
  })

  it('is none, not unreachable, when the gateway answered and the legacy list failed', async () => {
    vi.mocked(llmProviderApi.list).mockRejectedValue(new Error('down'))
    vi.mocked(anyRoutableBifrostProvider).mockResolvedValue(false)
    await expect(readProviderStatus()).resolves.toBe('none')
  })

  it('stays ready when one read fails and the other side can already route', async () => {
    vi.mocked(llmProviderApi.list).mockRejectedValue(new Error('down'))
    vi.mocked(anyRoutableBifrostProvider).mockResolvedValue(true)
    await expect(readProviderStatus()).resolves.toBe('ready')

    vi.mocked(llmProviderApi.list).mockResolvedValue({ data: [provider()] } as never)
    vi.mocked(anyRoutableBifrostProvider).mockRejectedValue(new Error('gateway'))
    await expect(readProviderStatus()).resolves.toBe('ready')
  })

  it('is ready for a legacy default or a routable Bifrost provider', async () => {
    expect(isProviderReady(provider({ is_default: false }))).toBe(false)
    vi.mocked(llmProviderApi.list).mockResolvedValue({ data: [provider()] } as never)
    vi.mocked(anyRoutableBifrostProvider).mockResolvedValue(false)
    await expect(readProviderStatus()).resolves.toBe('ready')

    vi.mocked(llmProviderApi.list).mockResolvedValue({ data: [] } as never)
    vi.mocked(anyRoutableBifrostProvider).mockResolvedValue(true)
    await expect(readProviderStatus()).resolves.toBe('ready')

    vi.mocked(anyRoutableBifrostProvider).mockResolvedValue(false)
    await expect(readProviderStatus()).resolves.toBe('none')
  })
})
