import { useCallback, useEffect, useState } from 'react'
import { agentsApi, aiDecisionsApi, approvalsApi, type AgentSummary } from '../../services/api'
import { mapApiDecision, type ApiDecision } from '../../data/mappers'
import type { Decision } from '../../data/appData'

export type Phase = 'loading' | 'ready' | 'error'

function errMsg(e: unknown, fallback: string): string {
  const r = e as { response?: { data?: { detail?: string } }; message?: string }
  return r?.response?.data?.detail || r?.message || fallback
}

/** the daemon is not an agent, so this never comes back from GET /agents */
const ORCHESTRATION_DECISION_ID = 'orchestration'

/** derived from the agent registry, so a new agent reaches the filter without
 *  a frontend edit (#476) */
export function useDecisionAgentIds(): string[] {
  const [ids, setIds] = useState<string[]>([ORCHESTRATION_DECISION_ID])

  useEffect(() => {
    let cancelled = false
    agentsApi
      .listAgents()
      .then((res) => {
        if (cancelled) return
        const agents = (res.data?.agents || []) as AgentSummary[]
        const decisionIds = agents.map((a) => a.decision_id || a.id)
        setIds([...new Set([...decisionIds, ORCHESTRATION_DECISION_ID])])
      })
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [])

  return ids
}

export type DecisionStatus = 'all' | 'pending' | 'completed'

export function useDecisions(agentId: string, status: DecisionStatus) {
  const [rows, setRows] = useState<Decision[]>([])
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)
    const params: { agent_id?: string; has_feedback?: boolean; limit: number } = {
      limit: 100,
    }
    if (agentId !== 'all') params.agent_id = agentId
    if (status === 'pending') params.has_feedback = false
    if (status === 'completed') params.has_feedback = true
    aiDecisionsApi
      .list(params)
      .then((res) => {
        if (cancelled) return
        const list = (res.data || []) as ApiDecision[]
        setRows(list.map(mapApiDecision))
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setError(errMsg(e, 'Failed to load decisions'))
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [agentId, status, reloadKey])

  return { rows, phase, error, reload }
}

export function usePendingDecisions() {
  const [rows, setRows] = useState<Decision[]>([])
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)
    aiDecisionsApi
      .getPendingFeedback(50)
      .then((res) => {
        if (cancelled) return
        const list = (res.data || []) as ApiDecision[]
        setRows(list.map(mapApiDecision))
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setError(errMsg(e, 'Failed to load pending decisions'))
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  return { rows, phase, error, reload }
}

export interface DecisionStats {
  total_decisions: number
  feedback_rate: number // 0–1
  total_with_feedback: number
  agreement_rate: number // 0–1
  avg_accuracy_grade: number // 0–1
  total_time_saved_hours: number
  total_time_saved_minutes: number
  period_days: number
  /** actual_outcome → count (true_positive / false_positive / …) */
  outcomes: Record<string, number>
}

export function useDecisionStats() {
  const [stats, setStats] = useState<DecisionStats | null>(null)
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)
    aiDecisionsApi
      .getStats()
      .then((res) => {
        if (cancelled) return
        setStats(res.data as DecisionStats)
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setError(errMsg(e, 'Failed to load decision stats'))
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  return { stats, phase, error, reload }
}

export interface ApprovalAction {
  action_id: string
  title?: string
  description?: string
  target?: string
  workflow_run_id?: string
  workflow_phase_id?: string
  reason?: string
  created_at?: string
  /**
   * What the agent layer stamped on the approval when it raised it — the
   * checkpoint id and its class. A compose phase carries neither.
   */
  parameters?: Record<string, unknown>
}

// Polled, because a parked run is waiting on a person and nothing else tells
// them: a question raised after the tab was opened used to sit there unseen
// until someone happened to reload.
const APPROVALS_POLL_MS = 20_000

export function usePendingApprovals() {
  const [actions, setActions] = useState<ApprovalAction[]>([])
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setError(null)
    const tick = () =>
      approvalsApi
        .listPending()
        .then((res) => {
          if (cancelled) return
          setActions((res.data?.actions || []) as ApprovalAction[])
          setPhase('ready')
        })
        .catch((e) => {
          if (cancelled) return
          // A poll that failed is not an empty queue: keep what was last shown
          // rather than reporting that nothing is waiting.
          setError(errMsg(e, 'Failed to load approvals'))
          setPhase((p) => (p === 'ready' ? p : 'error'))
        })

    void tick()
    const timer = setInterval(tick, APPROVALS_POLL_MS)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [reloadKey])

  return { actions, phase, error, reload }
}
