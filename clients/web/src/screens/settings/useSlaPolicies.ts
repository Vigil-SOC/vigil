import { useCallback, useEffect, useState } from 'react'
import { slaPoliciesApi } from '../../services/api'

export type Phase = 'loading' | 'ready' | 'error'
export type PriorityLevel = 'critical' | 'high' | 'medium' | 'low'

export interface SlaPolicy {
  policy_id: string
  name: string
  description?: string | null
  priority_level: PriorityLevel | string
  response_time_hours: number
  resolution_time_hours: number
  business_hours_only?: boolean
  notification_thresholds?: number[]
  is_active?: boolean
  is_default?: boolean
}

export interface SlaPolicyCreate {
  policy_id: string
  name: string
  description?: string
  priority_level: string
  response_time_hours: number
  resolution_time_hours: number
  business_hours_only?: boolean
  is_active?: boolean
  is_default?: boolean
}
// notification_thresholds is stored but nothing acts on it, so it is never written from here
export type SlaPolicyUpdate = Partial<Omit<SlaPolicyCreate, 'policy_id' | 'priority_level'>>

/** Case counts for one policy in the window the table reports on. */
export interface SlaUsage {
  total_cases: number
  breached_cases: number
  compliance_rate: number
}

/** Start of the current calendar month, UTC. */
export const monthStartUtc = (now = new Date()) =>
  new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1)).toISOString()

export function useSlaPolicies() {
  // undefined while loading; null when that policy's usage could not be read
  const [usage, setUsage] = useState<Record<string, SlaUsage | null | undefined>>({})
  const [policies, setPolicies] = useState<SlaPolicy[]>([])
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)
    slaPoliciesApi
      .getAll()
      .then((res) => {
        if (cancelled) return
        const data = res.data
        // tolerate both a bare array and a { policies: [...] } envelope
        const list = (Array.isArray(data) ? data : data?.policies || []) as SlaPolicy[]
        setPolicies(list)
        setUsage({})
        setPhase('ready')
        const since = monthStartUtc()
        list.forEach((p) => {
          slaPoliciesApi
            .getUsage(p.policy_id, { since })
            .then((u) => !cancelled && setUsage((prev) => ({ ...prev, [p.policy_id]: u.data as SlaUsage })))
            .catch(() => !cancelled && setUsage((prev) => ({ ...prev, [p.policy_id]: null })))
        })
      })
      .catch((e) => {
        if (cancelled) return
        setError((e as { message?: string })?.message || 'Failed to load SLA policies')
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  const create = useCallback(
    async (data: SlaPolicyCreate) => { await slaPoliciesApi.create(data); reload() },
    [reload],
  )
  const update = useCallback(
    async (id: string, data: SlaPolicyUpdate) => { await slaPoliciesApi.update(id, data); reload() },
    [reload],
  )
  const remove = useCallback(
    async (id: string) => { await slaPoliciesApi.delete(id); reload() },
    [reload],
  )

  return { policies, usage, phase, error, reload, create, update, remove }
}
