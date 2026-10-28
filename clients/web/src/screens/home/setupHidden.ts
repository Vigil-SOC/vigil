// Per-browser "Not now" and "Hide setup" on Home's setup steps. The shell's Finish setup chip reads
// the same list, so a step hidden on Home stops counting there too.
export const SETUP_HIDDEN_KEY = 'vigil.home.setup.hidden'

/** Fired on window after Home hides or shows steps, so the chip recounts without a screen change. */
export const SETUP_HIDDEN_CHANGED = 'vigil:setup-hidden-changed'

/** Where Home's setup steps live: the chip and the account menu link here. */
export const HOME_SETUP_HREF = '/home?section=setup'
export const HOME_SETUP_ID = 'home-setup'

export function readSetupHidden(): string[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(SETUP_HIDDEN_KEY) || '[]')
    return Array.isArray(parsed) ? parsed.filter((id) => typeof id === 'string') : []
  } catch {
    return []
  }
}

function saveSetupHidden(next: string[]): string[] {
  try {
    if (next.length) localStorage.setItem(SETUP_HIDDEN_KEY, JSON.stringify(next))
    else localStorage.removeItem(SETUP_HIDDEN_KEY)
  } catch {
    /* private mode */
  }
  window.dispatchEvent(new Event(SETUP_HIDDEN_CHANGED))
  return next
}

/** Adds `ids` to `hidden`, saves it and returns the new list. "Hide setup" passes every served step. */
export function hideSetupSteps(hidden: string[], ids: string[]): string[] {
  return saveSetupHidden([...new Set([...hidden, ...ids])])
}

/** Adds `id` to `hidden`, saves it and returns the new list. */
export function hideSetupStep(hidden: string[], id: string): string[] {
  return hideSetupSteps(hidden, [id])
}

/** "Show hidden steps": forgets every hidden step. */
export function clearSetupHidden(): string[] {
  return saveSetupHidden([])
}

/** Steps still to do: not done and not hidden with Not now. */
export function openSetupSteps<T extends { id: string; done: boolean }>(steps: T[], hidden: string[]): T[] {
  return steps.filter((step) => !step.done && !hidden.includes(step.id))
}
