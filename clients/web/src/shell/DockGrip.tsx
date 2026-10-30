import { useRef, type KeyboardEvent, type PointerEvent } from 'react'

/** One arrow-key press, in px. */
const STEP = 24

/**
 * The dock's left edge: drag it, or focus it and use the arrow keys, to widen
 * or narrow Ask Vigil. Double-click or Home restores the default width.
 */
export function DockGrip({
  width,
  min,
  max,
  preview,
  commit,
}: {
  width: number
  min: number
  max: number
  preview: (width: number) => void
  commit: (width: number | null) => void
}) {
  const drag = useRef<{ x: number; from: number; asked: number; moved: boolean } | null>(null)
  const clamp = (asked: number) => Math.round(Math.min(max, Math.max(min, asked)))

  const onPointerDown = (e: PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return
    drag.current = { x: e.clientX, from: width, asked: width, moved: false }
    e.currentTarget.setPointerCapture?.(e.pointerId)
    e.preventDefault()
  }
  const onPointerMove = (e: PointerEvent<HTMLDivElement>) => {
    const at = drag.current
    if (!at) return
    // the dock is anchored right, so moving the edge left widens it
    at.asked = clamp(at.from + at.x - e.clientX)
    at.moved = true
    preview(at.asked)
  }
  const onPointerUp = (e: PointerEvent<HTMLDivElement>) => {
    const at = drag.current
    if (!at) return
    drag.current = null
    e.currentTarget.releasePointerCapture?.(e.pointerId)
    // a press that never moved is half of a double-click, which must not pin the width it saw
    if (at.moved) commit(at.asked)
  }
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      e.preventDefault()
      commit(clamp(width + (e.key === 'ArrowLeft' ? STEP : -STEP)))
    } else if (e.key === 'Home') {
      e.preventDefault()
      commit(null)
    }
  }

  return (
    <div
      className="chat-grip"
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize Ask Vigil"
      aria-valuemin={min}
      aria-valuemax={max}
      aria-valuenow={width}
      title="Drag to resize · double-click to reset"
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
