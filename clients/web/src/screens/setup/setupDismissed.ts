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

// Per-browser wizard progress: the furthest step reached (1-5) and the steps
// the person continued past. Any unreadable value reads as "not started".
export const SETUP_PROGRESS_KEY = 'vigil.setupProgress'
export const SETUP_STEP_COUNT = 5

export interface SetupProgress {
  furthest: number
  passed: number[]
}

const inRange = (n: unknown): n is number =>
  Number.isInteger(n) && (n as number) >= 1 && (n as number) <= SETUP_STEP_COUNT

export function readSetupProgress(): SetupProgress {
  try {
    const raw = JSON.parse(localStorage.getItem(SETUP_PROGRESS_KEY) ?? 'null')
    if (raw && inRange(raw.furthest) && Array.isArray(raw.passed) && raw.passed.every(inRange)) {
      return { furthest: raw.furthest, passed: [...new Set<number>(raw.passed)] }
    }
  } catch {
    /* unreadable or malformed */
  }
  return { furthest: 1, passed: [] }
}

export function writeSetupProgress(progress: SetupProgress): void {
  try {
    localStorage.setItem(SETUP_PROGRESS_KEY, JSON.stringify(progress))
  } catch {
    /* private mode */
  }
}
