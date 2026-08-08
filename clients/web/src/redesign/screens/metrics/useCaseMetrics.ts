/* ============================================================
   Data hook for the Case Metrics screen — mirrors the legacy
   CaseMetrics page: summary KPIs, MTTD/MTTR (overall + by
   priority), cases-by-priority, status distribution, and
   analyst performance. All via caseMetricsApi.
   ============================================================ */
import { useCallback, useEffect, useState } from 'react'
import { caseMetricsApi } from '../../../services/api'
import type { Phase } from '../cases/useCases'

export type { Phase } from '../cases/useCases'

export interface PriorityRow {
  priority: string
  count: number
  closed_count: number
}

export interface StatusRow {
  status: string
  count: number
}

export interface AnalystRow {
  analyst_id: string
  analyst_name: string
  cases_assigned: number
  cases_resolved: number
  avg_resolution_time: number // hours
}

export interface CaseMetricsData {
  totalCases: number
  openCases: number
  criticalCases: number
  mttdHours: number
  mttrHours: number
  mttdByPriority: Record<string, number>
  mttrByPriority: Record<string, number>
  byPriority: PriorityRow[]
  byStatus: StatusRow[]
  analysts: AnalystRow[]
}

interface SummaryResp {
  total_cases?: number
  open_cases?: number
  critical_cases?: number
}
interface MttdResp {
  average_mttd_hours?: number
  mttd_by_priority?: Record<string, number>
}
interface MttrResp {
  average_mttr_hours?: number
  mttr_by_priority?: Record<string, number>
}

/** all case-metrics data for a rolling window of `days` (affects MTTD/MTTR) */
export function useCaseMetrics(days: number) {
  const [data, setData] = useState<CaseMetricsData | null>(null)
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)

    const end = new Date()
    const start = new Date(end.getTime() - days * 24 * 60 * 60 * 1000)
    const dateParams = { start_date: start.toISOString(), end_date: end.toISOString() }

    Promise.all([
      caseMetricsApi.getSummary(),
      caseMetricsApi.getMTTD(dateParams),
      caseMetricsApi.getMTTR(dateParams),
      caseMetricsApi.getByPriority(),
      caseMetricsApi.getByStatus(),
      caseMetricsApi.getAnalystPerformance(),
    ])
      .then(([sRes, mttdRes, mttrRes, priRes, statRes, anaRes]) => {
        if (cancelled) return
        const s = (sRes.data || {}) as SummaryResp
        const mttd = (mttdRes.data || {}) as MttdResp
        const mttr = (mttrRes.data || {}) as MttrResp
        setData({
          totalCases: s.total_cases ?? 0,
          openCases: s.open_cases ?? 0,
          criticalCases: s.critical_cases ?? 0,
          mttdHours: mttd.average_mttd_hours ?? 0,
          mttrHours: mttr.average_mttr_hours ?? 0,
          mttdByPriority: mttd.mttd_by_priority ?? {},
          mttrByPriority: mttr.mttr_by_priority ?? {},
          byPriority: (priRes.data?.priority_breakdown || []) as PriorityRow[],
          byStatus: (statRes.data?.status_breakdown || []) as StatusRow[],
          analysts: (anaRes.data?.analyst_performance || []) as AnalystRow[],
        })
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setError((e as { message?: string })?.message || 'Failed to load case metrics')
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [days, reloadKey])

  return { data, phase, error, reload }
}
