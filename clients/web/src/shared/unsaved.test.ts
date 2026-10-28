import { afterEach, describe, expect, it, vi } from 'vitest'
import { hasUnsaved, leaveUnsaved } from './unsaved'

describe('unsaved edits', () => {
  afterEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  it('lets a person leave freely when no editor holds edits', () => {
    const confirm = vi.spyOn(window, 'confirm')
    expect(hasUnsaved()).toBe(false)
    expect(leaveUnsaved()).toBe(true)
    expect(confirm).not.toHaveBeenCalled()
  })

  it('asks first while an editor is marked unsaved, and stays on a No', () => {
    document.body.innerHTML = '<div data-unsaved=""></div>'
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    expect(leaveUnsaved()).toBe(false)
    confirm.mockReturnValue(true)
    expect(leaveUnsaved()).toBe(true)
    expect(confirm).toHaveBeenCalledTimes(2)
  })
})
