/* ============================================================
   Data + actions hook for the Auto Ops (Autonomous Operations)
   screen — mirrors the legacy Orchestrator page: orchestrator
   status + the investigation queue, polled every 10s, plus the
   control actions (enable/disable, max-agents, scan, kill-all,
   wake/kill/review per investigation). All via orchestratorApi.
   ============================================================ */
import { useCallback, useEffect, useRef, useState } from 'react'
import { orchestratorApi, configApi } from '../../../services/api'
import type { Phase } from '../cases/useCases'

export type { Phase } from '../cases/useCases'

export interface OrchestratorCost {
  total_cost_usd: number
  active_cost_usd: number
  hourly_cost_usd: number
  hourly_budget_remaining: number
  per_investigation_limit: number
}

export interface OrchestratorStatus {
  enabled: boolean
  active_agents: number
  max_concurrent_agents: number
  queued: number
  completed: number
  failed: number
  pending_review: number
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

/** order the queue by "needs attention first", then newest — matches legacy */
const STATUS_PRIORITY: Record<string, number> = {
  executing: 0,
  assigned: 1,
  needs_rework: 2,
  review_submitted: 3,
  queued: 4,
  completed: 5,
  failed: 6,
}

function sortInvestigations(list: Investigation[]): Investigation[] {
  return list.slice().sort((a, b) => {
    const pa = STATUS_PRIORITY[a.status] ?? 4
    const pb = STATUS_PRIORITY[b.status] ?? 4
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
  /** which control action is in flight (disables the relevant button) */
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

  const wake = useCallback(
    (id: string) =>
      run(`wake:${id}`, () => orchestratorApi.wakeInvestigation(id), 'Failed to restart investigation'),
    [run],
  )

  const killInvestigation = useCallback(
    (id: string) =>
      run(`kill:${id}`, () => orchestratorApi.killInvestigation(id), 'Failed to kill investigation'),
    [run],
  )

  const review = useCallback(
    (id: string, action: 'approve' | 'rework', notes?: string) =>
      run(`review:${id}`, () => orchestratorApi.reviewInvestigation(id, action, notes), 'Review failed'),
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
    // actions
    toggleEnabled,
    killAll,
    setMaxAgents,
    scanFindings,
    wake,
    killInvestigation,
    review,
  }
}
