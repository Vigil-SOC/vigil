import { useState } from 'react'

/** The Ask Vigil dock's width the person chose, kept across pages. */
const KEY = 'soc.chat.dockWidth'
/** The dock's width until the person drags its edge. */
export const DOCK_WIDTH = 400
export const DOCK_MIN = 360

function stored(): number | null {
  try {
    const value = Number(localStorage.getItem(KEY))
    return Number.isFinite(value) && value >= DOCK_MIN ? value : null
  } catch {
    return null
  }
}

/**
 * The dock's width: null until the person drags its edge. `preview` follows a
 * drag and marks it live, so the page can stop animating its margin; `commit` keeps it.
 */
export function useDockWidth() {
  const [width, setWidth] = useState<number | null>(stored)
  const [dragging, setDragging] = useState(false)
  const preview = (next: number) => {
    setWidth(next)
    setDragging(true)
  }
  const commit = (next: number | null) => {
    setWidth(next)
    setDragging(false)
    try {
      if (next === null) localStorage.removeItem(KEY)
      else localStorage.setItem(KEY, String(Math.round(next)))
    } catch {
      /* the width still applies to this page */
    }
  }
  return { width, dragging, preview, commit }
}
