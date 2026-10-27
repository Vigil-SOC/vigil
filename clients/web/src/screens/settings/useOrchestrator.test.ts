import { describe, expect, it, vi } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { autonomyApi, orchestratorApi } from '../../services/api'
import { AUTONOMY_CHANGED, useCeiling } from './useSettings'

vi.mock('../../services/api', () => ({
  default: {},
  configApi: {},
  autonomyApi: { get: vi.fn(), update: vi.fn(), resume: vi.fn() },
  orchestratorApi: { kill: vi.fn() },
}))

const view = (over: Record<string, boolean> = {}) => ({
  data: {
    act_reversible: false,
    act_irreversible: false,
    stopped: false,
    read: true,
    write: true,
    propose: true,
    environment_locked: false,
    four_eyes: false,
    ...over,
  },
})

describe('useCeiling', () => {
  it('announces a save, a stop and a resume so the header badge re-reads', async () => {
    vi.mocked(autonomyApi.get).mockResolvedValue(view() as never)
    vi.mocked(autonomyApi.update).mockResolvedValue(view({ act_reversible: true }) as never)
    vi.mocked(orchestratorApi.kill).mockResolvedValue({ data: {} } as never)
    vi.mocked(autonomyApi.resume).mockResolvedValue(view({ act_reversible: true }) as never)
    const heard = vi.fn()
    window.addEventListener(AUTONOMY_CHANGED, heard)
    const { result } = renderHook(() => useCeiling())
    await waitFor(() => expect(result.current.phase).toBe('ready'))

    await act(() => result.current.save({ act_reversible: true }))
    expect(result.current.ceiling?.act_reversible).toBe(true)

    // stop is the orchestrator kill, then a re-read of the ceiling it set
    vi.mocked(autonomyApi.get).mockResolvedValue(view({ act_reversible: true, stopped: true, read: false }) as never)
    await act(() => result.current.stop())
    expect(orchestratorApi.kill).toHaveBeenCalledTimes(1)
    expect(result.current.ceiling?.stopped).toBe(true)

    await act(() => result.current.resume())
    expect(result.current.ceiling?.stopped).toBe(false)
    expect(heard).toHaveBeenCalledTimes(3)
    window.removeEventListener(AUTONOMY_CHANGED, heard)
  })

  it('re-reads the ceiling when a stop fails after storing it', async () => {
    vi.mocked(autonomyApi.get).mockResolvedValue(view({ act_reversible: true }) as never)
    const { result } = renderHook(() => useCeiling())
    await waitFor(() => expect(result.current.phase).toBe('ready'))

    vi.mocked(orchestratorApi.kill).mockRejectedValue(new Error('kill failed'))
    vi.mocked(autonomyApi.get).mockResolvedValue(view({ act_reversible: true, stopped: true }) as never)
    await act(() => expect(result.current.stop()).rejects.toThrow('kill failed'))
    expect(result.current.ceiling?.stopped).toBe(true)
  })

  it('reports an error, not a default ceiling, when the read fails', async () => {
    vi.mocked(autonomyApi.get).mockRejectedValue(new Error('down'))
    const { result } = renderHook(() => useCeiling())
    await waitFor(() => expect(result.current.phase).toBe('error'))
    expect(result.current.ceiling).toBeNull()
  })
})
