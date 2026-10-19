import { describe, it, expect, vi } from 'vitest'

vi.mock('axios', () => {
  const instance = {
    put: vi.fn(() => Promise.resolve({ data: {} })),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  }
  return { default: { create: () => instance } }
})

import axios from 'axios'
import { MCP_START_TIMEOUT, mcpApi } from './api'

describe('mcpApi.setServerEnabled', () => {
  it('waits long enough for a first start that downloads the server', async () => {
    await mcpApi.setServerEnabled('loglm', true)
    const put = (axios as unknown as { create: () => { put: ReturnType<typeof vi.fn> } }).create().put
    expect(put).toHaveBeenCalledWith('/mcp/servers/loglm/enabled', { enabled: true }, { timeout: MCP_START_TIMEOUT })
    expect(MCP_START_TIMEOUT).toBeGreaterThanOrEqual(120_000)
  })
})
