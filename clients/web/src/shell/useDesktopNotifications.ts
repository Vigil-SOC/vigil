import { useEffect } from 'react'
import { configApi, findingsApi } from '../services/api'
import { notificationService } from '../services/notifications'

interface ListedFinding {
  finding_id: string
  severity?: string
  title?: string
  description?: string
  created_at?: string
}

const POLL_MS = 30_000
const MAX_PER_TICK = 5

// Raw ISO strings: `new Date()` rounds to milliseconds and would merge distinct arrivals.
function newestCreatedAt(list: ListedFinding[]): string | undefined {
  let newest: string | undefined
  for (const finding of list) {
    const createdAt = finding.created_at
    if (createdAt && (newest === undefined || createdAt > newest)) newest = createdAt
  }
  return newest
}

export function useDesktopNotifications() {
  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    // Newest arrival already accounted for, plus the ids that share that exact timestamp.
    let mark: string | undefined
    const tieIds = new Set<string>()
    const supported = typeof window !== 'undefined' && 'Notification' in window

    const setMark = (list: ListedFinding[], next: string) => {
      mark = next
      tieIds.clear()
      for (const finding of list) {
        if (finding.created_at === next) tieIds.add(finding.finding_id)
      }
    }

    const tick = async () => {
      try {
        // excluded IPs are hidden from the queue, so they page nobody either
        const res = await findingsApi.getAll({
          limit: 25,
          exclusions: 'hide',
          sort_by: 'created_at',
          sort_order: 'desc',
        })
        const list = (res.data as { findings?: ListedFinding[] })?.findings || []
        if (mark === undefined) {
          // no mark yet (including an empty first poll): baseline, alert on nothing
          const newest = newestCreatedAt(list)
          if (newest !== undefined) setMark(list, newest)
        } else {
          const current = mark
          const fresh = list.filter((finding) => {
            if (!finding.created_at) return false
            if (finding.created_at > current) return true
            return finding.created_at === current && !tieIds.has(finding.finding_id)
          })
          const newest = newestCreatedAt(list)
          if (newest !== undefined && newest > current) {
            setMark(list, newest)
          } else {
            for (const finding of list) {
              if (finding.created_at === current) tieIds.add(finding.finding_id)
            }
          }
          // cap per-tick to avoid a notification storm on a big batch
          fresh.slice(0, MAX_PER_TICK).forEach((finding) =>
            notificationService.notifyNewFinding({
              finding_id: finding.finding_id,
              title: finding.title,
              severity: finding.severity,
              description: finding.description,
            }),
          )
        }
      } catch {
        /* best-effort — backend may be unavailable */
      } finally {
        if (!cancelled) timer = setTimeout(tick, POLL_MS)
      }
    }

    configApi
      .getGeneral()
      .then((res) => {
        if (cancelled) return
        const enabled = Boolean((res.data as { show_notifications?: boolean })?.show_notifications)
        notificationService.setEnabled(enabled)
        // only poll when the user opted in AND already granted browser permission
        // (the Settings toggle requests permission from a real user gesture)
        if (enabled && supported && Notification.permission === 'granted') {
          tick()
        }
      })
      .catch(() => {
        /* leave notifications off if the config can't be read */
      })

    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [])
}
