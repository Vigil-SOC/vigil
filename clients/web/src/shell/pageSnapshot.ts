// What the open page shows, in a few lines of text, so Ask Vigil can answer
// "what matters on this page?" from the rows on screen. A screen registers it
// with usePageSnapshot; Chat sends the one for the current page.
import { useEffect } from 'react'

// Matches PAGE_SNAPSHOT_MAX in services/api/routers/claude.py.
export const PAGE_SNAPSHOT_MAX = 12_000
const snapshots = new Map<string, string>()

export function getPageSnapshot(key?: string | null): string | undefined {
  if (!key) return undefined
  const text = snapshots.get(key)
  if (!text) return undefined
  // Cut by code points, so the cut cannot leave half of a surrogate pair.
  return Array.from(text).slice(0, PAGE_SNAPSHOT_MAX).join('')
}

export function usePageSnapshot(key: string, text: string): void {
  useEffect(() => {
    snapshots.set(key, text)
    return () => {
      if (snapshots.get(key) === text) snapshots.delete(key)
    }
  }, [key, text])
}
