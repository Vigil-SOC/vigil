/* ============================================================
   Data hooks for the Cases screen — fetch via the shared axios
   client in services/api.ts (auth/CSRF/401-refresh included),
   map onto the redesign view shapes, expose loading/error states.
   useEffect + local state, matching the rest of the app (no
   React-Query anywhere yet). See REDESIGN_GAPS.md §9.
   ============================================================ */
import { useCallback, useEffect, useState } from 'react'
import { format } from 'date-fns'
import { casesApi, findingsApi } from '../../../services/api'
import { mapApiCase, mapApiFinding, type ApiCase, type ApiFinding } from '../../data/mappers'
import type { CaseRow, Finding } from '../../data/data'
import type { Activity, ResolutionStep } from './CaseSections'

export type Phase = 'loading' | 'ready' | 'error'

/** list of all cases */
export function useCases() {
  const [rows, setRows] = useState<CaseRow[]>([])
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)
    casesApi
      .getAll()
      .then((res) => {
        if (cancelled) return
        const list = (res.data?.cases || []) as ApiCase[]
        setRows(list.map(mapApiCase))
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
  }, [reloadKey])

  return { rows, phase, error, reload }
}

/** a single case + its linked findings (capped) */
export interface TimelineEntry {
  event: string
  time: string
}

/** severity breakdown across a case's findings (old design's key metrics) */
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
        const data = res.data as ApiCase & {
          created_at?: string
          activities?: Activity[]
          resolution_steps?: ResolutionStep[]
        }
        setRow(mapApiCase(data))
        setActivities(data.activities || [])
        setResolutionSteps(data.resolution_steps || [])
        const d = data.created_at ? new Date(data.created_at) : null
        setCreated(d && !Number.isNaN(d.getTime()) ? format(d, 'MMM d, yyyy · HH:mm') : '—')
        setTimeline(
          (data.timeline || [])
            .filter((t) => t.event)
            .map((t) => {
              const td = t.timestamp ? new Date(t.timestamp) : null
              return {
                event: t.event as string,
                time: td && !Number.isNaN(td.getTime()) ? format(td, 'MMM d · HH:mm') : '—',
              }
            }),
        )
        // GET /cases/{id} returns full finding objects (include_findings=True),
        // so prefer those — accurate, uncapped severity counts in one request.
        // Fall back to per-id fetches for the JSON/demo path that only carries
        // finding_ids (cap to keep that fallback light).
        let all: Finding[]
        if (data.findings && data.findings.length) {
          all = data.findings.map((f) => mapApiFinding(f as ApiFinding))
        } else {
          const ids = (data.finding_ids || []).slice(0, 8)
          const settled = await Promise.all(
            ids.map((fid) =>
              findingsApi
                .getById(fid)
                .then((r) => mapApiFinding(r.data as ApiFinding))
                .catch(() => null),
            ),
          )
          all = settled.filter((f): f is Finding => f !== null)
        }
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
