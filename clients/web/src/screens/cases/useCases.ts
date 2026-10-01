import { useCallback, useEffect, useState } from 'react'
import { format } from 'date-fns'
import { casesApi, findingsApi } from '../../services/api'
import { mapApiCase, mapApiFinding, mapQueueCase, type ApiFinding } from '../../data/mappers'
import type { CaseRow, Finding } from '../../data/data'
import type { Activity, ResolutionStep } from './CaseSections'

export type Phase = 'loading' | 'ready' | 'error'

function strField(v: unknown): string | undefined {
  return typeof v === 'string' ? v : undefined
}

function asActivities(raw: unknown[]): Activity[] {
  return raw.map((item) => {
    const o = item && typeof item === 'object' ? (item as Record<string, unknown>) : {}
    return {
      description: strField(o.description),
      activity_type: strField(o.activity_type),
      timestamp: strField(o.timestamp),
      details: o.details && typeof o.details === 'object' ? (o.details as Record<string, unknown>) : undefined,
    }
  })
}

function asResolutionSteps(raw: unknown[]): ResolutionStep[] {
  return raw.map((item) => {
    const o = item && typeof item === 'object' ? (item as Record<string, unknown>) : {}
    return {
      description: strField(o.description),
      action_taken: strField(o.action_taken),
      result: strField(o.result),
    }
  })
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

export function useCases(filters: CaseFilters) {
  const [rows, setRows] = useState<CaseRow[]>([])
  const [total, setTotal] = useState(0)
  const [strip, setStrip] = useState<CaseStrip>(EMPTY_STRIP)
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

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

export interface TimelineEntry {
  event: string
  time: string
}

export interface SevBreakdown {
  critical: number
  high: number
  medium: number
  low: number
  total: number
}

const EMPTY_SEV: SevBreakdown = { critical: 0, high: 0, medium: 0, low: 0, total: 0 }

function countSev(findings: Finding[]): SevBreakdown {
  return findings.reduce<SevBreakdown>(
    (acc, f) => {
      acc.total += 1
      const k = f.sev.toLowerCase() as 'critical' | 'high' | 'medium' | 'low'
      if (k in acc) acc[k] += 1
      return acc
    },
    { ...EMPTY_SEV },
  )
}

export function useCaseDetail(id: string | null) {
  const [row, setRow] = useState<CaseRow | null>(null)
  const [created, setCreated] = useState<string>('—')
  const [linked, setLinked] = useState<Finding[]>([])
  const [sev, setSev] = useState<SevBreakdown>(EMPTY_SEV)
  const [timeline, setTimeline] = useState<TimelineEntry[]>([])
  const [activities, setActivities] = useState<Activity[]>([])
  const [resolutionSteps, setResolutionSteps] = useState<ResolutionStep[]>([])
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    if (!id) return
    let cancelled = false
    setPhase('loading')
    setError(null)
    setLinked([])
    setSev(EMPTY_SEV)
    casesApi
      .getById(id)
      .then(async (res) => {
        if (cancelled) return
        const data = res.data
        setRow(mapApiCase(data))
        setActivities(asActivities(data.activities ?? []))
        setResolutionSteps(asResolutionSteps(data.resolution_steps ?? []))
        const d = data.created_at ? new Date(data.created_at) : null
        setCreated(d && !Number.isNaN(d.getTime()) ? format(d, 'MMM d, yyyy · HH:mm') : '—')
        setTimeline(
          (data.timeline ?? [])
            .map((item) => {
              const o = item && typeof item === 'object' ? (item as Record<string, unknown>) : {}
              const event = strField(o.event)
              if (!event) return null
              const td = o.timestamp && typeof o.timestamp === 'string' ? new Date(o.timestamp) : null
              return {
                event,
                time: td && !Number.isNaN(td.getTime()) ? format(td, 'MMM d · HH:mm') : '—',
              }
            })
            .filter((t): t is TimelineEntry => t !== null),
        )
        // GET /cases/{id} dumps CaseSchema (finding_ids only), even though
        // DatabaseDataService.get_case loads findings first. Fetch the linked
        // findings separately; cap the list to keep the request light.
        const ids = (data.finding_ids || []).slice(0, 8)
        const settled = await Promise.all(
          ids.map((fid) =>
            findingsApi
              .getById(fid)
              .then((r) => mapApiFinding(r.data as ApiFinding))
              .catch(() => null),
          ),
        )
        const all = settled.filter((f): f is Finding => f !== null)
        if (cancelled) return
        setLinked(all)
        setSev(countSev(all))
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

  return { row, created, linked, sev, timeline, activities, resolutionSteps, phase, error, reload }
}
