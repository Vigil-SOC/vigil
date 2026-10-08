import { describe, expect, it } from 'vitest'
import { utcClock, utcDay, utcDayClock } from './utc'

describe('UTC time', () => {
  it('reads the stamp in UTC, 24-hour, whatever zone the machine is in', () => {
    expect(utcClock('2026-10-08T13:07:00Z')).toBe('13:07')
    expect(utcClock('2026-10-08T13:07:00-04:00')).toBe('17:07')
    expect(utcDay('2026-10-08T23:59:00Z')).toBe('Oct 8, 2026')
    expect(utcDayClock('2026-10-08T00:05:00Z')).toBe('Oct 8, 2026 · 00:05')
  })

  it('says nothing for a stamp that is missing or does not parse', () => {
    for (const bad of [undefined, null, '', 'soon']) {
      expect(utcClock(bad)).toBeNull()
      expect(utcDayClock(bad)).toBeNull()
    }
  })
})
