import { describe, expect, it, vi } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { configApi } from '../../services/api'
import { AUTONOMY_CHANGED, useForceManualApproval } from './useSettings'

vi.mock('../../services/api', () => ({
  default: {},
  configApi: {
    getOrchestrator: vi.fn(),
    setOrchestrator: vi.fn(),
    setForceManualApproval: vi.fn(),
    getForceManualApproval: vi.fn(),
  },
}))

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
