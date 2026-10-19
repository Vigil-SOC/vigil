import { useState } from 'react'

/** The case composer's thread height the person chose, kept across cases. */
const KEY = 'soc.chat.threadHeight'
/** A few lines of an answer: below this the thread is a sliver, and hiding it is the way to shrink it. */
export const THREAD_MIN = 96

function stored(): number | null {
  try {
    const value = Number(localStorage.getItem(KEY))
    return Number.isFinite(value) && value >= THREAD_MIN ? value : null
  } catch {
    return null
  }
}

/**
 * The thread's height: null until the person drags it, when the CSS default
 * (a share of the drawer) applies. `preview` follows a drag; `commit` keeps it.
 */
export function useThreadHeight() {
  const [height, setHeight] = useState<number | null>(stored)
  const commit = (next: number | null) => {
    setHeight(next)
    try {
      if (next === null) localStorage.removeItem(KEY)
      else localStorage.setItem(KEY, String(Math.round(next)))
    } catch {
      /* the height still applies to this page */
    }
  }
  return { height, preview: setHeight, commit }
}
