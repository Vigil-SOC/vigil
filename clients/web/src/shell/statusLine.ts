/** One level and one sentence from the five reads the shell polls. */

type StatusLevel = 'good' | 'fair' | 'poor'

export interface StatusFold {
  level: StatusLevel
  sentence: string
}

export interface HealthRead {
  status?: string
  storage?: { database_available?: boolean; demo_mode?: boolean }
  schema?: { state?: string }
}

/** Each running, unhealthy, not_running or unknown; the agent layer also no_token. */
export interface ProcessesRead {
  agent_layer?: string
  daemon?: string
}

export interface FederationRead {
  sources?: { source_id?: string; enabled?: boolean; consecutive_errors?: number }[]
}

export interface McpRead {
  statuses?: { name?: string; status?: string; enabled?: boolean; setup?: string; setup_action?: string }[]
}

export interface RoutabilityRead {
  providers?: Record<string, boolean>
}

export interface StatusReads {
  /** null: the call failed or was forbidden, so it is left out of the fold */
  health: HealthRead | null
  federation: FederationRead | null
  mcp: McpRead | null
  routability: RoutabilityRead | null
  processes: ProcessesRead | null
}

/** An enabled federation source that is erroring. Disabled and never-seeded sources report none. */
export function failingFederationSource(read: FederationRead | null) {
  return read?.sources?.find((source) => source.enabled && (source.consecutive_errors ?? 0) > 0)
}

/** Not running, and not down either: waiting for its first call, or for the
 * install to set it up (security-detections before its rules are fetched). */
export const WAITING_MCP_STATUSES = new Set(['on_demand', 'not_set_up'])

/** An enabled MCP server that should be running and is not. */
export function stoppedMcpServer(read: McpRead | null) {
  return read?.statuses?.find(
    (server) => server.enabled && server.status !== 'running' && !WAITING_MCP_STATUSES.has(server.status ?? ''),
  )
}

interface Fact {
  level: 'poor' | 'fair'
  sentence: string
}

/** Each one stops chat and workflow runs. */
const AGENT_LAYER_FACT: Partial<Record<string, string>> = {
  no_token: 'Agent layer has no internal token.',
  not_running: 'Agent layer is not running.',
  unhealthy: 'Agent layer is not ready.',
}

/** Worst fact wins. Poor outranks Fair; within a level, earlier checks outrank later ones. */
export function foldStatus(reads: StatusReads): StatusFold {
  const facts: Fact[] = []
  const health = reads.health
  if (health) {
    // Demo mode runs without a database by design; it is not a fault there.
    const databaseDown = health.storage?.database_available === false && !health.storage.demo_mode
    if (databaseDown) {
      facts.push({ level: 'poor', sentence: 'Database is unavailable.' })
    }
    // a degraded status the database already explains would only repeat it
    if (health.status && health.status !== 'healthy' && !databaseDown) {
      facts.push({ level: 'poor', sentence: `Health is ${health.status}.` })
    }
    const state = health.schema?.state
    if (state === 'drifted' || state === 'empty') {
      facts.push({ level: 'poor', sentence: `Schema is ${state}.` })
    }
  }

  const agentLayer = AGENT_LAYER_FACT[reads.processes?.agent_layer ?? '']
  if (agentLayer) facts.push({ level: 'fair', sentence: agentLayer })
  // Not running is the foreground default (#902), not a fault; the setup checks say it.
  if (reads.processes?.daemon === 'unhealthy') {
    facts.push({ level: 'fair', sentence: 'SOC daemon reports a stopped component.' })
  }

  const failing = failingFederationSource(reads.federation)
  if (failing) {
    facts.push({
      level: 'fair',
      sentence: `Federation source ${failing.source_id ?? 'unknown'} has consecutive errors.`,
    })
  }

  const down = stoppedMcpServer(reads.mcp)
  if (down) {
    facts.push({
      level: 'fair',
      sentence: `MCP server ${down.name ?? 'unknown'} is ${down.status ?? 'not running'}.`,
    })
  }

  if (reads.routability) {
    const providers = reads.routability.providers ?? {}
    if (!Object.values(providers).some(Boolean)) {
      facts.push({ level: 'fair', sentence: 'No routable provider.' })
    }
  }

  const worst = facts.find((fact) => fact.level === 'poor') ?? facts[0]
  if (!worst) return { level: 'good', sentence: 'No problems reported.' }
  return worst
}
