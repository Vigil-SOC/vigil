/** The shell status line polls routability on a timer. A key write fires this
    so "No routable provider." does not linger for the interval. */
export const ROUTABILITY_CHANGED = 'vigil:routability-changed'

export function notifyRoutabilityChanged() {
  window.dispatchEvent(new Event(ROUTABILITY_CHANGED))
}
