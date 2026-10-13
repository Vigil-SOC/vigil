// Per-tab "Not now" on Home's setup steps. The shell's Finish setup chip reads
// the same list, so a step hidden on Home stops counting there too.
export const SETUP_HIDDEN_KEY = 'vigil.home.setup.hidden'

/** Fired on window after Home hides a step, so the chip recounts without a screen change. */
export const SETUP_HIDDEN_CHANGED = 'vigil:setup-hidden-changed'

/** Where Home's setup steps live: the chip and the account menu link here. */
export const HOME_SETUP_HREF = '/home?section=setup'
export const HOME_SETUP_ID = 'home-setup'

export function readSetupHidden(): string[] {
  try {
    const parsed = JSON.parse(sessionStorage.getItem(SETUP_HIDDEN_KEY) || '[]')
    return Array.isArray(parsed) ? parsed.filter((id) => typeof id === 'string') : []
  } catch {
    return []
  }
}

/** Adds `id` to `hidden`, saves it and returns the new list. */
export function hideSetupStep(hidden: string[], id: string): string[] {
  const next = hidden.includes(id) ? hidden : [...hidden, id]
  try {
    sessionStorage.setItem(SETUP_HIDDEN_KEY, JSON.stringify(next))
  } catch {
    /* private mode */
  }
  window.dispatchEvent(new Event(SETUP_HIDDEN_CHANGED))
  return next
}

/** Steps still to do: not done and not hidden with Not now. */
export function openSetupSteps<T extends { id: string; done: boolean }>(steps: T[], hidden: string[]): T[] {
  return steps.filter((step) => !step.done && !hidden.includes(step.id))
}
