/* ============================================================
   Data hooks for the Analytics screen — the full metrics payload
   (/analytics) plus the AI insights rail (/analytics/insights).
   Same useEffect + shared-axios + phase pattern as useFindings.
   ============================================================ */
import { useCallback, useEffect, useState } from 'react'
import api from '../../../services/api'
import type { Phase } from '../cases/useCases'

export type { Phase } from '../cases/useCases'

/** Backend time-range tokens. The UI's "All" tab maps to "all". */
export type TimeRange = '24h' | '7d' | '30d' | 'all'

export interface AnalyticsMetrics {
  totalFindings: number
  totalCases: number
  avgResponseTime: number
  falsePositiveRate: number
  findingsChange: number
  casesChange: number
  responseTimeChange: number
  falsePositiveChange: number
}

export interface TimeSeriesPoint {
  timestamp: string
  findings: number
  cases: number
  alerts: number
}

export interface SeverityBucket {
  name: string
  value: number
  color: string
}

export interface AlertSource {
  name: string
  count: number
}

export interface ResponseTimePoint {
  period: string
  avgTime: number
  target: number
}

export interface AffectedEntity {
  entity: string
  count: number
  critical: number
  high: number
  medium: number
  low: number
  riskScore: number
}

export interface HeatmapCell {
  day: string
  dayNum: number
  hour: number
  count: number
  critical: number
  high: number
  intensity: number
}

export interface MitreTechnique {
  techniqueId: string
  techniqueName: string
  tactic: string
  count: number
}

export interface AnalyticsData {
  metrics: AnalyticsMetrics
  timeSeriesData: TimeSeriesPoint[]
  severityDistribution: SeverityBucket[]
  topSources: AlertSource[]
  responseTimeData: ResponseTimePoint[]
  affectedEntities: AffectedEntity[]
  attackHeatmap: HeatmapCell[]
  mitreTechniques: MitreTechnique[]
}

/** full analytics payload for a given time range */
export function useAnalytics(timeRange: TimeRange) {
  const [data, setData] = useState<AnalyticsData | null>(null)
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)
    api
      .get<AnalyticsData>('/analytics', { params: { time_range: timeRange } })
      .then((res) => {
        if (cancelled) return
        setData(res.data)
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setError((e as { message?: string })?.message || 'Failed to load analytics')
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [timeRange, reloadKey])

  return { data, phase, error, reload }
}

export interface ApiInsight {
  id: string
  type: 'recommendation' | 'warning' | 'info' | 'anomaly'
  title: string
  description: string
  confidence: number // 0..1
  timestamp: string
  actionable?: boolean
}

interface InsightsResponse {
  insights: ApiInsight[]
  generated_at: string | null
  is_stale: boolean
  generating: boolean
}

export type InsightLevel = 'crit' | 'high' | 'ok' | 'med'

/** anomalies are loudest, recommendations are calm/positive */
export function insightLevel(type: ApiInsight['type']): InsightLevel {
  switch (type) {
    case 'anomaly':
      return 'crit'
    case 'warning':
      return 'high'
    case 'recommendation':
      return 'ok'
    default:
      return 'med'
  }
}

/** cached AI insights for a given time range */
export function useAnalyticsInsights(timeRange: TimeRange) {
  const [insights, setInsights] = useState<ApiInsight[]>([])
  const [generatedAt, setGeneratedAt] = useState<string | null>(null)
  const [isStale, setIsStale] = useState(false)
  const [generating, setGenerating] = useState(false)
  const [phase, setPhase] = useState<Phase>('loading')
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    api
      .get<InsightsResponse>('/analytics/insights', { params: { time_range: timeRange } })
      .then((res) => {
        if (cancelled) return
        const d = res.data || ({} as InsightsResponse)
        setInsights(d.insights || [])
        setGeneratedAt(d.generated_at ?? null)
        setIsStale(!!d.is_stale)
        setGenerating(!!d.generating)
        setPhase('ready')
      })
      .catch(() => {
        if (cancelled) return
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [timeRange, reloadKey])

  return { insights, generatedAt, isStale, generating, phase, reload }
}
