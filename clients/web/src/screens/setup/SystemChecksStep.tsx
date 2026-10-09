import { useEffect, useState } from 'react'
import { consoleApi, federationApi, mcpApi, storageApi } from '../../services/api'
import { Icon } from '../../shared/icons'
import { readProviderConfigured } from '../../routing/useSetupStatus'
import {
  failingFederationSource,
  stoppedMcpServer,
  type FederationRead,
  type McpRead,
} from '../../shell/statusLine'
import CheckMark, { type CheckPhase } from './CheckMark'

export type { CheckPhase }

type CheckId = 'health' | 'storage' | 'provider' | 'federation' | 'mcp'

export interface SystemCheck {
  id: CheckId
  label: string
  phase: CheckPhase
  detail: string
}

type Result = Pick<SystemCheck, 'phase' | 'detail'>

const COULD_NOT_READ: Result = { phase: 'needs', detail: 'Could not read' }

const readHealth = async (): Promise<Result> => {
  const res = await consoleApi.getHealth()
  const status = (res.data as { status?: string } | undefined)?.status
  if (status !== 'healthy') return { phase: 'needs', detail: `Status is ${status ?? 'unknown'}` }
  return { phase: 'passed', detail: status }
}

const readStorage = async (): Promise<Result> => {
  const res = await storageApi.getStatus()
  const backend = (res.data as { backend?: string } | undefined)?.backend
  return { phase: 'passed', detail: backend || 'Unknown backend' }
}

const readProvider = async (): Promise<Result> => {
  const ready = await readProviderConfigured()
  return ready
    ? { phase: 'passed', detail: 'Ready' }
    : { phase: 'needs', detail: 'Not configured' }
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
  const enabled = read?.statuses?.filter((s) => s.enabled).length ?? 0
  return {
    phase: 'passed',
    detail: enabled ? `${enabled} enabled server${enabled === 1 ? '' : 's'} running` : 'None enabled',
  }
}

const CHECKS: { id: CheckId; label: string; read: () => Promise<Result> }[] = [
  { id: 'health', label: 'API health', read: readHealth },
  { id: 'storage', label: 'Storage', read: readStorage },
  { id: 'provider', label: 'AI provider', read: readProvider },
  { id: 'federation', label: 'Federation', read: readFederation },
  { id: 'mcp', label: 'MCP servers', read: readMcp },
]

const INITIAL: SystemCheck[] = CHECKS.map(({ id, label }) => ({
  id,
  label,
  phase: 'waiting',
  detail: '',
}))

export default function SystemChecksStep() {
  const [checks, setChecks] = useState<SystemCheck[]>(INITIAL)
  const [nonce, setNonce] = useState(0)

  useEffect(() => {
    let live = true
    const patch = (id: CheckId, result: Result) =>
      setChecks((rows) => rows.map((row) => (row.id === id ? { ...row, ...result } : row)))
    setChecks(INITIAL)
    // one after another: only the current row shows Checking, the rest wait
    ;(async () => {
      for (const { id, read } of CHECKS) {
        if (!live) return
        patch(id, { phase: 'checking', detail: '' })
        const result = await read().catch(() => COULD_NOT_READ)
        if (!live) return
        patch(id, result)
      }
    })()
    return () => {
      live = false
    }
  }, [nonce])

  return (
    <div className="flex flex-col">
      <div className="flex justify-end mb-1">
        <button className="btn ghost" onClick={() => setNonce((n) => n + 1)}>
          <Icon name="refresh" size={14} /> Retry
        </button>
      </div>
      <div role="list" aria-label="System checks" aria-live="polite" className="flex flex-col">
        {checks.map((check) => (
          <div
            key={check.id}
            role="listitem"
            className="grid grid-cols-[18px_7rem_minmax(0,1fr)] gap-3 items-center py-2.5 px-0.5 border-t border-line-soft text-sm"
          >
            <CheckMark phase={check.phase} />
            <span className="text-tx font-semibold">{check.label}</span>
            <span
              className={`text-xs leading-snug ${check.phase === 'needs' ? 'text-tx' : 'text-tx-2'}`}
            >
              {check.detail}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}
