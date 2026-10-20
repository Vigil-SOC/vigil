import { useCallback, useEffect, useRef, useState } from 'react'
import { orchestratorApi, configApi } from '../../services/api'
import type { Phase } from '../cases/useCases'

export type { Phase } from '../cases/useCases'

interface OrchestratorCost {
  total_cost_usd: number
  active_cost_usd: number
  hourly_cost_usd: number
  hourly_budget_remaining: number
  /** Intake is held because the rolling hour reached the cap; running work continues. */
  hourly_paused?: boolean
  per_investigation_limit: number
}

export interface OrchestratorStatus {
  enabled: boolean
  active_agents: number
  max_concurrent_agents: number
  queued: number
  completed: number
  failed: number
  total_investigations: number
  cost: OrchestratorCost
  stats: Record<string, number>
}

export interface Investigation {
  investigation_id: string
  case_id: string | null
  skill_id: string
  trigger_type: string
  status: string
  current_step: number
  total_steps: number
  iteration_count: number
  cost_usd: number
  priority: string
  created_at: string
  last_activity_at: string | null
  summary: string | null
  current_activity: string | null
}

const STATUS_PRIORITY: Record<string, number> = {
  executing: 0,
  assigned: 1,
  queued: 2,
  completed: 3,
  failed: 4,
}

function sortInvestigations(list: Investigation[]): Investigation[] {
  return list.slice().sort((a, b) => {
    const pa = STATUS_PRIORITY[a.status] ?? 2
    const pb = STATUS_PRIORITY[b.status] ?? 2
    if (pa !== pb) return pa - pb
    return new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
  })
}

interface ApiErr {
  response?: { data?: { detail?: string } }
  message?: string
}
const errMsg = (e: unknown, fallback: string) =>
  (e as ApiErr)?.response?.data?.detail || (e as ApiErr)?.message || fallback

export function useAutoOps() {
  const [status, setStatus] = useState<OrchestratorStatus | null>(null)
  const [investigations, setInvestigations] = useState<Investigation[]>([])
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const hasData = useRef(false)

  const load = useCallback(async (silent = false) => {
    if (!silent && !hasData.current) setPhase('loading')
    try {
      const [s, inv] = await Promise.all([
        orchestratorApi.getStatus(),
        orchestratorApi.listInvestigations(),
      ])
      setStatus(s.data)
      setInvestigations(sortInvestigations(inv.data?.investigations || []))
      hasData.current = true
      setError(null)
      setPhase('ready')
    } catch (e) {
      // a failed poll after we already have data keeps the last good view
      if (!hasData.current) setPhase('error')
      setError(errMsg(e, 'Failed to load autonomous operations'))
    }
  }, [])

  useEffect(() => {
    load()
    const id = setInterval(() => load(true), 10000)
    return () => clearInterval(id)
  }, [load])

  /** run a control action, surface errors, then refresh the live view */
  const run = useCallback(
    async (key: string, fn: () => Promise<unknown>, fallback: string) => {
      setBusy(key)
      try {
        await fn()
        await load(true)
        return true
      } catch (e) {
        setError(errMsg(e, fallback))
        return false
      } finally {
        setBusy(null)
      }
    },
    [load],
  )

  const toggleEnabled = useCallback(() => {
    if (!status) return
    return run(
      'toggle',
      () => (status.enabled ? orchestratorApi.disable() : orchestratorApi.enable()),
      'Failed to toggle autonomous operations',
    )
  }, [status, run])

  const killAll = useCallback(
    () => run('killAll', () => orchestratorApi.kill(), 'Failed to kill agents'),
    [run],
  )

  const setMaxAgents = useCallback(
    (n: number) => {
      if (n < 1 || n > 10) return
      return run(
        'maxAgents',
        async () => {
          const current = (await configApi.getOrchestrator()).data
          await configApi.setOrchestrator({ ...current, max_concurrent_agents: n })
        },
        'Failed to update agent limit',
      )
    },
    [run],
  )

  const scanFindings = useCallback(async () => {
    setBusy('scan')
    setNotice(null)
    try {
      const res = await orchestratorApi.scanFindings()
      setNotice(res.data?.message || 'Scan started.')
      await load(true)
    } catch (e) {
      setError(errMsg(e, 'Scan failed'))
    } finally {
      setBusy(null)
    }
  }, [load])

  // Restart queues a new investigation; the old one stays on record.
  const wake = useCallback(
    async (id: string) => {
      setNotice(null)
      return run(
        `wake:${id}`,
        async () => {
          const res = await orchestratorApi.wakeInvestigation(id)
          setNotice(res.data?.message || 'New investigation queued.')
        },
        'Failed to restart investigation',
      )
    },
    [run],
  )

  const killInvestigation = useCallback(
    (id: string) =>
      run(`kill:${id}`, () => orchestratorApi.killInvestigation(id), 'Failed to kill investigation'),
    [run],
  )

  return {
    status,
    investigations,
    phase,
    error,
    notice,
    busy,
    reload: () => load(false),
    clearError: () => setError(null),
    clearNotice: () => setNotice(null),
    toggleEnabled,
    killAll,
    setMaxAgents,
    scanFindings,
    wake,
    killInvestigation,
  }
}
