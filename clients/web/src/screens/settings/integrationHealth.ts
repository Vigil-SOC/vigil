import type { Level } from '../../shared/LevelBadge'
import type { IntegrationMetadata } from '../../config/integrationSchema'
import type { IntegrationHealthSource, IntegrationHealthState, SourceFacts } from '../../services/api'
import { getIntegrationForServer } from './integrationsData'

/** `last_test` of GET /config/integrations: the result POST .../test stored. The wizard still shows it. */
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
  /** a built-in alert source: its switch also governs collection (the integration's own gate) */
  collects: boolean
  /** collects: settings are saved, so switching on can also switch collection on */
  saved: boolean
  masterOn: boolean
  masterMixed: boolean
  /** On in Connected; everything else is offered under Add integration */
  connected: boolean
  /** null is "no level": neutral. Never Good without a healthy server state. */
  level: Level
  /** MCP process fact, when the health cell is not showing the server's wording */
  word: string
  note: string
  /** GET /api/integration-health failed: the row must not look healthy */
  healthFailed: boolean
  /** the server's judgement for this server's integration; null when it has none */
  judgement: IntegrationHealthSource | null
  /** the one action that sets the server up, while it waits on one */
  setupAction?: string
}

interface HealthInput {
  servers: string[]
  statuses: Record<string, string>
  enabled: Record<string, boolean>
  errors: Record<string, string>
  missingCredentials: Record<string, string[]>
  /** servers that cannot start until the install provides something */
  setups?: Record<string, { reason: string; action?: string }>
  enabledIntegrations: string[]
  integrations: Record<string, Record<string, unknown>>
  /** null: the health read failed. Rows with an integration then show a failed read. */
  health: IntegrationHealthSource[] | null
}

/** Federation source id → integration id. Same pairs as core/integrations/health.py. */
const FEDERATION_INTEGRATION: Record<string, string> = {
  elastic: 'elastic-siem',
  azure_sentinel: 'azure-sentinel',
  aws_security_hub: 'aws-security-hub',
  microsoft_defender: 'microsoft-defender',
}

/** Integrations the daemon and Alert collection pull from. Same set as LEGACY_POLLED in core/storage/source_health.py. */
const PULLED_SOURCES = new Set(['splunk', 'crowdstrike', 'opensearch', ...Object.values(FEDERATION_INTEGRATION)])

export function healthId(federationSourceId: string): string {
  return FEDERATION_INTEGRATION[federationSourceId] ?? federationSourceId
}

/** unhealthy and failing_once are Poor; healthy is Good; every other state has no level. */
export function healthLevel(state: IntegrationHealthState): Level {
  switch (state) {
    case 'healthy':
      return 'good'
    case 'unhealthy':
    case 'failing_once':
      return 'poor'
    case 'no_attempt':
    case 'requires_test':
    case 'not_checked':
    case 'switched_off':
    case 'unknown':
      return null
    default: {
      const unexpected: never = state
      void unexpected
      return null
    }
  }
}

/** The server's wording. Times are filled from `since`; the state itself is not recomputed. */
export function healthPhrase(source: Pick<IntegrationHealthSource, 'state' | 'reason' | 'since'>, now = Date.now()): string {
  const when = source.since ? relativeTime(source.since, now) : ''
  switch (source.state) {
    case 'healthy':
      return when ? `last confirmed working ${when}` : 'last confirmed working'
    case 'no_attempt':
      return 'no attempt yet'
    case 'requires_test':
      return 'requires a test'
    case 'not_checked':
      return source.reason ? `not checked: ${source.reason}` : 'not checked'
    case 'unknown':
      return when ? `unknown since ${when}` : 'unknown'
    case 'unhealthy':
      return source.reason || 'unhealthy'
    case 'failing_once':
      return source.reason || 'last attempt failed'
    case 'switched_off':
      return 'Switched off'
    default: {
      const unexpected: never = source.state
      void unexpected
      return ''
    }
  }
}

/** Wording for the health cell and the Last verified cell. Empty when the row has nothing to say. */
export function rowPhrase(r: Pick<ServerRow, 'healthFailed' | 'integration' | 'judgement'>, now = Date.now()): string {
  if (r.healthFailed && r.integration) return 'Could not read'
  if (!r.judgement) return ''
  return healthPhrase(r.judgement, now)
}

