import { configApi } from '../../services/api'

/**
 * Epic decision 20: a server-set DEMO_MODE=false cannot be overridden from setup.
 * Resolves 'environment' (nothing changed) or 'on'; rejects when the API fails.
 */
export async function turnOnDemoMode(): Promise<'environment' | 'on'> {
  const { data } = await configApi.getDemoMode()
  if (data?.source === 'environment' && !data.enabled) return 'environment'
  await configApi.setDemoMode(true)
  return 'on'
}

export const DEMO_ENV_NOTICE = "Demo mode is set by the server's environment"
