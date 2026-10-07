import { budgetsApi, type BudgetQuotaResponse, type BudgetSettings } from '../../services/api'
import { bifrostApi, type BifrostVirtualKey } from '../../services/bifrostApi'

/* The monthly ceiling lives on the default virtual key's budget in Bifrost.
   The budget endpoint's `default_vk` is the key's secret, and the key list
   returns values masked, so the key can never be found by matching values.
   The quota endpoint names the key instead: resolve by that name. */

/** The one place reset periods are normalised: quota says "1mo", the key list says "monthly". */
export function normalizeResetPeriod(raw: string | undefined | null): string {
  const p = (raw ?? '').trim().toLowerCase()
  if (p === '1mo' || p === 'monthly' || p === 'month' || p === 'mo') return 'monthly'
  if (p === '1d' || p === 'daily' || p === 'day') return 'daily'
  if (p === '1w' || p === 'weekly' || p === 'week') return 'weekly'
  if (p === '1h' || p === 'hourly' || p === 'hour') return 'hourly'
  return p || 'monthly'
}

export type CeilingState =
  | { kind: 'loading' }
  | { kind: 'budget-error' }
  | { kind: 'resolve-error' }
  | { kind: 'later' }
  | {
      kind: 'ready'
      key: BifrostVirtualKey
      currentLimit: number
      /** normalised period for display */
      period: string
      /** the reset duration exactly as the key list returns it, preserved on save */
      resetDuration: string
    }

/** Pure resolution from the three reads, so the rules are testable without a gateway. */
export function resolveCeiling(
  budget: BudgetSettings,
  quota: BudgetQuotaResponse | null,
  vks: BifrostVirtualKey[],
): CeilingState {
  if (!budget?.default_vk) return { kind: 'later' }
  const name = quota?.quota?.virtual_key_name
  if (!quota?.configured || quota.available === false || !name) return { kind: 'later' }
  const matches = vks.filter((vk) => vk.name === name)
  if (matches.length !== 1) return { kind: 'later' }
  const key = matches[0]
  const tier = quota.quota?.budgets?.[0]
  return {
    kind: 'ready',
    key,
    currentLimit: tier?.max_limit ?? key.budget?.max_limit ?? 0,
    period: normalizeResetPeriod(key.budget?.reset_duration ?? tier?.reset_duration),
    resetDuration: key.budget?.reset_duration ?? normalizeResetPeriod(tier?.reset_duration),
  }
}

export async function fetchCeiling(): Promise<CeilingState> {
  let budget: BudgetSettings
  try {
    budget = (await budgetsApi.get()).data
  } catch {
    return { kind: 'budget-error' }
  }
  if (!budget?.default_vk) return { kind: 'later' }
  try {
    const [quotaRes, vkRes] = await Promise.all([budgetsApi.getQuota(), bifrostApi.listVirtualKeys()])
    return resolveCeiling(budget, quotaRes.data, vkRes.data.virtual_keys || [])
  } catch {
    return { kind: 'resolve-error' }
  }
}
