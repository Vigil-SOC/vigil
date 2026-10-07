import type { LLMProvider } from '../../services/api'
import { secretText, type BifrostKey } from '../../services/bifrostApi'
import { urlIsLoopback } from '../../shared/loopback'

export const DATA_STAYS_ON_SITE = 'data stays on site'
export const DATA_LEAVES_THE_SITE = 'data leaves the site'

export function residencyCopy(stays: boolean): string {
  return stays ? DATA_STAYS_ON_SITE : DATA_LEAVES_THE_SITE
}

export function bifrostStaysOnSite(name: string, keys: BifrostKey[]): boolean {
  if (name === 'ollama') return true
  return keys.some((key) => urlIsLoopback(secretText(key.ollama_key_config?.url)))
}

export function legacyStaysOnSite(provider: LLMProvider): boolean {
  if (provider.provider_type === 'ollama') return true
  return !!provider.base_url && urlIsLoopback(provider.base_url)
}
