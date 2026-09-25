import { createElement } from 'react'
import { act, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { configApi, findingsApi } from '../services/api'
import { notificationService } from '../services/notifications'
import { useDesktopNotifications } from './useDesktopNotifications'

vi.mock('../services/api', () => ({
  findingsApi: { getAll: vi.fn() },
  configApi: { getGeneral: vi.fn() },
}))

vi.mock('../services/notifications', () => ({
  notificationService: {
    setEnabled: vi.fn(),
    notifyNewFinding: vi.fn(),
  },
}))

function Probe() {
  useDesktopNotifications()
  return null
}

function finding(id: string, createdAt: string) {
  return { finding_id: id, created_at: createdAt, severity: 'high', title: id }
}

const T0 = '2026-09-01T00:00:00.000001+00:00'
const T1 = '2026-09-01T00:00:01.000001+00:00'
const T2 = '2026-09-01T00:00:02.000002+00:00'

async function settle() {
  for (let i = 0; i < 10; i++) {
    await act(async () => {
      await Promise.resolve()
    })
  }
}

async function nextPoll() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(30_000)
  })
}

describe('useDesktopNotifications', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    vi.mocked(configApi.getGeneral).mockResolvedValue({
      data: { show_notifications: true },
    } as never)
    vi.mocked(findingsApi.getAll).mockResolvedValue({ data: { findings: [] } } as never)
    vi.mocked(notificationService.notifyNewFinding).mockReset()
    Object.defineProperty(window, 'Notification', {
      configurable: true,
      value: { permission: 'granted' },
    })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('does not alert when older findings slide into the window', async () => {
    vi.mocked(findingsApi.getAll).mockResolvedValueOnce({
      data: { findings: [finding('kept', T1)] },
    } as never)
    render(createElement(Probe))
    await settle()

    expect(findingsApi.getAll).toHaveBeenCalledWith({
      limit: 25,
      exclusions: 'hide',
      sort_by: 'created_at',
      sort_order: 'desc',
    })
    expect(notificationService.notifyNewFinding).not.toHaveBeenCalled()

    vi.mocked(findingsApi.getAll).mockResolvedValueOnce({
      data: { findings: [finding('kept', T1), finding('slid-in', T0)] },
    } as never)
    await nextPoll()
    expect(notificationService.notifyNewFinding).not.toHaveBeenCalled()
  })

  it('alerts once for one newer finding', async () => {
    vi.mocked(findingsApi.getAll).mockResolvedValueOnce({
      data: { findings: [finding('baseline', T0)] },
    } as never)
    render(createElement(Probe))
    await settle()

    vi.mocked(findingsApi.getAll).mockResolvedValueOnce({
      data: { findings: [finding('arrived', T1), finding('baseline', T0)] },
    } as never)
    await nextPoll()
    expect(notificationService.notifyNewFinding).toHaveBeenCalledTimes(1)
    expect(notificationService.notifyNewFinding).toHaveBeenCalledWith(
      expect.objectContaining({ finding_id: 'arrived' }),
    )

    vi.mocked(findingsApi.getAll).mockResolvedValueOnce({
      data: { findings: [finding('arrived', T1), finding('baseline', T0)] },
    } as never)
    await nextPoll()
    expect(notificationService.notifyNewFinding).toHaveBeenCalledTimes(1)
  })

  it('alerts once each for two findings that share a created_at', async () => {
    vi.mocked(findingsApi.getAll).mockResolvedValueOnce({
      data: { findings: [finding('baseline', T0)] },
    } as never)
    render(createElement(Probe))
    await settle()

    vi.mocked(findingsApi.getAll).mockResolvedValueOnce({
      data: { findings: [finding('tie-a', T1), finding('baseline', T0)] },
    } as never)
    await nextPoll()

    vi.mocked(findingsApi.getAll).mockResolvedValueOnce({
      data: { findings: [finding('tie-b', T1), finding('tie-a', T1), finding('baseline', T0)] },
    } as never)
    await nextPoll()

    vi.mocked(findingsApi.getAll).mockResolvedValueOnce({
      data: { findings: [finding('tie-b', T1), finding('tie-a', T1)] },
    } as never)
    await nextPoll()

    const ids = vi.mocked(notificationService.notifyNewFinding).mock.calls.map(
      (call) => call[0].finding_id,
    )
    expect(ids).toEqual(['tie-a', 'tie-b'])
  })

  it('caps alerts at five per poll', async () => {
    vi.mocked(findingsApi.getAll).mockResolvedValueOnce({
      data: { findings: [finding('baseline', T0)] },
    } as never)
    render(createElement(Probe))
    await settle()

    const batch = ['n6', 'n5', 'n4', 'n3', 'n2', 'n1'].map((id, index) =>
      finding(id, index === 0 ? T2 : T1),
    )
    vi.mocked(findingsApi.getAll).mockResolvedValueOnce({
      data: { findings: [...batch, finding('baseline', T0)] },
    } as never)
    await nextPoll()

    const ids = vi.mocked(notificationService.notifyNewFinding).mock.calls.map(
      (call) => call[0].finding_id,
    )
    expect(ids).toEqual(['n6', 'n5', 'n4', 'n3', 'n2'])

    vi.mocked(findingsApi.getAll).mockResolvedValueOnce({
      data: { findings: [...batch, finding('baseline', T0)] },
    } as never)
    await nextPoll()
    expect(notificationService.notifyNewFinding).toHaveBeenCalledTimes(5)
  })
})
