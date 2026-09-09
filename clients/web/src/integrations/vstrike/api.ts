import api from '../../services/api'

export interface VStrikeFault {
  event_id: string
  occurred_at: string | null
  label: string
  description: string
  source_level: string
  ip4s: string[]
  measurement: Record<string, string>
}

const root = '/integrations/vstrike'
export const vstrikeApi = {
  connect: () => api.post<{ iframe_url: string }>(`${root}/ui/iframe-token`),
  networks: () => api.get<{ networks: unknown[] }>(`${root}/ui/networks`),
  loadNetwork: (networkId: string) => api.post(`${root}/ui/load-network`, { network_id: networkId }),
  storylines: (networkId: string) => api.get<{ storylines: unknown[] }>(`${root}/storylines`, { params: { network_id: networkId } }),
  applyStoryline: (networkId: string, storylineId: string) => api.post(`${root}/ui/storyline-apply`, { network_id: networkId, storyline_id: storylineId }),
  step: (direction: 'forward' | 'backward') => api.post(`${root}/ui/storyline-${direction}`),
  focus: (networkId: string, ip4s: string[]) => api.post(`${root}/ui/find-by-ip-then-zoom`, { network_id: networkId, ip4s }),
  faults: (storylineId: string) => api.post<{ faults: VStrikeFault[]; fetched_at: string; truncated: boolean; limit: number }>(`${root}/faults/query`, { storyline_id: storylineId, limit: 100 }),
}

export interface VStrikeOption { id: string; label: string }
export function options(values: unknown, idKeys: string[]): VStrikeOption[] {
  if (!Array.isArray(values)) return []
  const unique = new Map<string, VStrikeOption>()
  for (const item of values) {
    if (!item || typeof item !== 'object') continue
    const raw = item as Record<string, unknown>
    const id = idKeys.map((key) => raw[key]).find((value) => typeof value === 'string' && value.trim())
    if (typeof id !== 'string' || unique.has(id)) continue
    const label = [raw.name, raw.label, raw.title, raw.description].find((value) => typeof value === 'string' && value.trim())
    unique.set(id, { id, label: typeof label === 'string' ? label : id })
  }
  return [...unique.values()]
}

/** Don't surface raw provider errors: they may carry token URLs. */
export function actionError(action: string): string {
  return `Could not ${action}. Check VStrike configuration or reconnect and try again.`
}
