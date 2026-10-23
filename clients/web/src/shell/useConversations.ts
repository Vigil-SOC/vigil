import { useCallback, useEffect, useRef, useState } from 'react'
import {
  conversationsApi,
  type ConversationSummary,
} from '../services/api'
import { serverUnreachable } from './chatCache'

export type Phase = 'loading' | 'ready' | 'error'

/** The current user's conversations, newest activity first. `query` is `q`. */
export function useConversations(includeArchived = false, query = '', userId: string | null = null) {
  const [items, setItems] = useState<ConversationSummary[]>([])
  const [phase, setPhase] = useState<Phase>('loading')
  const [offline, setOffline] = useState(false)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])
  const [q, setQ] = useState(query)
  const seenUser = useRef(userId)

  // A different account must not keep the previous list on screen while its own loads.
  useEffect(() => {
    if (seenUser.current === userId) return
    seenUser.current = userId
    setItems([])
    setOffline(false)
    setPhase('loading')
  }, [userId])

  useEffect(() => {
    const timer = setTimeout(() => {
      setQ((current) => (current === query ? current : query))
    }, 200)
    return () => clearTimeout(timer)
  }, [query])

  useEffect(() => {
    let cancelled = false
    const needle = q.trim()
    conversationsApi
      .list({ archived: includeArchived, q: needle || undefined })
      .then((res) => {
        if (cancelled) return
        setItems((res.data?.conversations || []) as ConversationSummary[])
        setOffline(false)
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setOffline(serverUnreachable(e))
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [includeArchived, q, reloadKey, userId])

  return { items, phase, reload, offline }
}

