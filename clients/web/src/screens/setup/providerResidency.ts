import type { LLMProvider } from '../../services/api'
import { secretText, type BifrostKey } from '../../services/bifrostApi'
import { urlIsLoopback } from '../../shared/loopback'

export const DATA_STAYS_ON_SITE = 'Local: data stays on site'
export const DATA_STAYS_IN_CLOUD = 'Data stays in your cloud'
export const DATA_LEAVES_THE_SITE = 'Hosted: data leaves your site'

/** The providers behind "Your cloud account": the models run in the customer's own cloud. */
export const CLOUD_PROVIDERS = ['bedrock', 'vertex', 'azure'] as const

export function residencyCopy(stays: boolean, inCloud = false): string {
  if (inCloud) return DATA_STAYS_IN_CLOUD
  return stays ? DATA_STAYS_ON_SITE : DATA_LEAVES_THE_SITE
}

export function bifrostStaysOnSite(name: string, keys: BifrostKey[]): boolean {
  if (name === 'ollama') return true
  return keys.some((key) => urlIsLoopback(secretText(key.ollama_key_config?.url)))
}

export function bifrostStaysInCloud(name: string): boolean {
  return (CLOUD_PROVIDERS as readonly string[]).includes(name)
}

export function legacyStaysOnSite(provider: LLMProvider): boolean {
  if (provider.provider_type === 'ollama') return true
  return !!provider.base_url && urlIsLoopback(provider.base_url)
}
