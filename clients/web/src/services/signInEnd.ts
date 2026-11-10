// A request the server refused because its sign-in ended (the refresh failed too).
// The server names the reason in this header; the console shows the signed-out state.
export const END_REASON_HEADER = 'x-sign-in-end-reason'

type Listener = (reason: string | null) => void
const listeners = new Set<Listener>()

export function onSignInEnded(fn: Listener): () => void {
  listeners.add(fn)
  return () => {
    listeners.delete(fn)
  }
}

/** The end reason a failed request carries, if the server sent one. */
export function endReasonOf(error: unknown): string | null {
  const headers = (error as { response?: { headers?: Record<string, unknown> } } | null)?.response?.headers
  const value = headers?.[END_REASON_HEADER]
  return typeof value === 'string' && value ? value : null
}

export function reportSignInEnded(reason: string | null) {
  listeners.forEach((fn) => fn(reason))
}

// Facts only: what ended the sign-in, not what it means for anyone.
const REASON_TEXT: Record<string, string> = {
  idle: 'You were signed out because the sign-in sat idle past its limit.',
  length_limit: 'You were signed out because the sign-in reached its length limit.',
  expiry: 'You were signed out because the sign-in ran out.',
  sign_in_limit: 'You were signed out because a newer sign-in took its place under the account’s sign-in limit.',
  account_changed: 'You were signed out because your role, password, email or sign-in methods changed.',
  ended_by_person: 'You were signed out because this sign-in was ended from the sign-in list.',
  ended_by_admin: 'You were signed out because an admin ended this sign-in.',
  everyone_signed_out: 'You were signed out because every sign-in was ended.',
  deactivated: 'You were signed out because the account was deactivated or deleted.',
  token_reuse: 'You were signed out because a sign-in token was used twice.',
  sign_out: 'You signed out from another tab or device.',
}

export function endReasonText(reason: string | null): string {
  return (reason && REASON_TEXT[reason]) || 'Your sign-in has ended.'
}
