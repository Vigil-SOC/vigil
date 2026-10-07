import { useCallback, useEffect, useState } from 'react'
import { exclusionsApi, type IpExclusion } from '../../services/api'

export function apiError(e: unknown, fallback: string): string {
  const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (typeof detail === 'string') return detail
  return (e as { message?: string })?.message || fallback
}

/** active exclusions, and on request the removed ones beside them */
export function useExclusions(refreshKey = 0) {
  const [rows, setRows] = useState<IpExclusion[]>([])
  const [hiddenTotal, setHiddenTotal] = useState(0)
  const [history, setHistory] = useState(false)
  const [phase, setPhase] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    exclusionsApi
      .list(history)
      .then((res) => {
        if (cancelled) return
        setRows(res.data?.exclusions ?? [])
        setHiddenTotal(res.data?.hidden_findings_total ?? 0)
        setPhase('ready')
        setError(null)
      })
      .catch((e) => {
        if (cancelled) return
        setError(apiError(e, 'Failed to load exclusions'))
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [history, reloadKey, refreshKey])

  return { rows, hiddenTotal, phase, error, reload, history, setHistory }
}
