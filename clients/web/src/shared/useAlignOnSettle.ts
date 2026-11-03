import { useEffect, type RefObject } from 'react'

const SETTLE_MS = 2000

/** Cards above an anchor settle after load: keep it in view until they do, or until the reader moves. */
export function useAlignOnSettle(anchorId: string | null, page: RefObject<HTMLElement | null>) {
  useEffect(() => {
    if (!anchorId) return
    const align = () => document.getElementById(anchorId)?.scrollIntoView?.({ block: 'start' })
    align()
    if (typeof ResizeObserver === 'undefined' || !page.current) return
    const watch = new ResizeObserver(align)
    watch.observe(page.current)
    const stop = () => watch.disconnect()
    const timer = window.setTimeout(stop, SETTLE_MS)
    const events = ['wheel', 'touchstart', 'keydown', 'pointerdown'] as const
    events.forEach((e) => window.addEventListener(e, stop, { once: true, passive: true }))
    return () => {
      stop()
      window.clearTimeout(timer)
      events.forEach((e) => window.removeEventListener(e, stop))
    }
  }, [anchorId, page])
}
