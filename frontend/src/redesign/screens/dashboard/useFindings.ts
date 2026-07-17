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

/** list of all findings; polls in the background so new findings appear live */
export function useFindings() {
  const [rows, setRows] = useState<Finding[]>([])
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false

    // silent = background poll: refresh rows without flashing the loading state
    const fetchFindings = (silent: boolean) => {
      if (!silent) {
        setPhase('loading')
        setError(null)
      }
      findingsApi
        .getAll({ limit: 1000 })
        .then((res) => {
          if (cancelled) return
          const list = (res.data?.findings || []) as ApiFinding[]
          setRows(list.map(mapApiFinding))
          setPhase('ready')
        })
        .catch((e) => {
          if (cancelled) return
          if (silent) return // a background blip shouldn't blank the table
          setError((e as { message?: string })?.message || 'Failed to load findings')
          setPhase('error')
        })
    }

    fetchFindings(false)
    const id = setInterval(() => fetchFindings(true), 10_000)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [reloadKey])

  return { rows, phase, error, reload }
}

export interface DashKpis {
  casesTotal: number
  casesOpen: number
  casesInvestigating: number
}

interface CasesSummary {
  total?: number
  by_status?: Record<string, number>
}

/** cases summary counts for the KPI cards (findings counts come from useFindings) */
export function useDashboardKpis() {
  const [kpis, setKpis] = useState<DashKpis | null>(null)
  const [phase, setPhase] = useState<Phase>('loading')
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    casesApi
      .getSummary()
      .then((cRes) => {
        if (cancelled) return
        const c = (cRes.data || {}) as CasesSummary
        setKpis({
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
