/** Offline copy of one user's chats.

Keys include the user id. The old unscoped `soc.chat.history`, `soc.chat.keymap`
and `soc.chat.imported` entries are not attributable to anyone, so they are
deleted rather than shown. `soc.chat.threadHeight` and the dock
width are layout preferences and stay.
*/

export const CHAT_HISTORY_MAX = 30
const HISTORY = 'soc.chat.history'
const KEYMAP = 'soc.chat.keymap'
const IMPORTED = 'soc.chat.imported'

export interface CachedConversation {
  id: string
  title: string
  ts: number
  messages: Array<{ role: string; text: string; ms?: number }>
  key?: string
}

function usable(userId: string | null | undefined): userId is string {
  return typeof userId === 'string' && userId.length > 0
}

function scoped(prefix: string, userId: string): string {
  return `${prefix}.${userId}`
}

function isContentKey(key: string): boolean {
  return (
    key === HISTORY ||
    key.startsWith(`${HISTORY}.`) ||
    key === KEYMAP ||
    key.startsWith(`${KEYMAP}.`) ||
    key === IMPORTED ||
    key.startsWith(`${IMPORTED}.`)
  )
}

function contentKeys(): string[] {
  const keys: string[] = []
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i)
    if (key && isContentKey(key)) keys.push(key)
  }
  return keys
}

/** A missing response or a 5xx. A 4xx is the server answering, so the cache must not fill in. */
export function serverUnreachable(error: unknown): boolean {
  const status = (error as { response?: { status?: unknown } } | null)?.response?.status
  return typeof status !== 'number' || status >= 500
}

export function loadHistory<T = CachedConversation>(userId: string | null | undefined): T[] {
  if (!usable(userId)) return []
  try {
    const raw = JSON.parse(localStorage.getItem(scoped(HISTORY, userId)) || '[]')
    return Array.isArray(raw) ? (raw as T[]) : []
  } catch {
    return []
  }
}

export function saveHistory(userId: string | null | undefined, list: CachedConversation[]) {
  if (!usable(userId)) return
  try {
    localStorage.setItem(scoped(HISTORY, userId), JSON.stringify(list.slice(0, CHAT_HISTORY_MAX)))
  } catch {
    /* empty */
  }
}

export function loadKeymap(userId: string | null | undefined): Record<string, string> {
  if (!usable(userId)) return {}
  try {
    const raw = JSON.parse(localStorage.getItem(scoped(KEYMAP, userId)) || '{}')
    return raw && typeof raw === 'object' ? (raw as Record<string, string>) : {}
  } catch {
    return {}
  }
}

export function setKeymapEntry(userId: string | null | undefined, key: string, sid: string) {
  if (!usable(userId)) return
  try {
    const map = loadKeymap(userId)
    map[key] = sid
    localStorage.setItem(scoped(KEYMAP, userId), JSON.stringify(map))
  } catch {
    /* ignore */
  }
}

/** Drop every other user's copy, and the unscoped keys. */
export function retainChatCache(userId: string) {
  if (!usable(userId)) return
  const keep = new Set([scoped(HISTORY, userId), scoped(KEYMAP, userId)])
  for (const key of contentKeys()) {
    if (!keep.has(key)) localStorage.removeItem(key)
  }
}

/** Sign-out: nothing cached should remain for the next person at this browser. */
export function clearChatCache() {
  for (const key of contentKeys()) localStorage.removeItem(key)
}
