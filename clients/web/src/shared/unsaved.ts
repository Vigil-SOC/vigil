// An editor with unsaved work marks its root with data-unsaved. Leaving the screen
// (the top nav, a search result, the story-day reload) checks for it first.
import { useEffect } from 'react'

export const UNSAVED_SELECTOR = '[data-unsaved]'

export const hasUnsaved = () => typeof document !== 'undefined' && document.querySelector(UNSAVED_SELECTOR) !== null

/** True when nothing is unsaved, or the person agrees to drop it. */
export const leaveUnsaved = () => !hasUnsaved() || window.confirm('You have unsaved changes. Leave and discard them?')

/** While `dirty`, closing or reloading the tab asks the browser's own "Leave site?" first. */
export function useWarnOnUnload(dirty: boolean) {
  useEffect(() => {
    if (!dirty) return
    const onUnload = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = '' }
    window.addEventListener('beforeunload', onUnload)
    return () => window.removeEventListener('beforeunload', onUnload)
  }, [dirty])
}
