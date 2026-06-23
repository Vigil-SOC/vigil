/* ============================================================
   Data hooks for the Dashboard Findings tab — the findings list
   plus the KPI summary cards (findings + cases stats). Same
   useEffect + shared-axios pattern as useCases. See §9.
   ============================================================ */
import { useCallback, useEffect, useState } from 'react'
import { casesApi, findingsApi } from '../../../services/api'
import { mapApiFinding, type ApiFinding } from '../../data/mappers'
import type { Finding } from '../../data/data'
import type { Phase } from '../cases/useCases'

export type { Phase } from '../cases/useCases'

/** list of all findings */
export function useFindings() {
  const [rows, setRows] = useState<Finding[]>([])
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)
    findingsApi
      .getAll()
      .then((res) => {
        if (cancelled) return
        const list = (res.data?.findings || []) as ApiFinding[]
        setRows(list.map(mapApiFinding))
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setError((e as { message?: string })?.message || 'Failed to load findings')
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  return { rows, phase, error, reload }
}

export interface DashKpis {
  findingsTotal: number
  findingsCritical: number
  findingsHigh: number
  casesTotal: number
  casesOpen: number
  casesInvestigating: number
}

interface FindingsSummary {
  total?: number
  by_severity?: Record<string, number>
}
interface CasesSummary {
  total?: number
  by_status?: Record<string, number>
}

/** the four KPI cards: findings + cases summary counts */
export function useDashboardKpis() {
  const [kpis, setKpis] = useState<DashKpis | null>(null)
  const [phase, setPhase] = useState<Phase>('loading')
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    Promise.all([findingsApi.getSummary(), casesApi.getSummary()])
      .then(([fRes, cRes]) => {
        if (cancelled) return
        const f = (fRes.data || {}) as FindingsSummary
        const c = (cRes.data || {}) as CasesSummary
        setKpis({
          findingsTotal: f.total ?? 0,
          findingsCritical: f.by_severity?.critical ?? 0,
          findingsHigh: f.by_severity?.high ?? 0,
          casesTotal: c.total ?? 0,
          casesOpen: c.by_status?.open ?? 0,
          casesInvestigating: c.by_status?.investigating ?? 0,
        })
        setPhase('ready')
      })
      .catch(() => {
        if (cancelled) return
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  return { kpis, phase, reload }
}
