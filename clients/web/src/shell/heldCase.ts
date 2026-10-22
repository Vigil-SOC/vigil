export function heldCase(state: unknown): string | null {
  const held = (state as { drawerCase?: unknown } | null)?.drawerCase
  return typeof held === 'string' ? held : null
}

export const holdCase = (state: unknown, id: string | null) => ({ ...((state as object | null) ?? {}), drawerCase: id })
