// The server ends a sign-in after it sits idle, counting only a person's own
// actions. A request carries this header when real input (a click, a key, the
// back button) happened just before it; pollers and streams fire on timers, so
// they land outside that window and never carry it.
export const USER_ACTION_HEADER = 'X-Vigil-User-Action'

const WINDOW_MS = 5_000
let lastInput = 0

if (typeof document !== 'undefined') {
  const note = () => {
    lastInput = Date.now()
  }
  document.addEventListener('pointerdown', note, { capture: true, passive: true })
  document.addEventListener('keydown', note, { capture: true, passive: true })
  window.addEventListener('popstate', note)
}

export const justActed = () => Date.now() - lastInput <= WINDOW_MS
