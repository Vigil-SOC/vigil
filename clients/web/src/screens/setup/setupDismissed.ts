// Per-browser "I'm done here". An unset key is not yet dismissed, including
// when a provider already exists. The account menu is the only way back.
export const SETUP_DISMISSED_KEY = 'vigil.setupDismissed'

export function readSetupDismissed(): boolean {
  try {
    return localStorage.getItem(SETUP_DISMISSED_KEY) === '1'
  } catch {
    return false
  }
}

export function markSetupDismissed(): void {
  try {
    localStorage.setItem(SETUP_DISMISSED_KEY, '1')
  } catch {
    /* private mode */
  }
}
