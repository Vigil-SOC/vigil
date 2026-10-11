import { describe, expect, it } from 'vitest'
import { foldStatus, type StatusReads } from './statusLine'

const reads = (health: StatusReads['health']): StatusReads => ({
  health,
  federation: null,
  mcp: null,
  routability: null,
})

describe('foldStatus storage', () => {
  it('goes Poor and names the database when it is down', () => {
    expect(
      foldStatus(
        reads({ status: 'degraded', storage: { database_available: false, demo_mode: false } }),
      ),
    ).toEqual({ level: 'poor', sentence: 'Database is unavailable.' })
  })

  it('does not read a missing database as a fault in demo mode', () => {
    expect(
      foldStatus(
        reads({ status: 'healthy', storage: { database_available: false, demo_mode: true } }),
      ),
    ).toEqual({ level: 'good', sentence: 'No problems reported.' })
  })

  it('still reports a degraded status that has no database cause', () => {
    expect(
      foldStatus(reads({ status: 'degraded', storage: { database_available: true } })),
    ).toEqual({ level: 'poor', sentence: 'Health is degraded.' })
  })
})
