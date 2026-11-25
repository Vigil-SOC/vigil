/* Where a provider's calls go: the console's one set of labels for the class
   the server works out (core/llm/residency.py). The console never classifies a
   provider itself, and nothing here says "data stays on site": an address is
   not a self-hosted statement. */
import type { Schema } from '../services/apiTypes'
import type { IconName } from './icons'

export type Residency = Schema<'ProviderResidency'>
export type ResidencyClass = Residency['class']

/** The providers behind "Your cloud account". The server's YOUR_CLOUD_TYPES is the same set. */
export const CLOUD_PROVIDERS = ['bedrock', 'vertex', 'azure'] as const

export const RESIDENCY_LABEL: Record<Exclude<ResidencyClass, 'unknown'>, string> = {
  local_by_address: 'Local by address, not confirmed',
  your_cloud: 'Your cloud account',
  hosted: 'Hosted: data leaves your site',
}

export const RESIDENCY_ICON: Record<ResidencyClass, IconName> = {
  local_by_address: 'lock',
  your_cloud: 'cloud',
  hosted: 'globe',
  unknown: 'alert',
}

const GATEWAY_UNREAD = "The gateway couldn't be read."

/** A class with no residency to read (the gateway's routability call failed) is unknown, never local. */
export const residencyOf = (r: Residency | null | undefined): Residency =>
  r ?? { class: 'unknown', address_known: false, reason: GATEWAY_UNREAD }

/** Who stated a provider type self-hosted, and when (a held Provider Statement). */
export interface SelfHostedBy {
  by: string
  at: string
}

/** "Not known: the gateway couldn't be read" — the server's reason, as a label.
 *  A held self-hosted statement replaces the class: an address alone never says it. */
export function residencyLabel(r: Residency | null | undefined, selfHosted?: SelfHostedBy | null): string {
  const { class: cls, reason } = residencyOf(r)
  // non-breaking hyphens: the date never wraps in half
  if (selfHosted && cls !== 'unknown')
    return `Self-hosted, stated by ${selfHosted.by} on ${selfHosted.at.slice(0, 10).replace(/-/g, '\u2011')}`
  if (cls !== 'unknown') return RESIDENCY_LABEL[cls]
  const why = (reason || GATEWAY_UNREAD).replace(/\.$/, '')
  return `Not known: ${why.charAt(0).toLowerCase()}${why.slice(1)}`
}
