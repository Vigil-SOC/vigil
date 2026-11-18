import { basePath } from '../config/basePath'
import type { MfaMethod } from '../shared/MfaMethodsList'

// A method change for a person who signs in only through SSO: confirming at the provider
// stands in for the password (#4756). The callback comes back to this page with a short proof.

const STEP_KEY = 'vigil.twoStep.return'
const MARK = 'sso_method'
const PROOF = 'sso_proof'

/** The dialog step to reopen on the way back. */
export type ReturnStep = {
  view: string
  keyName?: string
  target?: MfaMethod | null
  justAdded?: 'app' | 'key' | null
}

export function confirmAtProvider(step: ReturnStep) {
  sessionStorage.setItem(STEP_KEY, JSON.stringify(step))
  // a plain navigation: the server answers with the provider's redirect, and the callback returns here
  const back = window.location.pathname + window.location.search
  window.location.assign(`${basePath}/api/auth/sso/start?method=1&confirm=${encodeURIComponent(back)}`)
}

type Return = { step: ReturnStep | null; proof: string | null }
// Read from the address once per page load, then kept: the dialog may mount several times
// while the console loads, and each mount must land on the same step.
let held: Return | null | undefined

/** Whether this page load is a return from the provider for a method change. */
export const returningFromProvider = () =>
  held ? true : held === undefined && new URLSearchParams(window.location.search).has(MARK)

/** The return: the step to reopen and the proof (null when the confirmation failed). On the
 * first read the marks leave the address (through ``replace``, the router's), so the proof isn't
 * kept in history or a bookmark. */
export function providerReturn(replace: (search: string) => void): Return | null {
  if (held !== undefined) return held
  const params = new URLSearchParams(window.location.search)
  if (!params.has(MARK)) return (held = null)
  const proof = params.get(MARK) === 'ok' ? params.get(PROOF) : null
  params.delete(MARK)
  params.delete(PROOF)
  const rest = params.toString()
  replace(rest ? `?${rest}` : '')
  const raw = sessionStorage.getItem(STEP_KEY)
  sessionStorage.removeItem(STEP_KEY)
  let step: ReturnStep | null
  try {
    step = raw ? (JSON.parse(raw) as ReturnStep) : null
  } catch {
    step = null
  }
  return (held = { step, proof })
}

/** The dialog closed: the return is spent. */
export function dropProviderReturn() {
  if (held) held = null
}

/** The server's refusal of a missing or stale proof: confirm at the provider again. */
export const proofRefused = (message: string) => message.includes('at your SSO provider')

/** Whether a proof is still inside its five minutes (the server is the judge; this only hides a stale one). */
export function proofLive(proof: string | null): boolean {
  if (!proof) return false
  try {
    const { exp } = JSON.parse(atob(proof.split('.')[1].replace(/-/g, '+').replace(/_/g, '/'))) as { exp?: number }
    return typeof exp === 'number' && exp * 1000 > Date.now()
  } catch {
    return false
  }
}
