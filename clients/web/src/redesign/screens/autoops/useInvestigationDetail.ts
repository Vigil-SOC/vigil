/* ============================================================
   Detail hook for a single autonomous investigation — loads the
   investigation record, its reasoning trace (GH #79) and its
   chain-of-custody timeline (GH #192). The trace and custody are
   best-effort: a failure there leaves that section empty rather
   than failing the whole detail view.
   ============================================================ */
import { useCallback, useEffect, useState } from 'react'
import { orchestratorApi, reasoningApi } from '../../../services/api'
import type { Phase } from '../cases/useCases'

export interface ProposedAction {
  action?: string
  type?: string
  target?: string
  entity?: string
  reason?: string
  description?: string
}

export interface LogEntry {
  ts?: string
  event?: string
  iteration?: number
  cost_usd?: number
  reason?: string
}

export interface InvestigationDetailData {
  investigation_id: string
  status: string
  skill_id: string
  iteration_count: number
  cost_usd: number
  summary?: string | null
  master_review_notes?: string | null
  last_error?: string | null
  proposed_actions?: ProposedAction[]
  files?: string[]
  recent_log?: LogEntry[]
  [k: string]: unknown
}

interface ApiErr {
  response?: { data?: { detail?: string } }
  message?: string
}

export function useInvestigationDetail(id: string | null) {
  const [detail, setDetail] = useState<InvestigationDetailData | null>(null)
  const [reasoning, setReasoning] = useState<Record<string, unknown>[]>([])
  const [coc, setCoc] = useState<Record<string, unknown> | null>(null)
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [bump, setBump] = useState(0)
  const reload = useCallback(() => setBump((b) => b + 1), [])

  useEffect(() => {
    if (!id) {
      setDetail(null)
      setReasoning([])
      setCoc(null)
      return
    }
    let cancelled = false
    setPhase('loading')
    setError(null)
    setReasoning([])
    setCoc(null)
    ;(async () => {
      try {
        const res = await orchestratorApi.getInvestigation(id)
        if (cancelled) return
        setDetail(res.data)
        setPhase('ready')
      } catch (e) {
        if (cancelled) return
        const err = e as ApiErr
        setError(err?.response?.data?.detail || err?.message || 'Failed to load investigation')
        setPhase('error')
        return
      }
      // reasoning trace — best effort
      try {
        const trace = await reasoningApi.listInvestigationInteractions(id, { limit: 500 })
        if (!cancelled) setReasoning((trace?.interactions as Record<string, unknown>[]) || [])
      } catch {
        if (!cancelled) setReasoning([])
      }
      // chain of custody — best effort
      try {
        const c = await orchestratorApi.getChainOfCustody(id)
        if (!cancelled) setCoc(c.data)
      } catch {
        if (!cancelled) setCoc(null)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [id, bump])

  return { detail, reasoning, coc, phase, error, reload }
}
