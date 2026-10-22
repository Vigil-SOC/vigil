// The drawer is held on the history entry it is open over, so browser Back from a page it led to
// (Watch it run, Replay) comes back to the case, and Back to an entry it was never open over has none.
export function heldCase(state: unknown): string | null {
  const held = (state as { drawerCase?: unknown } | null)?.drawerCase
  return typeof held === 'string' ? held : null
}

export const holdCase = (state: unknown, id: string | null) => ({ ...((state as object | null) ?? {}), drawerCase: id })
