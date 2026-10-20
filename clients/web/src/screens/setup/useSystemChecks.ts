import { useEffect, useState } from 'react'
import { consoleApi, federationApi, mcpApi, storageApi } from '../../services/api'
import { readProviderStatus } from '../../routing/useSetupStatus'
import {
  failingFederationSource,
  stoppedMcpServer,
  type FederationRead,
  type McpRead,
  type ProcessesRead,
} from '../../shell/statusLine'
import type { CheckRow } from './CheckTable'

type CheckId = 'database' | 'provider' | 'storage' | 'health' | 'agent' | 'daemon' | 'federation' | 'mcp'

type SystemCheck = CheckRow & { id: CheckId }

type Result = Pick<SystemCheck, 'phase' | 'detail'>

interface StorageRead {
  backend?: string
  description?: string
  database_available?: boolean
  demo_mode?: boolean
}

interface HealthBody {
  status?: string
  storage?: { database_available?: boolean; demo_mode?: boolean }
}

/** One read each per run: Database and API health share the health call, Database and Storage the storage call, Agent layer and SOC daemon the processes call */
interface Reads {
  storage: () => Promise<StorageRead>
  health: () => Promise<HealthBody>
  processes: () => Promise<ProcessesRead>
}
type Read = (reads: Reads) => Promise<Result>

const COULD_NOT_READ: Result = { phase: 'needs', detail: 'Could not read' }
const readHealth: Read = async ({ health }) => {
  const { status } = await health()
  if (status !== 'healthy') return { phase: 'needs', detail: `Status is ${status ?? 'unknown'}` }
  return { phase: 'passed', detail: status }
}

// Health is public. The storage route needs a signed-in user, and that lookup
// reads the database, so it fails outright when the database is the thing down.
const readDatabase: Read = async ({ health }) => {
  const read = (await health()).storage
  if (read?.demo_mode) return { phase: 'passed', detail: 'Demo data, no database needed' }
  return read?.database_available
    ? { phase: 'passed', detail: 'Connected' }
    : { phase: 'needs', detail: 'Not connected' }
}

// The console is served without either process, so these are the rows that say
// whether chat, workflow runs and alert triage can happen at all.
const readAgentLayer: Read = async ({ processes }) => {
  switch ((await processes()).agent_layer) {
    case 'running':
      return { phase: 'passed', detail: 'Running' }
    case 'no_token':
      return { phase: 'needs', detail: 'AGENT_INTERNAL_TOKEN is not set · chat and workflow runs fail' }
    case 'unhealthy':
      return { phase: 'needs', detail: 'Not ready · chat and workflow runs fail' }
    case 'not_running':
      return { phase: 'needs', detail: 'Not running · chat and workflow runs fail' }
    default:
      return COULD_NOT_READ
  }
}

const readDaemon: Read = async ({ processes }) => {
  switch ((await processes()).daemon) {
    case 'running':
      return { phase: 'passed', detail: 'Running' }
    case 'unhealthy':
      return { phase: 'needs', detail: 'Running, but a component stopped' }
    case 'not_running':
      return { phase: 'needs', detail: 'Not running · alerts are not polled or triaged' }
    default:
      return COULD_NOT_READ
  }
}

const DATABASE_UNAVAILABLE: Result = { phase: 'needs', detail: 'Database unavailable' }

const readStorage: Read = async ({ storage, health }) => {
  let read: StorageRead
  try {
    read = await storage()
  } catch (err) {
    // Auth fails first when the database is down; the public health read says why.
    // If health fails too, or says the database is up, this stays a failed read.
    const db = (await health()).storage
    if (!db?.demo_mode && db?.database_available === false) return DATABASE_UNAVAILABLE
    throw err
  }
  if (!read.demo_mode && read.database_available === false) return DATABASE_UNAVAILABLE
  return {
    phase: 'passed',
    detail: [read.backend || 'Unknown backend', read.description].filter(Boolean).join(' · '),
  }
}

const readProvider = async (): Promise<Result> => {
  const status = await readProviderStatus()
  switch (status) {
    case 'ready':
      return { phase: 'passed', detail: 'Ready' }
    case 'none':
      return { phase: 'passed', detail: 'Running on this server · you add a provider in step 3' }
    case 'unreachable':
      return { phase: 'needs', detail: 'Could not reach the model gateway' }
    default: {
      const unreachable: never = status
      return unreachable
    }
  }
}

