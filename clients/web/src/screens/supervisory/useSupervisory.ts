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

/** Each write returns the history entry it recorded. */
async function postApply(s: Suggestion): Promise<HistoryEntry> {
  return (await api.post<HistoryEntry>(`/supervisory/suggestions/${encodeURIComponent(s.id)}/apply`)).data
}

async function postUndo(entry: HistoryEntry): Promise<HistoryEntry> {
  return (await api.post<HistoryEntry>(`/supervisory/history/${encodeURIComponent(entry.id)}/undo`)).data
}

async function postDismiss(id: string, reason: DismissReason): Promise<HistoryEntry> {
  return (await api.post<HistoryEntry>(`/supervisory/suggestions/${encodeURIComponent(id)}/dismiss`, { reason })).data
}

export function useSupervisory() {
  const [data, setData] = useState<SupervisoryRead | null>(null)
  const [error, setError] = useState<string | null>(null)
  const gen = useRef(0)
  // Applied suggestions still in their undo window. The server no longer lists
  // them as open, so a read that lands meanwhile keeps them until `settle`.
  const pinned = useRef(new Map<string, Suggestion>())

  const load = useCallback(() => {
    const mine = ++gen.current
    setError(null)
    fetchRead().then(
      (read) => {
        if (mine !== gen.current) return
        setData((cur) => {
          const kept = [...pinned.current.values()].filter((p) => !read.suggestions.some((x) => x.id === p.id))
          if (!kept.length) return read
          // each kept card goes back where it was, so the list does not jump
          const suggestions = [...read.suggestions]
          for (const p of kept) {
            const at = cur?.suggestions.findIndex((x) => x.id === p.id) ?? -1
            suggestions.splice(at < 0 ? 0 : Math.min(at, suggestions.length), 0, p)
          }
          return { ...read, suggestions }
        })
      },
      (e: unknown) => { if (mine === gen.current) setError(e instanceof Error ? e.message : 'network') },
    )
  }, [])
  useEffect(load, [load])

  /** Moves the suggestion into history with the reason given. Throws on failure, after reading the page again so the card shows as the server has it. */
  const dismiss = useCallback(async (id: string, reason: DismissReason) => {
    let entry: HistoryEntry
    try {
      entry = await postDismiss(id, reason)
    } catch (e) {
      load()
      throw e
    }
    setData((cur) => cur && { ...cur, suggestions: cur.suggestions.filter((x) => x.id !== id), history: [entry, ...cur.history] })
  }, [load])

  /** On success the change heads History with an Undo; the card stays until `settle`. Throws on failure. */
  const apply = useCallback(async (s: Suggestion) => {
    let entry: HistoryEntry
    try {
      entry = await postApply(s)
    } catch (e) {
      // Whether or not the write landed, the page shows the setting as the server has it.
      load()
      throw e
    }
    pinned.current.set(s.id, s)
    setData((cur) => cur && withValue({ ...cur, history: [entry, ...cur.history] }, s.key, s.to))
    return entry
  }, [load])

  /** The applied suggestion leaves the open list, and the page reads the server's values again. */
  const settle = useCallback((id: string) => {
    pinned.current.delete(id)
    setData((cur) => cur && { ...cur, suggestions: cur.suggestions.filter((x) => x.id !== id) })
    load()
  }, [load])

  /** Puts an applied value back: the applied entry can no longer be undone and an Undone entry heads History. Throws on failure. */
  const undo = useCallback(async (entry: HistoryEntry) => {
    const undone = await postUndo(entry)
    setData((cur) => cur && withValue({
      ...cur,
      history: [undone, ...cur.history.map((h) => (h.id === entry.id ? { ...h, undoable: false } : h))],
    }, entry.key, entry.from))
  }, [])

  return { data, error, reload: load, dismiss, apply, settle, undo }
}
