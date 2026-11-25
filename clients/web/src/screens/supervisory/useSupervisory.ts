import { useCallback, useEffect, useRef, useState } from 'react'
import api from '../../services/api'
import type { DismissReason, HistoryEntry, Suggestion, SupervisoryRead } from './supervisoryTypes'

/** GET /api/supervisory: everything the page reads, in one response. */
async function fetchRead(): Promise<SupervisoryRead> {
  const r = await api.get<SupervisoryRead>('/supervisory')
  if (!Array.isArray(r.data?.suggestions) || !r.data.status) throw new Error('not a supervisory read')
  return r.data
}

/** The settings panel with one value changed, until the next read brings the server's own. */
const withValue = (read: SupervisoryRead, key: string, value: string): SupervisoryRead => ({
  ...read,
  settings: read.settings?.map((x) => (x.key === key ? { ...x, value } : x)) ?? null,
})

const roundTrip = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

/** Stands in for POST /api/supervisory/suggestions/{id}/apply, which returns the new history entry. */
async function postApply(s: Suggestion): Promise<HistoryEntry> {
  await roundTrip(900)
  return { id: `h-${s.id}-${Date.now()}`, at: new Date().toISOString(), key: s.key, from: s.from, to: s.to, outcome: 'applied', note: '', undoable: true }
}

/** Stands in for POST /api/supervisory/history/{id}/undo. */
async function postUndo(): Promise<void> {
  await roundTrip(300)
}

export function useSupervisory() {
  const [data, setData] = useState<SupervisoryRead | null>(null)
  const [error, setError] = useState<string | null>(null)
  const gen = useRef(0)

  const load = useCallback(() => {
    const mine = ++gen.current
    setError(null)
    fetchRead().then(
      (read) => { if (mine === gen.current) setData(read) },
      (e: unknown) => { if (mine === gen.current) setError(e instanceof Error ? e.message : 'network') },
    )
  }, [])
  useEffect(load, [load])

  /** Moves the suggestion into history with the reason given. Local only until POST /api/supervisory/suggestions/{id}/dismiss exists. */
  const dismiss = useCallback((id: string, reason: DismissReason) => {
    setData((cur) => {
      if (!cur) return cur
      const s = cur.suggestions.find((x) => x.id === id)
      if (!s) return cur
      const entry: HistoryEntry = { id: `h-${id}-${Date.now()}`, at: new Date().toISOString(), key: s.key, from: s.from, to: s.to, outcome: 'dismissed', note: reason }
      return { ...cur, suggestions: cur.suggestions.filter((x) => x.id !== id), history: [entry, ...cur.history] }
    })
  }, [])

  /** On success the change heads History with an Undo; the card stays until `settle`. Throws on failure. */
  const apply = useCallback(async (s: Suggestion) => {
    const entry = await postApply(s)
    setData((cur) => cur && withValue({ ...cur, history: [entry, ...cur.history] }, s.key, s.to))
    return entry
  }, [])

  /** The applied suggestion leaves the open list. */
  const settle = useCallback((id: string) => {
    setData((cur) => cur && { ...cur, suggestions: cur.suggestions.filter((x) => x.id !== id) })
  }, [])

  /** Puts an applied value back; its history entry then reads Undone. Throws on failure. */
  const undo = useCallback(async (entry: HistoryEntry) => {
    await postUndo()
    const undone: HistoryEntry = { ...entry, outcome: 'undone', at: new Date().toISOString(), undoable: false }
    setData((cur) => cur && withValue({ ...cur, history: cur.history.map((h) => (h.id === entry.id ? undone : h)) }, entry.key, entry.from))
  }, [])

  return { data, error, reload: load, dismiss, apply, settle, undo }
}
