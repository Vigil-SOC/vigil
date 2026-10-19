import { useRef, type KeyboardEvent, type PointerEvent, type RefObject } from 'react'
import { THREAD_MIN } from './useThreadHeight'

/** One arrow-key press, in px. */
const STEP = 24

/**
 * The top edge of the case composer: drag it, or focus it and use the arrow
 * keys, to give the thread more or less of the drawer. Double-click restores
 * the default. The tab above keeps a floor in CSS, so the thread stops there.
 */
export function ThreadGrip({
  bodyRef,
  height,
  preview,
  commit,
}: {
  bodyRef: RefObject<HTMLElement>
  height: number | null
  preview: (height: number) => void
  commit: (height: number | null) => void
}) {
  // the thread's bottom edge holds still while its top follows the pointer
  const drag = useRef<{ bottom: number; offset: number; asked: number; moved: boolean } | null>(null)
  const shown = () => bodyRef.current?.getBoundingClientRect().height || height || THREAD_MIN
  // keeps what the layout allowed, not what was asked, so a drag past the tab's floor leaves no slack to drag back through
  const settle = (asked: number) =>
    requestAnimationFrame(() => {
      const laid = Math.round(bodyRef.current?.getBoundingClientRect().height ?? 0)
      commit(laid > 0 && laid < asked ? Math.max(THREAD_MIN, laid) : asked)
    })

  const onPointerDown = (e: PointerEvent<HTMLDivElement>) => {
    const el = bodyRef.current
    if (!el || e.button !== 0) return
    const box = el.getBoundingClientRect()
    drag.current = { bottom: box.bottom, offset: box.top - e.clientY, asked: Math.round(box.height), moved: false }
    e.currentTarget.setPointerCapture?.(e.pointerId)
    e.preventDefault()
  }
  const onPointerMove = (e: PointerEvent<HTMLDivElement>) => {
    const at = drag.current
    if (!at) return
    at.asked = Math.max(THREAD_MIN, Math.round(at.bottom - (e.clientY + at.offset)))
    at.moved = true
    preview(at.asked)
  }
  const onPointerUp = (e: PointerEvent<HTMLDivElement>) => {
    const at = drag.current
    if (!at) return
    drag.current = null
    e.currentTarget.releasePointerCapture?.(e.pointerId)
    // a press that never moved is half of a click or a double-click, which must not pin the height it saw
    if (at.moved) settle(at.asked)
  }
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
      e.preventDefault()
      const asked = Math.max(THREAD_MIN, Math.round(shown() + (e.key === 'ArrowUp' ? STEP : -STEP)))
      preview(asked)
      settle(asked)
    } else if (e.key === 'Home') {
      e.preventDefault()
      commit(null)
    }
  }

  return (
    <div
      className="composer-grip"
      role="separator"
      aria-orientation="horizontal"
      aria-label="Resize the thread"
      aria-valuemin={THREAD_MIN}
      aria-valuenow={height ?? undefined}
      title="Drag to resize the thread · double-click to reset"
      tabIndex={0}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
      onKeyDown={onKeyDown}
      onDoubleClick={() => commit(null)}
    />
  )
}
