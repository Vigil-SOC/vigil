/* ============================================================
   ATT&CK data hook — the technique rollup driving the table, the
   tactics distribution and the severity donut. Min-confidence and
   time-range are passed to the backend (server-side filtering via
   attackApi.getTechniqueRollup). Tactic/name are resolved client
   side from ./mitre (the rollup leaves them "Unknown"/== id).
   ============================================================ */
import { useCallback, useEffect, useState } from 'react'
import { attackApi } from '../../../services/api'
import { techniqueName, techniqueTactic } from '../../data/mitre'
import type { Phase } from '../cases/useCases'

export interface AttackTechnique {
  id: string
  name: string
  tactic: string
  c: number
  h: number
  m: number
  l: number
  total: number
}

interface RollupRow {
  technique_id: string
  count?: number
  severities?: { critical?: number; high?: number; medium?: number; low?: number }
}

export interface AttackData {
  techniques: AttackTechnique[]
  kpis: { techniques: number; detections: number; critical: number; high: number }
  tacticDist: [string, number][]
  sevDist: [string, number, string][]
}

export function useAttack(minConfidence: number, timeRange: string) {
  const [data, setData] = useState<AttackData | null>(null)
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  // redesign tabs use "All"; the API wants "all"
  const range = timeRange.toLowerCase()

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)
    attackApi
      .getTechniqueRollup(minConfidence, range)
      .then((res) => {
        if (cancelled) return
        const rows = (res.data?.techniques || []) as RollupRow[]
        const techniques: AttackTechnique[] = rows.map((r) => {
          const s = r.severities || {}
          return {
            id: r.technique_id,
            name: techniqueName(r.technique_id),
            tactic: techniqueTactic(r.technique_id),
            c: s.critical ?? 0,
            h: s.high ?? 0,
            m: s.medium ?? 0,
            l: s.low ?? 0,
            total: r.count ?? 0,
          }
        })
        const sum = (k: keyof AttackTechnique) => techniques.reduce((a, t) => a + (t[k] as number), 0)
        const tacMap: Record<string, number> = {}
        techniques.forEach((t) => { tacMap[t.tactic] = (tacMap[t.tactic] || 0) + t.total })
        setData({
          techniques,
          kpis: { techniques: techniques.length, detections: sum('total'), critical: sum('c'), high: sum('h') },
          tacticDist: Object.entries(tacMap).sort((a, b) => b[1] - a[1]),
          sevDist: [
            ['Critical', sum('c'), 'var(--crit)'],
            ['High', sum('h'), 'var(--high)'],
            ['Medium', sum('m'), 'var(--med)'],
            ['Low', sum('l'), 'var(--ok)'],
          ],
        })
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setError((e as { message?: string })?.message || 'Failed to load ATT&CK data')
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [minConfidence, range, reloadKey])

  return { data, phase, error, reload }
}
