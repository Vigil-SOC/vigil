import { useCallback, useEffect, useState } from 'react'
import type { AxiosResponse } from 'axios'

export const POLL_MS = 15_000

export type Phase = 'loading' | 'error' | 'ready'

/** One read, polled; a failed poll keeps the last good answer on screen. */
export function usePoll<T>(read: () => Promise<AxiosResponse<T>>, ms = POLL_MS) {
  const [data, setData] = useState<T | null>(null)
  const [phase, setPhase] = useState<Phase>('loading')
  const load = useCallback(() => {
    read()
      .then((res) => {
        setData(res.data)
        setPhase('ready')
      })
      .catch(() => setPhase((current) => (current === 'ready' ? 'ready' : 'error')))
  }, [read])
  useEffect(() => {
    load()
    const id = setInterval(load, ms)
    return () => clearInterval(id)
  }, [load, ms])
  return { data, phase, reload: load }
}

export const money = (n: number | null | undefined) => (n == null ? '—' : `$${n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`)
export const pct = (n: number | null | undefined) => (n == null ? '—' : `${Math.round(n * 100)}%`)
export const count = (n: number | null | undefined) => (n == null ? '—' : n.toLocaleString('en-US'))
export const hhmm = (iso: string | null | undefined) => (iso ? iso.slice(11, 16) : '—')

/** "4 min", "1 h 10 min", "3 d 2 h": the console's duration words. */
export function minutes(total: number | null | undefined): string {
  if (total == null) return '—'
  const m = Math.round(total)
  if (m < 60) return `${m} min`
  const h = Math.floor(m / 60)
  if (h < 24) return m % 60 ? `${h} h ${m % 60} min` : `${h} h`
  const d = Math.floor(h / 24)
  return h % 24 ? `${d} d ${h % 24} h` : `${d} d`
}

