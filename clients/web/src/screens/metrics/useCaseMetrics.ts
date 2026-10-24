import { useCallback, useEffect, useState } from 'react'
import { caseMetricsApi } from '../../services/api'
import type { Schema } from '../../services/apiTypes'
import type { Phase } from '../cases/useCases'

interface AnalystRow {
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
  mttdHours: number | null
  mttrHours: number | null
  mttdByPriority: Record<string, number | null>
  mttrByPriority: Record<string, number | null>
  byPriority: Schema<'PriorityBreakdownRow'>[]
  byStatus: Schema<'StatusBreakdownRow'>[]
  analysts: AnalystRow[]
}

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
        const s = sRes.data
        const mttd = mttdRes.data
        const mttr = mttrRes.data
        setData({
          totalCases: s.total_cases,
          openCases: s.open_cases,
          criticalCases: s.critical_cases,
          mttdHours: mttd.average_mttd_hours ?? null,
          mttrHours: mttr.average_mttr_hours ?? null,
          mttdByPriority: mttd.mttd_by_priority ?? {},
          mttrByPriority: mttr.mttr_by_priority ?? {},
          byPriority: priRes.data.priority_breakdown ?? [],
          byStatus: statRes.data.status_breakdown ?? [],
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
