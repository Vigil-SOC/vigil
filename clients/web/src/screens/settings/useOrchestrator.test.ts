import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { configApi, orchestratorApi } from '../../services/api'
import { AUTONOMY_CHANGED, useForceManualApproval, useOrchestrator } from './useSettings'

vi.mock('../../services/api', () => ({
  default: {},
  configApi: {
    getOrchestrator: vi.fn(),
    setOrchestrator: vi.fn(),
    setForceManualApproval: vi.fn(),
    getForceManualApproval: vi.fn(),
  },
  orchestratorApi: { getStatus: vi.fn() },
}))

describe('useOrchestrator', () => {
  beforeEach(() => {
    vi.mocked(configApi.getOrchestrator).mockResolvedValue({ data: { enabled: false } } as never)
    vi.mocked(configApi.setOrchestrator).mockResolvedValue({ data: {} } as never)
    vi.mocked(orchestratorApi.getStatus).mockReset()
    vi.mocked(orchestratorApi.getStatus).mockResolvedValueOnce({ data: { enabled: false } } as never)
  })

  it('re-reads the status after a save, without reloading the section', async () => {
    const { result } = renderHook(() => useOrchestrator())
    await waitFor(() => expect(result.current.phase).toBe('ready'))
    expect(result.current.status?.enabled).toBe(false)

    vi.mocked(orchestratorApi.getStatus).mockResolvedValueOnce({ data: { enabled: true } } as never)
    await act(() => result.current.save({ ...result.current.config!, enabled: true }))
    await waitFor(() => expect(result.current.status?.enabled).toBe(true))
    expect(result.current.phase).toBe('ready')
  })

  it('keeps the last status when the re-read fails, and the save still succeeds', async () => {
    const { result } = renderHook(() => useOrchestrator())
    await waitFor(() => expect(result.current.phase).toBe('ready'))

    vi.mocked(orchestratorApi.getStatus).mockRejectedValueOnce(new Error('down'))
    await expect(act(() => result.current.save(result.current.config!))).resolves.not.toThrow()
    expect(result.current.status?.enabled).toBe(false)
  })
})

describe('useForceManualApproval', () => {
  it('announces a saved change so the header badge re-reads', async () => {
    vi.mocked(configApi.getForceManualApproval).mockResolvedValue({ data: { enabled: false } } as never)
    vi.mocked(configApi.setForceManualApproval).mockResolvedValue({ data: { enabled: true } } as never)
    const heard = vi.fn()
    window.addEventListener(AUTONOMY_CHANGED, heard)
    const { result } = renderHook(() => useForceManualApproval())
    await waitFor(() => expect(result.current.phase).toBe('ready'))

    await act(() => result.current.save(true))
    expect(heard).toHaveBeenCalledTimes(1)
    window.removeEventListener(AUTONOMY_CHANGED, heard)
  })
})
