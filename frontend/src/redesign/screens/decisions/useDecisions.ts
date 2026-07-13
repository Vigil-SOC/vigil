/* ============================================================
   Data hooks for the AI Decisions screen — same useEffect +
   local-state + Phase pattern as useCases.ts (no React-Query).
   Fetch via the shared axios client in services/api.ts
   (auth/CSRF/401-refresh included), map onto the redesign view
   shapes. See DECISIONS_WIRING.md §4.
   ============================================================ */
import { useCallback, useEffect, useState } from 'react'
import { aiDecisionsApi, approvalsApi } from '../../../services/api'
import { mapApiDecision, type ApiDecision } from '../../data/mappers'
import type { Decision } from '../../data/appData'

export type Phase = 'loading' | 'ready' | 'error'

/** prefer FastAPI's `detail`, fall back to the axios message, then a default */
function errMsg(e: unknown, fallback: string): string {
  const r = e as { response?: { data?: { detail?: string } }; message?: string }
  return r?.response?.data?.detail || r?.message || fallback
}

export type DecisionStatus = 'all' | 'pending' | 'completed'

/** All Decisions tab — agent + feedback-status filters drive a refetch */
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

/** Pending tab — decisions awaiting human feedback */
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

/** KPI strip + Analytics tab — aggregate stats (optionally scoped to one agent) */
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

export function useDecisionStats(agentId: string, days?: number) {
  const [stats, setStats] = useState<DecisionStats | null>(null)
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)
    const params: { agent_id?: string; days?: number } = {}
    if (agentId !== 'all') params.agent_id = agentId
    if (days) params.days = days
    aiDecisionsApi
      .getStats(params)
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
  }, [agentId, days, reloadKey])

  return { stats, phase, error, reload }
}

/** Pending Approvals tab — separate human-in-the-loop queue (workflow + daemon) */
export interface ApprovalAction {
  action_id: string
  title?: string
  description?: string
  target?: string
  workflow_run_id?: string
  workflow_phase_id?: string
  reason?: string
  created_at?: string
}

export function usePendingApprovals() {
  const [actions, setActions] = useState<ApprovalAction[]>([])
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)
    approvalsApi
      .listPending()
      .then((res) => {
        if (cancelled) return
        setActions((res.data?.actions || []) as ApprovalAction[])
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setError(errMsg(e, 'Failed to load approvals'))
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  return { actions, phase, error, reload }
}
