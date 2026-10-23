/* The custody load is best-effort: a failure leaves that section
   empty rather than failing the whole detail view. */
import { useCallback, useEffect, useState } from 'react'
import { orchestratorApi } from '../../services/api'
import type { Phase } from '../cases/useCases'

interface ProposedAction {
  action?: string
  type?: string
  target?: string
  entity?: string
  reason?: string
  description?: string
}

interface LogEntry {
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

export interface Interaction {
  interaction_id?: string
  created_at?: string
  model?: string
  stop_reason?: string
  has_thinking?: boolean
  thinking_content?: string
  response_content?: string
  tool_calls?: { name?: string; input?: unknown }[]
  tool_results?: { tool_use_id?: string; is_error?: boolean; content?: unknown }[]
  input_tokens?: number
  output_tokens?: number
  cost_usd?: number
  duration_ms?: number
}

export interface Coc {
  investigation?: { created_at?: string; workflow_id?: string; trigger_type?: string; priority?: string }
  logs?: { timestamp?: string; event_type?: string; details?: Record<string, unknown>; tokens_used?: number }[]
  llm_interactions?: Interaction[]
  otel_trace_id?: string
}

interface ApiErr {
  response?: { data?: { detail?: string } }
  message?: string
}

export function useInvestigationDetail(id: string | null) {
  const [detail, setDetail] = useState<InvestigationDetailData | null>(null)
  const [coc, setCoc] = useState<Coc | null>(null)
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [bump, setBump] = useState(0)
  const reload = useCallback(() => setBump((b) => b + 1), [])

  useEffect(() => {
    if (!id) {
      setDetail(null)
      setCoc(null)
      return
    }
    let cancelled = false
    setPhase('loading')
    setError(null)
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

  return { detail, coc, phase, error, reload }
}
