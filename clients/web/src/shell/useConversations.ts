import { useCallback, useEffect, useState } from 'react'
import {
  conversationsApi,
  type ConversationSummary,
} from '../services/api'

export type Phase = 'loading' | 'ready' | 'error'

/** The current user's conversations, newest activity first. `query` is `q`. */
export function useConversations(includeArchived = false, query = '') {
  const [items, setItems] = useState<ConversationSummary[]>([])
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])
  const [q, setQ] = useState(query)

  useEffect(() => {
    const timer = setTimeout(() => {
      setQ((current) => (current === query ? current : query))
    }, 200)
    return () => clearTimeout(timer)
  }, [query])

  useEffect(() => {
    let cancelled = false
    setError(null)
    const needle = q.trim()
    conversationsApi
      .list({ archived: includeArchived, q: needle || undefined })
      .then((res) => {
        if (cancelled) return
        setItems((res.data?.conversations || []) as ConversationSummary[])
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setError((e as { message?: string })?.message || 'Failed to load history')
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [includeArchived, q, reloadKey])

  return { items, phase, error, reload }
}