const readFederation = async (): Promise<Result> => {
  const read = (await federationApi.getHealth()).data as FederationRead | undefined
  // no body is a failed read, not an empty list
  if (!read) throw new Error('no federation health body')
  const failing = failingFederationSource(read)
  if (failing) {
    return {
      phase: 'needs',
      detail: `${failing.source_id ?? 'A source'} has consecutive errors`,
    }
  }
  const enabled = read?.sources?.filter((s) => s.enabled).length ?? 0
  if (!enabled) {
    return {
      phase: 'waiting',
      detail: 'Nothing to check yet · the next step connects a source',
    }
  }
  return { phase: 'passed', detail: `${enabled} source${enabled === 1 ? '' : 's'} collecting` }
}

const readMcp = async (): Promise<Result> => {
  const read = (await mcpApi.getStatuses()).data as McpRead | undefined
  // no body is a failed read, not "None enabled"
  if (!read) throw new Error('no MCP status body')
  const down = stoppedMcpServer(read)
  if (down) {
    return {
      phase: 'needs',
      detail: `${down.name ?? 'A server'} is ${down.status ?? 'not running'}`,
    }
  }
  const enabled = read?.statuses?.filter((s) => s.enabled) ?? []
  const running = enabled.filter((s) => s.status === 'running').length
  const onDemand = enabled.filter((s) => s.status === 'on_demand').length
  const parts = [
    running ? `${running} enabled server${running === 1 ? '' : 's'} running` : '',
    onDemand ? `${onDemand} start${onDemand === 1 ? 's' : ''} on first use` : '',
    ...enabled.filter((s) => s.status === 'not_set_up').map((s) => `${s.name ?? 'A server'} not set up`),
  ]
  return {
    phase: 'passed',
    detail: parts.filter(Boolean).join(' · ') || 'None enabled',
  }
}

const CHECKS: { id: CheckId; label: string; read: Read }[] = [
  { id: 'database', label: 'Database', read: readDatabase },
  { id: 'provider', label: 'Model gateway', read: readProvider },
  { id: 'storage', label: 'Storage', read: readStorage },
  { id: 'health', label: 'API health', read: readHealth },
  { id: 'agent', label: 'Agent layer', read: readAgentLayer },
  { id: 'daemon', label: 'SOC daemon', read: readDaemon },
  { id: 'federation', label: 'Alert collection', read: readFederation },
  { id: 'mcp', label: 'Tool servers', read: readMcp },
]

const INITIAL: SystemCheck[] = CHECKS.map(({ id, label }) => ({
  id,
  label,
  phase: 'waiting',
  detail: '',
}))

/** One run of the system checks, owned by the wizard so the rail and step 1 read the same result */
export function useSystemChecks() {
  const [checks, setChecks] = useState<SystemCheck[]>(INITIAL)
  const [finished, setFinished] = useState(false)
  const [nonce, setNonce] = useState(0)

  useEffect(() => {
    let live = true
    const patch = (id: CheckId, result: Result) =>
      setChecks((rows) => rows.map((row) => (row.id === id ? { ...row, ...result } : row)))
    setChecks(INITIAL)
    setFinished(false)
    let storageRead: Promise<StorageRead> | undefined
    let healthRead: Promise<HealthBody> | undefined
    let processesRead: Promise<ProcessesRead> | undefined
    const health = () =>
      (healthRead ??= consoleApi.getHealth().then((res) => {
        if (!res.data) throw new Error('health has no body')
        return res.data as HealthBody
      }))
    const storage = () =>
      (storageRead ??= storageApi.getStatus().then((res) => {
        if (!res.data) throw new Error('storage status has no body')
        return res.data as StorageRead
      }))
    const processes = () =>
      (processesRead ??= consoleApi.getProcesses().then((res) => {
        if (!res.data) throw new Error('processes has no body')
        return res.data as ProcessesRead
      }))
    // one after another: only the current row shows Checking, the rest wait
    ;(async () => {
      for (const { id, read } of CHECKS) {
        if (!live) return
        patch(id, { phase: 'checking', detail: '' })
        const result = await read({ storage, health, processes }).catch(() => COULD_NOT_READ)
        if (!live) return
        patch(id, result)
      }
      setFinished(true)
    })()
    return () => {
      live = false
    }
  }, [nonce])


  // Alert collection stays Waiting until a source exists, so wait for the run, not for every row
  const warnings = finished ? checks.filter((c) => c.phase === 'needs').length : 0

  return { checks, finished, warnings, rerun: () => setNonce((n) => n + 1) }
}

export type SystemChecks = ReturnType<typeof useSystemChecks>
