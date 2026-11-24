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

/** "Not known: the gateway couldn't be read" — the server's reason, as a label. */
export function residencyLabel(r: Residency | null | undefined): string {
  const { class: cls, reason } = residencyOf(r)
  if (cls !== 'unknown') return RESIDENCY_LABEL[cls]
  const why = (reason || GATEWAY_UNREAD).replace(/\.$/, '')
  return `Not known: ${why.charAt(0).toLowerCase()}${why.slice(1)}`
}
