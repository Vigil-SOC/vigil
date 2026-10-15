import { renderHook, act, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { useOrchestrator, type OrchestratorConfig } from './useSettings'

const { getOrchestrator, setOrchestrator, getStatus } = vi.hoisted(() => ({
  getOrchestrator: vi.fn(),
  setOrchestrator: vi.fn(),
  getStatus: vi.fn(),
}))

vi.mock('../../services/api', () => ({
  configApi: { getOrchestrator, setOrchestrator },
  orchestratorApi: { getStatus },
}))

describe('useOrchestrator', () => {
  it('refreshes status after a save without reloading or resetting config', async () => {
    const cfg = { enabled: false, max_concurrent_agents: 3 } as OrchestratorConfig
    getOrchestrator.mockResolvedValue({ data: cfg })
    getStatus.mockResolvedValueOnce({ data: { enabled: false } })
    setOrchestrator.mockResolvedValue({ data: {} })
    const { result } = renderHook(() => useOrchestrator())
    await waitFor(() => expect(result.current.phase).toBe('ready'))
    expect(result.current.status?.enabled).toBe(false)

    getStatus.mockResolvedValueOnce({ data: { enabled: true } })
    const edited = { ...cfg, enabled: true, max_concurrent_agents: 9 }
    act(() => result.current.setConfig(edited))
    await act(async () => {
      await result.current.save(edited)
    })
    await waitFor(() => expect(result.current.status?.enabled).toBe(true))
    expect(result.current.phase).toBe('ready')
    expect(result.current.config).toEqual(edited)
    expect(getOrchestrator).toHaveBeenCalledTimes(1)
  })
})
