import { describe, it, expect, vi, beforeEach } from 'vitest'

// Mock the shared axios instance used by api.ts
vi.mock('axios', () => {
  const get = vi.fn()
  const instance = {
    get,
    post: vi.fn(),
    interceptors: {
      request: { use: vi.fn() },
      response: { use: vi.fn() },
    },
  }
  return {
    default: {
      create: () => instance,
      post: vi.fn(),
      get,
    },
  }
})

import axios from 'axios'
import { reasoningApi } from './api'

describe('reasoningApi (GH #79)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    // Re-wire the shared instance's .get for each test
    const inst = (axios as any).create()
    inst.get.mockImplementation((url: string, config?: any) =>
      Promise.resolve({ data: { __url: url, __params: config?.params } }),
    )
  })

  it('getSessionSummary hits /reasoning/:sid and unwraps data', async () => {
    const res = await reasoningApi.getSessionSummary('abc-123')
    expect(res.__url).toBe('/reasoning/abc-123')
  })

  it('listInteractions forwards limit + offset', async () => {
    const res = await reasoningApi.listInteractions('sess-1', { limit: 50, offset: 10 })
    expect(res.__url).toBe('/reasoning/sess-1/interactions')
    expect(res.__params).toEqual({ limit: 50, offset: 10 })
  })

  it('getInteraction encodes ids', async () => {
    const res = await reasoningApi.getInteraction('sess with space', 'int/slash')
    expect(res.__url).toBe('/reasoning/sess%20with%20space/interactions/int%2Fslash')
  })

  it('listInvestigationInteractions hits investigation path', async () => {
    const res = await reasoningApi.listInvestigationInteractions('inv-9')
    expect(res.__url).toBe('/reasoning/investigation/inv-9/interactions')
  })
})
