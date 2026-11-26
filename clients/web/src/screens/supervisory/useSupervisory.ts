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

  const load = useCallback(() => {
    const mine = ++gen.current
    setError(null)
    fetchRead().then(
      (read) => { if (mine === gen.current) setData(read) },
      (e: unknown) => { if (mine === gen.current) setError(e instanceof Error ? e.message : 'network') },
    )
  }, [])
  useEffect(load, [load])

  /** Moves the suggestion into history with the reason given. A refused dismiss reads the page again, so the card shows as the server has it. */
  const dismiss = useCallback((id: string, reason: DismissReason) => {
    postDismiss(id, reason).then(
      (entry) => setData((cur) => cur && { ...cur, suggestions: cur.suggestions.filter((x) => x.id !== id), history: [entry, ...cur.history] }),
      () => load(),
    )
  }, [load])

  /** On success the change heads History with an Undo; the card stays until `settle`. Throws on failure. */
  const apply = useCallback(async (s: Suggestion) => {
    const entry = await postApply(s)
    setData((cur) => cur && withValue({ ...cur, history: [entry, ...cur.history] }, s.key, s.to))
    return entry
  }, [])

  /** The applied suggestion leaves the open list, and the page reads the server's values again. */
  const settle = useCallback((id: string) => {
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
