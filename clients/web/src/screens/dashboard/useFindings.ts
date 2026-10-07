import { useCallback, useEffect, useState } from 'react'
import { casesApi, findingsApi, type ExclusionView } from '../../services/api'
import { mapApiFinding, type ApiFinding } from '../../data/mappers'
import type { Finding } from '../../data/data'
import type { Phase } from '../cases/useCases'

export type { Phase } from '../cases/useCases'

/** polls in the background, so new findings appear live. Findings naming an
 *  analyst-excluded IP are hidden unless the view asks for them. */
export function useFindings(exclusions: ExclusionView = 'hide') {
  const [rows, setRows] = useState<Finding[]>([])
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false

    // silent = background poll, so the loading state doesn't flash
    const fetchFindings = (silent: boolean) => {
      if (!silent) {
        setPhase('loading')
        setError(null)
      }
      findingsApi
        .getAll({ limit: 1000, exclusions })
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
  }, [reloadKey, exclusions])

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

/** the summary endpoints, so these are true totals and not the capped list */
export function useDashboardKpis() {
  const [kpis, setKpis] = useState<DashKpis | null>(null)
  const [phase, setPhase] = useState<Phase>('loading')
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false

    const fetchKpis = (silent: boolean) => {
      if (!silent) setPhase('loading')
      // excluded findings are not counted: the KPIs describe the queue
      Promise.all([findingsApi.getSummary({ exclusions: 'hide' }), casesApi.getSummary()])
        .then(([fRes, cRes]) => {
          if (cancelled) return
          const f = (fRes.data || {}) as FindingsSummary
          const c = cRes.data
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
          if (cancelled || silent) return
          setPhase('error')
        })
    }

    fetchKpis(false)
    const id = setInterval(() => fetchKpis(true), 10_000)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [reloadKey])

  return { kpis, phase, reload }
}