/** One row per MCP server. Level comes only from the server's state. Nobody synthesises Fair. */
export function buildRows(i: HealthInput): ServerRow[] {
  const byId = new Map((i.health ?? []).map((s) => [s.source_id, s]))
  const failed = i.health === null
  return i.servers.map((name) => {
    const integration = getIntegrationForServer(name)
    const isEnabled = !!i.enabled[name]
    const isRunning = i.statuses[name] === 'running'
    const configured = integration ? i.enabledIntegrations.includes(integration.id) : false
    const isExtension = !!integration?.fields?.some((f) => f.name === 'connectorUrl')
    // saving an extension does not switch it on, so a saved one counts as connected (shown Off)
    const extConfigured = Boolean(isExtension && integration && i.integrations[integration.id]?.['connectorUrl'])
    const collects = Boolean(integration && PULLED_SOURCES.has(integration.id))
    const saved = Boolean(integration && i.integrations[integration.id])
    // A saved built-in stays on Connected when its own on/off is off. An
    // extension still needs a connector URL (extConfigured) to count as set up.
    const savedBuiltIn = saved && !isExtension
    // an alert source reads On while either gate is, so Off never hides live collection
    const masterOn = collects ? isEnabled || configured : configured && isEnabled
    const missing = i.missingCredentials[name] ?? []
    // "Missing X" already says what a "missing credentials: X" error does
    const repeatsMissing = (t: string) => missing.length > 0 && /missing credentials/i.test(t)
    const faults = [
      missing.length ? `Missing ${missing.join(', ')}` : '',
      i.errors[name] || '',
    ].filter((n, idx, all) => n && all.indexOf(n) === idx && !(idx > 0 && repeatsMissing(n)))
    // waiting on setup or on a first call is a runtime fact, not a health input
    const setup = i.setups?.[name]
    const waiting = setup ? 'Not set up' : i.statuses[name] === 'on_demand' ? 'Starts on first use' : ''
    const stopped = isEnabled && !waiting && !isRunning
    const mcpWord = waiting || (stopped ? 'Not running' : '')
    const unconfigured = integration && !configured && !extConfigured && !savedBuiltIn ? 'Not set up' : ''
    const runtime = mcpWord || unconfigured
    const judgement = !failed && integration ? byId.get(integration.id) ?? null : null
    // no integration, or no record, is neutral — never Good
    const level = judgement ? healthLevel(judgement.state) : null
    const phrase = judgement ? healthPhrase(judgement) : ''
    // the health cell is taken by a level, the server's wording, or a failed read of this integration
    const cellTaken = Boolean((failed && integration) || level || phrase)
    // An alert source's switch is its agent tools'; Alert collection runs on its own
    // schedule, which the health cell reports. Say so, or Good reads wrong beside Off.
    // Not while health can't vouch for the schedule (unknown: overdue), and after any
    // fault, so a Poor row still leads with what needs fixing.
    const collecting =
      !isEnabled &&
      judgement?.kind === 'pulled' &&
      !['not_checked', 'switched_off', 'unknown'].includes(judgement.state)
    const notes = [
      level === 'poor' ? phrase : '',
      cellTaken && runtime ? runtime : '',
      setup?.reason || '',
      ...faults,
      collecting ? 'Agent tools off · alert collection still on' : '',
    ].filter((n, idx, all) => n && all.indexOf(n) === idx)
    // a server nobody connected has no health to report; a saved built-in stays
    // on Connected when switched off, tool server on or off
    const connected = isEnabled || configured || extConfigured || savedBuiltIn
    const word = cellTaken ? '' : runtime || (isEnabled || (collects && configured) ? '' : 'Off')
    return {
      name,
      integration,
      isEnabled,
      isRunning,
      configured,
      isExtension,
      extConfigured,
      collects,
      saved,
      masterOn,
      masterMixed: (isExtension || (collects && saved)) && configured !== isEnabled,
      connected,
      level,
      word,
      note: notes.join(' — '),
      healthFailed: failed,
      judgement,
      setupAction: setup?.action,
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

/** The federation poll counts of the current hour, e.g. "40 returned · 10 stored · …"; null when none are current.
 * Each count keeps its label on one line, so the text wraps only between counts. */
export function windowCounts(record: IntegrationHealthSource['record']): string | null {
  if (!record || record.returned_window == null) return null
  return [
    `${record.returned_window} returned`,
    `${record.stored_window ?? 0} stored`,
    `${record.filtered_window ?? 0} filtered`,
    `${record.duplicate_window ?? 0} duplicate`,
    `${record.dropped_window ?? 0} dropped`,
  ].map((c) => c.replace(' ', '\u00a0')).join(' · ')
}

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

/** The facts a collector read from the source (OpenCTI), as one line; empty when there are none. */
export function factsPhrase(facts: SourceFacts | null | undefined, now = Date.now()): string {
  if (!facts?.collected_at) return ''
  const missing = new Set((facts.missing ?? []).map((m) => m.fact))
  const parts: string[] = []
  const age = now - Date.parse(facts.collected_at)
  if (age > 2 * (facts.interval_seconds || 300) * 1000) parts.push(`Unknown since ${relativeTime(facts.collected_at, now)}`)
  if (!missing.has('newest_intel')) {
    parts.push(facts.newest_intel_at ? `Newest intelligence: ${relativeTime(facts.newest_intel_at, now)}` : 'No intelligence yet')
  }
  if (facts.connectors && facts.queue_messages_total != null) {
    const held = facts.connectors
      .filter((c) => c.queue_messages > 0)
      .sort((a, b) => b.queue_messages - a.queue_messages)
      .map((c) => `${c.name} ${c.queue_messages.toLocaleString()}${c.active ? '' : ' (inactive)'}`)
    const total = facts.queue_messages_total.toLocaleString()
    parts.push(held.length ? `Backlog: ${total} messages (${held.join(', ')})` : 'Backlog: none')
  }
  // each reason names the fact: "backlog: the token's role lacks …"
  for (const m of facts.missing ?? []) parts.push(m.reason.charAt(0).toUpperCase() + m.reason.slice(1))
  if (facts.version) parts.push(`OpenCTI ${facts.version}`)
  return parts.join(' · ')
}
