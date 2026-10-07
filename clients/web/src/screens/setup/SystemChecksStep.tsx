import { useEffect, useState } from 'react'
import { consoleApi, storageApi } from '../../services/api'
import { Icon } from '../../shared/icons'
import { readProviderConfigured } from '../../routing/useSetupStatus'

export type CheckPhase = 'pending' | 'running' | 'pass' | 'fail'

export interface SystemCheck {
  id: 'health' | 'storage' | 'provider'
  label: string
  phase: CheckPhase
  detail: string
}

const PHASE_LABEL: Record<CheckPhase, string> = {
  pending: 'Pending',
  running: 'Running',
  pass: 'Pass',
  fail: 'Fail',
}

const INITIAL: SystemCheck[] = [
  { id: 'health', label: 'API health', phase: 'pending', detail: '' },
  { id: 'storage', label: 'Storage', phase: 'pending', detail: '' },
  { id: 'provider', label: 'AI provider', phase: 'pending', detail: '' },
]

async function readHealth(): Promise<SystemCheck> {
  try {
    const res = await consoleApi.getHealth()
    const status = (res.data as { status?: string } | undefined)?.status
    return { id: 'health', label: 'API health', phase: 'pass', detail: status || 'Reachable' }
  } catch {
    return { id: 'health', label: 'API health', phase: 'fail', detail: 'Could not read' }
  }
}

async function readStorage(): Promise<SystemCheck> {
  try {
    const res = await storageApi.getStatus()
    const backend = (res.data as { backend?: string } | undefined)?.backend
    return {
      id: 'storage',
      label: 'Storage',
      phase: 'pass',
      detail: backend || 'Unknown backend',
    }
  } catch {
    return { id: 'storage', label: 'Storage', phase: 'fail', detail: 'Could not read' }
  }
}

async function readProvider(): Promise<SystemCheck> {
  try {
    const ready = await readProviderConfigured()
    return {
      id: 'provider',
      label: 'AI provider',
      phase: ready ? 'pass' : 'fail',
      detail: ready ? 'Ready' : 'Not configured',
    }
  } catch {
    return { id: 'provider', label: 'AI provider', phase: 'fail', detail: 'Could not read' }
  }
}

export default function SystemChecksStep() {
  const [checks, setChecks] = useState<SystemCheck[]>(INITIAL)
  const [nonce, setNonce] = useState(0)

  useEffect(() => {
    let live = true
    setChecks((rows) => rows.map((row) => ({ ...row, phase: 'running', detail: '' })))
    Promise.all([readHealth(), readStorage(), readProvider()]).then((rows) => {
      if (live) setChecks(rows)
    })
    return () => {
      live = false
    }
  }, [nonce])

  return (
    <div className="flex flex-col gap-2">
      <div className="flex justify-end">
        <button className="btn ghost" onClick={() => setNonce((n) => n + 1)}>
          <Icon name="refresh" size={14} /> Retry
        </button>
      </div>
      {checks.map((check) => (
        <div key={check.id} className="flex items-center gap-3 text-sm">
          <span className="text-tx font-medium w-28 shrink-0">{check.label}</span>
          <span className={check.phase === 'fail' ? 'text-high' : 'text-tx-2'}>
            {PHASE_LABEL[check.phase]}
          </span>
          {check.detail && <span className="text-tx-3 text-xs">{check.detail}</span>}
        </div>
      ))}
    </div>
  )
}
