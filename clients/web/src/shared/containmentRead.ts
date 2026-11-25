import { useAuth } from '../contexts/AuthContext'

/** One vendor record an isolation or account disable resolved to (#5059). */
export interface ContainmentCandidate {
  id: string
  vendor: string
  tool?: string
  vendor_id: string | number
  name: string
  state: string
}

export interface Containment {
  candidates: ContainmentCandidate[]
  missing: string | null
}

/** The stored resolution on a needs-you item or an action's parameters, when there is one. */
export function containmentOf(raw: unknown): Containment | null {
  if (!raw || typeof raw !== 'object') return null
  const stored = raw as { candidates?: unknown; missing?: unknown }
  const candidates = Array.isArray(stored.candidates) ? (stored.candidates as ContainmentCandidate[]) : []
  return { candidates, missing: typeof stored.missing === 'string' && stored.missing ? stored.missing : null }
}

const VENDOR: Record<string, string> = {
  'microsoft-defender': 'Microsoft Defender',
  'carbon-black': 'Carbon Black',
  'azure-ad': 'Entra ID',
}
export const vendorName = (vendor: string | null | undefined) => (vendor ? VENDOR[vendor] ?? vendor : 'The vendor')

/** A candidate's current state, as the vendor reported it at lookup. */
export const CURRENT: Record<string, string> = {
  contained: 'already isolated',
  not_contained: 'not isolated',
  requested: 'isolation pending',
  disabled: 'already disabled',
  enabled: 'enabled',
}

/** An account's states are enabled or disabled; a host's are not. */
const isAccount = (c: ContainmentCandidate) => c.state === 'enabled' || c.state === 'disabled'

/** The signed-in person's own account: shown, not refused. */
export function useOwnAccount(): (c: ContainmentCandidate) => boolean {
  const { user } = useAuth()
  const mine = [user?.username, user?.email].filter(Boolean).map((one) => one!.toLowerCase())
  return (c) => isAccount(c) && mine.includes(String(c.name).toLowerCase())
}

/** Whether Approve can send: something resolved, and one is picked when there are several. */
export function canApprove(containment: Containment | null, picked: string | null): boolean {
  if (!containment) return true
  if (containment.candidates.length === 0) return false
  return containment.candidates.length === 1 || picked !== null
}

/** The candidate Approve sends: the pick, or the only one. */
export function chosenCandidate(containment: Containment | null, picked: string | null): string | undefined {
  if (!containment) return undefined
  return picked ?? (containment.candidates.length === 1 ? containment.candidates[0].id : undefined)
}

