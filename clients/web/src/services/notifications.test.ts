import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const constructed: { title: string; options: NotificationOptions }[] = []
const requestPermission = vi.fn()

function stubNotification(permission: NotificationPermission) {
  class FakeNotification {
    static permission = permission
    static requestPermission = requestPermission
    constructor(title: string, options: NotificationOptions) {
      constructed.push({ title, options })
    }
    close() {}
  }
  vi.stubGlobal('Notification', FakeNotification)
}

async function loadService() {
  vi.resetModules()
  return (await import('./notifications')).notificationService
}

const complete = { title: 'Vigil', summary: 'done' }

describe('notificationService', () => {
  beforeEach(() => {
    const meta = document.createElement('meta')
    meta.name = 'vigil-base-path'
    meta.content = '/vigil'
    document.head.appendChild(meta)
  })

  afterEach(() => {
    document.head.querySelector('meta[name="vigil-base-path"]')?.remove()
    vi.unstubAllGlobals()
    constructed.length = 0
    requestPermission.mockReset()
  })

  it('shows nothing until enabled, even with permission granted', async () => {
    stubNotification('granted')
    const service = await loadService()
    service.notifyInvestigationComplete(complete)
    expect(constructed).toEqual([])
  })

  it('never asks for permission when showing', async () => {
    stubNotification('default')
    const service = await loadService()
    service.notifyInvestigationComplete(complete)
    service.setEnabled(true)
    service.notifyInvestigationComplete(complete)
    service.notifyNewFinding({ finding_id: 'f1', severity: 'high' })
    expect(requestPermission).not.toHaveBeenCalled()
    expect(constructed).toEqual([])
  })

  it('shows under the context path once enabled and granted', async () => {
    stubNotification('granted')
    const service = await loadService()
    service.setEnabled(true)
    service.notifyNewFinding({ finding_id: 'f1', severity: 'high', title: 'Beacon' })
    expect(constructed).toEqual([
      {
        title: 'New Security Finding',
        options: {
          body: 'HIGH: Beacon',
          tag: 'finding-f1',
          requireInteraction: true,
          data: { type: 'finding', id: 'f1' },
          icon: '/vigil/favicon.ico',
        },
      },
    ])
  })

  it('requestPermission asks the browser when permission is undecided', async () => {
    stubNotification('default')
    requestPermission.mockResolvedValue('granted')
    const service = await loadService()
    await expect(service.requestPermission()).resolves.toBe(true)
    expect(requestPermission).toHaveBeenCalledTimes(1)
  })
})
