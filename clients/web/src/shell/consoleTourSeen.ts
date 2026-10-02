// Per-browser "I've seen the console tour". An unset key shows it once.
// Skip, Done, and a finished replay all write the same flag. The account
// menu starts the tour again without clearing it.
export const CONSOLE_TOUR_SEEN_KEY = 'vigil.consoleTourSeen'

export function readConsoleTourSeen(): boolean {
  try {
    return localStorage.getItem(CONSOLE_TOUR_SEEN_KEY) === '1'
  } catch {
    return false
  }
}

export function markConsoleTourSeen(): void {
  try {
    localStorage.setItem(CONSOLE_TOUR_SEEN_KEY, '1')
  } catch {
    /* private mode */
  }
}
