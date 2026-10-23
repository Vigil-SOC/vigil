import { afterEach, describe, expect, it, vi } from 'vitest'
import { authBypassed, foldStatus, type StatusReads } from './statusLine'

const reads = (health: StatusReads['health']): StatusReads => ({
  health,
  federation: null,
  mcp: null,
  routability: null,
  processes: null,
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

describe('foldStatus processes', () => {
  const healthy = (processes: StatusReads['processes']): StatusReads => ({
    ...reads({ status: 'healthy', storage: { database_available: true } }),
    processes,
  })

  it.each([
    ['no_token', 'Agent layer has no internal token.'],
    ['not_running', 'Agent layer is not running.'],
    ['unhealthy', 'Agent layer is not ready.'],
  ])('goes Fair when the agent layer is %s', (agent_layer, sentence) => {
    expect(foldStatus(healthy({ agent_layer, daemon: 'running' }))).toEqual({ level: 'fair', sentence })
  })

  it('does not read a daemon that was never started as a fault', () => {
    expect(foldStatus(healthy({ agent_layer: 'running', daemon: 'not_running' }))).toEqual({
      level: 'good',
      sentence: 'No problems reported.',
    })
  })

  it('goes Fair when the daemon reports a stopped component', () => {
    expect(foldStatus(healthy({ agent_layer: 'running', daemon: 'unhealthy' }))).toEqual({
      level: 'fair',
      sentence: 'SOC daemon reports a stopped component.',
    })
  })

  it('says nothing when the read failed or the state is unknown', () => {
    for (const processes of [null, { agent_layer: 'unknown', daemon: 'unknown' }]) {
      expect(foldStatus(healthy(processes))).toEqual({ level: 'good', sentence: 'No problems reported.' })
    }
  })

  it('lets a Poor fact win, and outranks a stopped MCP server', () => {
    expect(
      foldStatus({
        ...reads({ status: 'degraded', storage: { database_available: false, demo_mode: false } }),
        processes: { agent_layer: 'not_running' },
      }),
    ).toEqual({ level: 'poor', sentence: 'Database is unavailable.' })
    expect(
      foldStatus({
        ...healthy({ agent_layer: 'not_running', daemon: 'running' }),
        mcp: { statuses: [{ name: 'splunk', status: 'stopped', enabled: true }] },
      }),
    ).toEqual({ level: 'fair', sentence: 'Agent layer is not running.' })
  })
})

describe('foldStatus tool servers', () => {
  const mcp = (statuses: NonNullable<StatusReads['mcp']>['statuses']): StatusReads => ({
    health: null,
    federation: null,
    mcp: { statuses },
    routability: null,
    processes: null,
  })

  it('reads a fresh install as clear: rules not fetched and servers not used yet are not faults', () => {
    expect(
      foldStatus(
        mcp([
          { name: 'security-detections', status: 'not_set_up', enabled: true },
          { name: 'splunk-selfhosted', status: 'on_demand', enabled: true },
        ]),
      ),
    ).toEqual({ level: 'good', sentence: 'No problems reported.' })
  })

  it('still names an enabled server that went down', () => {
    expect(
      foldStatus(
        mcp([
          { name: 'security-detections', status: 'not_set_up', enabled: true },
          { name: 'splunk-selfhosted', status: 'disconnected', enabled: true },
        ]),
      ),
    ).toEqual({ level: 'fair', sentence: 'MCP server splunk-selfhosted is disconnected.' })
  })
})

describe('authBypassed', () => {
  afterEach(() => vi.unstubAllEnvs())

  it('is on when the build was made with the bypass on, before any read', () => {
    vi.stubEnv('VITE_DEV_MODE', 'true')
    expect(authBypassed(null)).toBe(true)
  })

  // The deployment this exists for: a prebuilt bundle, built with auth on,
  // served by a backend running DEV_MODE=true.
  it('is on when the backend reports the bypass, whatever the build said', () => {
    vi.stubEnv('VITE_DEV_MODE', 'false')
    expect(authBypassed({ auth_bypassed: true })).toBe(true)
  })

  it('is off when neither says so', () => {
    vi.stubEnv('VITE_DEV_MODE', 'false')
    expect(authBypassed({ auth_bypassed: false })).toBe(false)
    vi.stubEnv('VITE_DEV_MODE', '')
    expect(authBypassed({})).toBe(false)
  })

  it('claims nothing from a failed read', () => {
    vi.stubEnv('VITE_DEV_MODE', 'false')
    expect(authBypassed(null)).toBe(false)
  })
})
