/** One level and one sentence from the four reads the shell already has. */

export type StatusLevel = 'good' | 'fair' | 'poor'

export interface StatusFold {
  level: StatusLevel
  sentence: string
}

export interface HealthRead {
  status?: string
  storage?: { database_available?: boolean }
  schema?: { state?: string }
}

export interface FederationRead {
  sources?: { source_id?: string; enabled?: boolean; consecutive_errors?: number }[]
}

export interface McpRead {
  statuses?: { name?: string; status?: string; enabled?: boolean }[]
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
}

interface Fact {
  level: 'poor' | 'fair'
  sentence: string
}

/** Worst fact wins. Poor outranks Fair; within a level, earlier checks outrank later ones. */
export function foldStatus(reads: StatusReads): StatusFold {
  const facts: Fact[] = []
  const health = reads.health
  if (health) {
    if (health.status && health.status !== 'healthy') {
      facts.push({ level: 'poor', sentence: `Health is ${health.status}.` })
    }
    if (health.storage?.database_available === false) {
      facts.push({ level: 'poor', sentence: 'Storage is unavailable.' })
    }
    const state = health.schema?.state
    if (state === 'drifted' || state === 'empty') {
      facts.push({ level: 'poor', sentence: `Schema is ${state}.` })
    }
  }

  const failing = reads.federation?.sources?.find(
    (source) => source.enabled && (source.consecutive_errors ?? 0) > 0,
  )
  if (failing) {
    facts.push({
      level: 'fair',
      sentence: `Federation source ${failing.source_id ?? 'unknown'} has consecutive errors.`,
    })
  }

  const down = reads.mcp?.statuses?.find(
    (server) => server.enabled && server.status !== 'running',
  )
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
