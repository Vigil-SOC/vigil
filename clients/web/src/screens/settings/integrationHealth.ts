import type { Level } from '../../shared/LevelBadge'
import type { IntegrationMetadata } from '../../config/integrationSchema'
import { getIntegrationForServer } from './integrationsData'

/** `last_test` of GET /config/integrations: the result POST .../test stored. */
export interface LastTest {
  at: string
  success: boolean | null
  error: string | null
}

export interface ServerRow {
  name: string
  integration?: IntegrationMetadata
  isEnabled: boolean
  isRunning: boolean
  /** the integration's config gate (extension: gate D) */
  configured: boolean
  /** extension master switch: configured on both gates / on only one */
  isExtension: boolean
  extConfigured: boolean
  masterOn: boolean
  masterMixed: boolean
  /** On in Connected; everything else is offered under Add integration */
  connected: boolean
  /** null is "no level": neutral, and `word` says why */
  level: Level
  word: string
  note: string
  lastTest?: LastTest
}

export interface HealthInput {
  servers: string[]
  statuses: Record<string, string>
  enabled: Record<string, boolean>
  errors: Record<string, string>
  missingCredentials: Record<string, string[]>
  enabledIntegrations: string[]
  integrations: Record<string, Record<string, unknown>>
  lastTest: Record<string, LastTest>
}

/** One row per MCP server. Nobody synthesises Fair (epic #1636 decision 8). */
export function buildRows(i: HealthInput): ServerRow[] {
  return i.servers.map((name) => {
    const integration = getIntegrationForServer(name)
    const isEnabled = !!i.enabled[name]
    const isRunning = i.statuses[name] === 'running'
    const configured = integration ? i.enabledIntegrations.includes(integration.id) : false
    const isExtension = !!integration?.fields?.some((f) => f.name === 'connectorUrl')
    const masterOn = configured && isEnabled
    const lastTest = integration ? i.lastTest[integration.id] : undefined
    const missing = i.missingCredentials[name] ?? []
    const failedTest = lastTest?.success === false
    // "Missing X" already says what a "missing credentials: X" error does
    const repeatsMissing = (t: string) => missing.length > 0 && /missing credentials/i.test(t)
    const notes = [
      missing.length ? `Missing ${missing.join(', ')}` : '',
      i.errors[name] || '',
      failedTest ? lastTest.error || 'Last test failed' : '',
    ].filter((n, idx, all) => n && all.indexOf(n) === idx && !(idx > 0 && repeatsMissing(n)))
    // a server nobody connected has no health to report
    const connected = isEnabled || configured
    const level: Level = !connected ? null : notes.length ? 'poor' : isRunning ? 'good' : null
    return {
      name,
      integration,
      isEnabled,
      isRunning,
      configured,
      isExtension,
      extConfigured: Boolean(isExtension && integration && i.integrations[integration.id]?.['connectorUrl']),
      masterOn,
      masterMixed: isExtension && configured !== isEnabled,
      connected,
      level,
      word: level ? '' : isEnabled ? 'Not running' : integration && !configured ? 'Not set up' : 'Off',
      note: notes.join(' — '),
      lastTest,
    }
  })
}

/**
 * A row switched off stays on Connected, shown Off, while the page lives, so it
 * can be switched back on there. `kept` collects every server seen connected.
 */
export function keepConnected(rows: ServerRow[], kept: Set<string>): ServerRow[] {
  for (const r of rows) if (r.connected) kept.add(r.name)
  return rows.map((r) => (!r.connected && kept.has(r.name) ? { ...r, connected: true, word: 'Off' } : r))
}

/** The one definition of "Need attention": Poor. */
export const needsAttention = (r: ServerRow) => r.level === 'poor'

/** "12 min ago", "3 h ago", "2 d ago". */
export function relativeTime(iso: string, now = Date.now()): string {
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return '—'
  const s = Math.max(0, Math.round((now - t) / 1000))
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)} min ago`
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`
  return `${Math.floor(s / 86400)} d ago`
}
