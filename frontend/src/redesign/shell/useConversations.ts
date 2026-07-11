/* ============================================================
   Data hooks for persistent chat history — fetch via the shared
   axios client in services/api.ts (auth/CSRF/401-refresh included).
   useEffect + local state, matching the rest of the app (no
   React-Query anywhere yet — see screens/cases/useCases.ts).
   Mutations (rename/archive/delete/import) are called directly on
   conversationsApi from the component, then `reload()`.
   ============================================================ */
import { useCallback, useEffect, useState } from 'react'
import {
  conversationsApi,
  type ConversationSummary,
  type ConversationDetail,
} from '../../services/api'

export type Phase = 'loading' | 'ready' | 'error'

/** The current user's conversations, newest activity first. */
export function useConversations(includeArchived = false) {
  const [items, setItems] = useState<ConversationSummary[]>([])
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)
    conversationsApi
      .list({ archived: includeArchived })
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
  }, [includeArchived, reloadKey])

  return { items, phase, error, reload }
}

/** A single conversation with its ordered messages, or null until loaded. */
export function useConversation(id: string | null) {
  const [detail, setDetail] = useState<ConversationDetail | null>(null)
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    if (!id) {
      setDetail(null)
      setPhase('ready')
      return
    }
    let cancelled = false
    setPhase('loading')
    setError(null)
    conversationsApi
      .get(id)
      .then((res) => {
        if (cancelled) return
        setDetail(res.data as ConversationDetail)
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setError(
          (e as { message?: string })?.message || 'Failed to load conversation',
        )
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [id, reloadKey])

  return { detail, phase, error, reload }
}
