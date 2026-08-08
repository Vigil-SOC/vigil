/* ============================================================
   Timeline data hook — maps timelineApi.getTimelineRange onto the
   interactive Gantt's TimelineEvent shape. Technique isn't in the
   range payload, so `tech` is left blank. See REDESIGN_GAPS.md §9.
   ============================================================ */
import { useEffect, useState } from 'react'
import { timelineApi } from '../../../services/api'
import type { TimelineEvent, TimelineKind } from './attackData'
import type { Phase } from '../cases/useCases'

interface RangeEvent {
  id: string
  start?: string
  type?: string
  severity?: string
  metadata?: { finding_id?: string }
}

const sevOf = (s?: string): TimelineEvent['sev'] => {
  const v = (s || '').toLowerCase()
  return v === 'critical' || v === 'high' || v === 'low' ? v : 'medium'
}
const kindOf = (t?: string): TimelineKind => (t === 'case' ? 'case' : t === 'alert' ? 'alert' : 'finding')

export function useTimeline() {
  const [events, setEvents] = useState<TimelineEvent[]>([])
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)
    timelineApi
      .getTimelineRange({ limit: 200 })
      .then((res) => {
        if (cancelled) return
        const list = (res.data?.events || []) as RangeEvent[]
        const mapped = list
          .map((ev) => ({
            id: ev.metadata?.finding_id || ev.id,
            sev: sevOf(ev.severity),
            tech: '',
            t: ev.start ? Date.parse(ev.start) : NaN,
            kind: kindOf(ev.type),
          }))
          .filter((e) => !Number.isNaN(e.t))
          .sort((a, b) => a.t - b.t)
        setEvents(mapped)
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setError((e as { message?: string })?.message || 'Failed to load timeline')
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [])

  return { events, phase, error }
}
