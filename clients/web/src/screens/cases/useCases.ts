import { useCallback, useEffect, useState } from 'react'
import { format } from 'date-fns'
import { casesApi } from '../../services/api'
import { mapApiCase, mapQueueCase } from '../../data/mappers'
import type { CaseRow } from '../../data/data'

export type Phase = 'loading' | 'ready' | 'error'

export interface CaseInvestigationRef {
  investigation_id: string
  status: string
  workflow_id: string
  run_id: string
  live: boolean
  cost_usd: number
  max_cost_usd: number
  budget_health: string
  iteration_count: number
  created_at?: string | null
}

export interface CaseClosureView {
  closure_category: string
  closed_by: string
  closed_by_kind: string
  verdict: string
}

export interface CaseLinkedFinding {
  finding_id: string
  description: string | null
  source_link: string | null
}

/** Matches ``CaseRepository`` page size. */
export const CASE_PAGE_LIMIT = 100

export interface CaseFilters {
  query: string
  state: string
  priority: string
  sla: '' | 'risk'
  assignee: string
  workflow: string
  dataSource: string
  limit: number
  offset: number
}

export const INITIAL_CASE_FILTERS: CaseFilters = {
  query: '',
  state: '',
  priority: 'any',
  sla: '',
  assignee: '',
  workflow: '',
  dataSource: '',
  limit: CASE_PAGE_LIMIT,
  offset: 0,
}

export interface CaseStrip {
  by_state: Record<string, number>
  sla_at_risk: number
  closed_today: number
  agent_closure_share: number
}

export const EMPTY_STRIP: CaseStrip = {
  by_state: {},
  sla_at_risk: 0,
  closed_today: 0,
  agent_closure_share: 0,
}

function toParams(f: CaseFilters) {
  const params: NonNullable<Parameters<typeof casesApi.getAll>[0]> = {
    limit: f.limit,
    offset: f.offset,
  }
  if (f.state === 'closed') params.closed = true
  else if (f.state) params.state = f.state
  if (f.priority && f.priority !== 'any') params.priority = f.priority
  if (f.sla === 'risk') params.sla_at_risk = true
  if (f.assignee.trim()) params.assignee = f.assignee.trim()
  if (f.workflow.trim()) params.workflow = f.workflow.trim()
  if (f.dataSource.trim()) params.data_source = f.dataSource.trim()
  if (f.query.trim()) params.query = f.query.trim()
  return params
}

export const CASES_CHANGED = 'vigil:cases-changed'

/** Tells any mounted case list to reload, e.g. after an edit made in the drawer over it. */
export const notifyCasesChanged = () => window.dispatchEvent(new Event(CASES_CHANGED))

export function useCases(filters: CaseFilters) {
  const [rows, setRows] = useState<CaseRow[]>([])
  const [total, setTotal] = useState(0)
  const [strip, setStrip] = useState<CaseStrip>(EMPTY_STRIP)
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    window.addEventListener(CASES_CHANGED, reload)
    return () => window.removeEventListener(CASES_CHANGED, reload)
  }, [reload])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)
    casesApi
      .getAll(toParams(filters))
      .then((res) => {
        if (cancelled) return
        const data = res.data as typeof res.data & {
          strip?: CaseStrip
          total?: number
        }
        const cases = data.cases ?? []
        setRows(cases.map((c) => mapQueueCase(c)))
        setTotal(data.total ?? cases.length)
        setStrip(data.strip ?? EMPTY_STRIP)
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setError((e as { message?: string })?.message || 'Failed to load cases')
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [filters, reloadKey])

  return { rows, total, strip, phase, error, reload }
}

function asInvestigations(raw: unknown): CaseInvestigationRef[] {
  if (!Array.isArray(raw)) return []
  return raw.flatMap((item) => {
    if (!item || typeof item !== 'object') return []
    const o = item as Record<string, unknown>
    if (typeof o.investigation_id !== 'string' || typeof o.run_id !== 'string') return []
    return [{
      investigation_id: o.investigation_id,
      status: typeof o.status === 'string' ? o.status : '',
      workflow_id: typeof o.workflow_id === 'string' ? o.workflow_id : '',
      run_id: o.run_id,
      live: o.live === true,
      cost_usd: typeof o.cost_usd === 'number' ? o.cost_usd : 0,
      max_cost_usd: typeof o.max_cost_usd === 'number' ? o.max_cost_usd : 0,
      budget_health: typeof o.budget_health === 'string' ? o.budget_health : 'healthy',
      iteration_count: typeof o.iteration_count === 'number' ? o.iteration_count : 0,
      created_at: typeof o.created_at === 'string' ? o.created_at : null,
    }]
  })
}

function asLinkedFindings(raw: unknown): CaseLinkedFinding[] {
  if (!Array.isArray(raw)) return []
  return raw.flatMap((item) => {
    if (!item || typeof item !== 'object') return []
    const o = item as Record<string, unknown>
    if (typeof o.finding_id !== 'string' || !o.finding_id) return []
    return [{
      finding_id: o.finding_id,
      description: typeof o.description === 'string' ? o.description : null,
      source_link: typeof o.source_link === 'string' && o.source_link ? o.source_link : null,
    }]
  })
}

function asClosure(raw: unknown): CaseClosureView | null {
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  if (typeof o.closure_category !== 'string') return null
  return {
    closure_category: o.closure_category,
    closed_by: typeof o.closed_by === 'string' ? o.closed_by : '',
    closed_by_kind: typeof o.closed_by_kind === 'string' ? o.closed_by_kind : '',
    verdict: typeof o.verdict === 'string' ? o.verdict : '',
  }
}

export function useCaseDetail(id: string | null) {
  const [row, setRow] = useState<CaseRow | null>(null)
  const [created, setCreated] = useState<string>('—')
  const [combinedState, setCombinedState] = useState('')
  const [investigations, setInvestigations] = useState<CaseInvestigationRef[]>([])
  const [closure, setClosure] = useState<CaseClosureView | null>(null)
  const [linkedFindings, setLinkedFindings] = useState<CaseLinkedFinding[]>([])
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    if (!id) return
    let cancelled = false
    setPhase('loading')
    setError(null)
    setRow(null)
    setCombinedState('')
    setInvestigations([])
    setClosure(null)
    setLinkedFindings([])
    casesApi
      .getById(id)
      .then((res) => {
        if (cancelled) return
        const data = res.data as typeof res.data & {
          combined_state?: unknown
          investigations?: unknown
          closure?: unknown
          linked_findings?: unknown
        }
        setRow(mapApiCase(data))
        setCombinedState(typeof data.combined_state === 'string' ? data.combined_state : '')
        setInvestigations(asInvestigations(data.investigations))
        setClosure(asClosure(data.closure))
        setLinkedFindings(asLinkedFindings(data.linked_findings))
        const d = data.created_at ? new Date(data.created_at) : null
        setCreated(d && !Number.isNaN(d.getTime()) ? format(d, 'MMM d, yyyy · HH:mm') : '—')
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setError((e as { message?: string })?.message || 'Failed to load case')
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [id, reloadKey])

  return { row, created, combinedState, investigations, closure, linkedFindings, phase, error, reload }
}
