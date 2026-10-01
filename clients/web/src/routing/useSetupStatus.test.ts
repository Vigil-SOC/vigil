import { beforeEach, describe, expect, it, vi } from 'vitest'
import { llmProviderApi, type LLMProvider } from '../services/api'
import { anyRoutableBifrostProvider } from '../services/bifrostApi'
import { isProviderReady, readProviderConfigured } from './useSetupStatus'

vi.mock('../services/api', () => ({
  llmProviderApi: { list: vi.fn() },
}))

vi.mock('../services/bifrostApi', () => ({
  anyRoutableBifrostProvider: vi.fn(),
}))

const provider = (over: Partial<LLMProvider> = {}): LLMProvider =>
  ({ is_active: true, is_default: true, ...over }) as LLMProvider

describe('readProviderConfigured', () => {
  beforeEach(() => {
    vi.mocked(llmProviderApi.list).mockReset()
    vi.mocked(anyRoutableBifrostProvider).mockReset()
  })

  it('rejects when a provider read fails and the other side is not ready', async () => {
    vi.mocked(llmProviderApi.list).mockRejectedValue(new Error('down'))
    vi.mocked(anyRoutableBifrostProvider).mockResolvedValue(false)
    await expect(readProviderConfigured()).rejects.toThrow('down')
  })

  it('stays ready when one read fails and the other side can already route', async () => {
    vi.mocked(llmProviderApi.list).mockRejectedValue(new Error('down'))
    vi.mocked(anyRoutableBifrostProvider).mockResolvedValue(true)
    await expect(readProviderConfigured()).resolves.toBe(true)

    vi.mocked(llmProviderApi.list).mockResolvedValue({ data: [provider()] } as never)
    vi.mocked(anyRoutableBifrostProvider).mockRejectedValue(new Error('gateway'))
    await expect(readProviderConfigured()).resolves.toBe(true)
  })

  it('is ready for a legacy default or a routable Bifrost provider', async () => {
    expect(isProviderReady(provider({ is_default: false }))).toBe(false)
    vi.mocked(llmProviderApi.list).mockResolvedValue({ data: [provider()] } as never)
    vi.mocked(anyRoutableBifrostProvider).mockResolvedValue(false)
    await expect(readProviderConfigured()).resolves.toBe(true)

    vi.mocked(llmProviderApi.list).mockResolvedValue({ data: [] } as never)
    vi.mocked(anyRoutableBifrostProvider).mockResolvedValue(true)
    await expect(readProviderConfigured()).resolves.toBe(true)

    vi.mocked(anyRoutableBifrostProvider).mockResolvedValue(false)
    await expect(readProviderConfigured()).resolves.toBe(false)
  })
})
