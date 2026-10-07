import { useEffect, useState } from 'react'
import { consoleApi, federationApi, mcpApi, storageApi } from '../../services/api'
import { Icon, type IconName } from '../../shared/icons'
import { SettingsCard } from '../../shared/ui'
import { readProviderConfigured } from '../../routing/useSetupStatus'
import {
  failingFederationSource,
  stoppedMcpServer,
  type FederationRead,
  type McpRead,
} from '../../shell/statusLine'
import type { CheckPhase } from './CheckMark'
import CheckTable, { type CheckRow } from './CheckTable'

export type { CheckPhase }

type CheckId = 'database' | 'provider' | 'storage' | 'health' | 'federation' | 'mcp'

export type SystemCheck = CheckRow & { id: CheckId }

type Result = Pick<SystemCheck, 'phase' | 'detail'>

interface StorageRead {
  backend?: string
  description?: string
  database_available?: boolean
  demo_mode?: boolean
}

/** Database and Storage share one read per run */
type Read = (storage: () => Promise<StorageRead>) => Promise<Result>

const COULD_NOT_READ: Result = { phase: 'needs', detail: 'Could not read' }

const NEEDS: { icon: IconName; title: string; sub: string }[] = [
  {
    icon: 'link',
    title: 'Access to one alert source',
    sub: 'A SIEM, EDR or identity tool, or a file export to upload',
  },
  {
    icon: 'bot',
    title: 'An AI provider key, or a local model',
    sub: 'Anthropic, OpenAI or your cloud; or an Ollama server for fully local use',
  },
  { icon: 'clock', title: 'About 15 minutes', sub: 'Nothing runs on its own until you finish' },
]

const readHealth = async (): Promise<Result> => {
  const res = await consoleApi.getHealth()
  const status = (res.data as { status?: string } | undefined)?.status
  if (status && status !== 'healthy') return { phase: 'needs', detail: `Status is ${status}` }
  return { phase: 'passed', detail: status || 'Reachable' }
}

const readDatabase: Read = async (storage) => {
  const read = await storage()
  if (read.demo_mode) return { phase: 'passed', detail: 'Demo data, no database needed' }
  return read.database_available
    ? { phase: 'passed', detail: 'Connected' }
    : { phase: 'needs', detail: 'Not connected' }
}

const readStorage: Read = async (storage) => {
  const { backend, description } = await storage()
  return {
    phase: 'passed',
    detail: [backend || 'Unknown backend', description].filter(Boolean).join(' · '),
  }
}

const readProvider = async (): Promise<Result> => {
  const ready = await readProviderConfigured()
  return ready
    ? { phase: 'passed', detail: 'Ready' }
    : { phase: 'needs', detail: 'You add a provider in step 3' }
}

const readFederation = async (): Promise<Result> => {
  const read = (await federationApi.getHealth()).data as FederationRead | undefined
  const failing = failingFederationSource(read ?? null)
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
  const down = stoppedMcpServer(read ?? null)
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

const CHECKS: { id: CheckId; label: string; read: Read }[] = [
  { id: 'database', label: 'Database', read: readDatabase },
  { id: 'provider', label: 'Model gateway', read: readProvider },
  { id: 'storage', label: 'Storage', read: readStorage },
  { id: 'health', label: 'API health', read: readHealth },
  { id: 'federation', label: 'Alert collection', read: readFederation },
  { id: 'mcp', label: 'Tool servers', read: readMcp },
]

const INITIAL: SystemCheck[] = CHECKS.map(({ id, label }) => ({
  id,
  label,
  phase: 'waiting',
  detail: '',
}))

export default function SystemChecksStep() {
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
    const storage = () =>
      (storageRead ??= storageApi.getStatus().then((res) => (res.data ?? {}) as StorageRead))
    // one after another: only the current row shows Checking, the rest wait
    ;(async () => {
      for (const { id, read } of CHECKS) {
        if (!live) return
        patch(id, { phase: 'checking', detail: '' })
        const result = await read(storage).catch(() => COULD_NOT_READ)
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

  return (
    <>
      <SettingsCard
        title="System checks"
        desc="Vigil checked this server before you start."
        actions={
          <button className="btn ghost" onClick={() => setNonce((n) => n + 1)}>
            <Icon name="refresh" size={14} /> Check again
          </button>
        }
      >
        <CheckTable
          rows={checks}
          label="System checks"
          summary={
            warnings
              ? {
                  tone: 'fair',
                  text: `${warnings} ${warnings === 1 ? 'warning' : 'warnings'}. You can continue; fix them before you rely on alerts.`,
                }
              : undefined
          }
        />
      </SettingsCard>
      <SettingsCard title="What you'll need">
        <ul className="su-needs">
          {NEEDS.map((need) => (
            <li key={need.title}>
              <span className="su-needs-icon">
                <Icon name={need.icon} size={16} />
              </span>
              <span>
                <span className="su-needs-t">{need.title}</span>
                <span className="su-needs-s">{need.sub}</span>
              </span>
            </li>
          ))}
        </ul>
      </SettingsCard>
    </>
  )
}
