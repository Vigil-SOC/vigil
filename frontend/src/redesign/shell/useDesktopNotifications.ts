/* ============================================================
   Desktop-notification consumer for the redesign (REDESIGN_GAPS.md
   §10). The shared notificationService already knows how to fire OS
   notifications; this hook is the missing consumer:
     1. reads the General `show_notifications` setting on mount and
        gates the service accordingly (mirrors NotificationContext);
     2. when enabled AND the browser has granted permission, polls
        the findings feed and fires a desktop notification for each
        newly-arrived finding (baselining the backlog on first tick).
   Load-on-mount mirrors the production NotificationContext: toggling
   the setting on mid-session takes effect for show()-gating
   immediately, but the poll starts on the next load.
   ============================================================ */
import { useEffect } from 'react'
import { configApi, findingsApi } from '../../services/api'
import { notificationService } from '../../services/notifications'

interface ListedFinding {
  finding_id: string
  severity?: string
  title?: string
  description?: string
}

const POLL_MS = 30_000
const MAX_PER_TICK = 5

export function useDesktopNotifications() {
  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const seen = new Set<string>()
    let baselined = false
    const supported = typeof window !== 'undefined' && 'Notification' in window

    const tick = async () => {
      try {
        const res = await findingsApi.getAll({ limit: 25 })
        const list = (res.data as { findings?: ListedFinding[] })?.findings || []
        if (!baselined) {
          // establish a baseline so we don't notify for the existing backlog
          list.forEach((f) => seen.add(f.finding_id))
          baselined = true
        } else {
          const fresh = list.filter((f) => !seen.has(f.finding_id))
          fresh.forEach((f) => seen.add(f.finding_id))
          // cap per-tick to avoid a notification storm on a big batch
          fresh.slice(0, MAX_PER_TICK).forEach((f) =>
            notificationService.notifyNewFinding({
              finding_id: f.finding_id,
              title: f.title,
              severity: f.severity,
              description: f.description,
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
