import { llmProviderApi, type LLMProvider } from '../services/api'
import { anyRoutableBifrostProvider } from '../services/bifrostApi'

// A legacy active+default provider, or a routable Bifrost provider. A failed
// read is a failure only when the other side did not already prove a provider
// is ready. The old catch that treated a provider-list error as configured
// existed so the hard gate would not trap a working install.
export const isProviderReady = (provider: LLMProvider): boolean =>
  provider.is_active && provider.is_default

export type ProviderStatus = 'ready' | 'none' | 'unreachable'

/** Only a failed routability read is `unreachable`: a failed legacy list with
    a healthy gateway is still a gateway that answered. */
export async function readProviderStatus(): Promise<ProviderStatus> {
  const [legacy, bifrost] = await Promise.allSettled([
    llmProviderApi.list().then((res) => (res.data || []).some(isProviderReady)),
    anyRoutableBifrostProvider(),
  ])
  const legacyReady = legacy.status === 'fulfilled' && legacy.value
  const bifrostReady = bifrost.status === 'fulfilled' && bifrost.value
  if (legacyReady || bifrostReady) return 'ready'
  return bifrost.status === 'rejected' ? 'unreachable' : 'none'
}
