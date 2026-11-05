import { useCallback, useEffect, useRef, useState } from 'react'
import api from '../../services/api'
import type { DecisionRecord, FeedbackBody, IncidentDetail, IncidentPage, PackState, SensorList, StatusRead } from './medicTypes'

export type Phase = 'loading' | 'ready' | 'error'

const POLL_MS = 30_000
const PAGE = 100
/** Pages followed for one read: 100 × 50 is far past what one incident or list holds. */
const MAX_PAGES = 50

/** The proxy's problem+json `{status, code}`; the code, or '' for a network failure. */
export const problemCode = (e: unknown): string =>
  (e as { response?: { data?: { code?: unknown } } })?.response?.data?.code?.toString() ?? ''

/** Calls `fn` every 30 s while the page is visible, and once more when it becomes visible again. */
function useVisiblePoll(fn: () => void, on: boolean) {
  const latest = useRef(fn)
  latest.current = fn
  useEffect(() => {
    if (!on) return
    const tick = () => document.visibilityState === 'visible' && latest.current()
    const timer = window.setInterval(tick, POLL_MS)
    document.addEventListener('visibilitychange', tick)
    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', tick)
    }
  }, [on])
}

/** GET /api/medic/status: the state word, and Medic's own snapshot when it is Running. The last good read is kept. */
export function useStatus() {
  const [status, setStatus] = useState<StatusRead | null>(null)
  const [error, setError] = useState<string | null>(null)
  const gen = useRef(0)
  const load = useCallback(() => {
    const mine = ++gen.current
    api.get<StatusRead>('/medic/status').then(
      (r) => { if (mine === gen.current) { setStatus(r.data); setError(null) } },
      (e) => { if (mine === gen.current) setError(problemCode(e) || 'network') },
    )
  }, [])
  useEffect(load, [load])
  useVisiblePoll(load, true)
  return { status, error, reload: load }
}

/** GET /api/medic/sensors: every sensor, not paginated. Polled while visible; a failed read keeps the last good list and `failedSince` says since when. */
export function useSensors(enabled: boolean) {
  const [list, setList] = useState<SensorList | null>(null)
  const [phase, setPhase] = useState<Phase>('loading')
  const [failedSince, setFailedSince] = useState<number | null>(null)
  const [lastGood, setLastGood] = useState<number | null>(null)
  const gen = useRef(0)
  const load = useCallback(() => {
    const mine = ++gen.current
    api.get<SensorList>('/medic/sensors').then((r) => {
      if (!Array.isArray(r.data?.items)) throw new Error('not a sensor list') // the proxy passes bytes through
      return r.data
    }).then(
      (data) => {
        if (mine !== gen.current) return
        setList(data)
        setPhase('ready')
        setFailedSince(null)
        setLastGood(Date.now())
      },
      () => {
        if (mine !== gen.current) return
        setFailedSince((t) => t ?? Date.now())
        setPhase((p) => (p === 'ready' ? p : 'error'))
      },
    )
  }, [])
  useEffect(() => { if (enabled) load() }, [enabled, load])
  useVisiblePoll(load, enabled)
  return { list, phase, failedSince, lastGood, reload: load }
}

export interface IncidentFilters {
  state: 'open' | 'resolved' | 'all'
  lane: '' | '1' | '2' | '3'
  unrated: boolean
}

/**
 * The incident list. Suppressed children come too (include_suppressed) so the screen can
 * nest them under their cause. A poll re-reads every page loaded so far; a failed read
 * keeps the last good list and `failedSince` says since when it has been stale.
 */
export function useIncidents(filters: IncidentFilters, enabled: boolean) {
  const [page, setPage] = useState<IncidentPage | null>(null)
  const [phase, setPhase] = useState<Phase>('loading')
  const [failedSince, setFailedSince] = useState<number | null>(null)
  const [lastGood, setLastGood] = useState<number | null>(null)
  const [more, setMore] = useState(false)
  const pages = useRef(1)
  const gen = useRef(0)
  const paging = useRef(false)
  const key = `${filters.state}|${filters.lane}|${filters.unrated}`

  const read = useCallback(async (upTo: number) => {
    const mine = ++gen.current
    try {
      let cursor: string | null = null
      let items: IncidentPage['items'] = []
      let counts: IncidentPage['counts'] | null = null
      for (let n = 0; n < upTo; n++) {
        const params: Record<string, string | number | boolean> = { state: filters.state, include_suppressed: true, limit: PAGE }
        if (filters.lane) params.lane = Number(filters.lane)
        if (filters.unrated) params.unrated = true
        if (cursor) params.cursor = cursor
        const { data }: { data: IncidentPage } = await api.get('/medic/incidents', { params })
        if (mine !== gen.current) return
        items = items.concat(data.items)
        counts = data.counts
        cursor = data.next_cursor
        if (!cursor) break
      }
      pages.current = upTo
      setPage({ items, next_cursor: cursor, counts: counts! })
      setPhase('ready')
      setFailedSince(null)
      setLastGood(Date.now())
    } catch {
      if (mine !== gen.current) return
      setFailedSince((t) => t ?? Date.now())
      setPhase((p) => (p === 'ready' ? p : 'error'))
    }
  }, [filters.state, filters.lane, filters.unrated])

  // a new filter starts over; the list on screen is for the old one until the first read lands
  useEffect(() => {
    if (!enabled) return
    pages.current = 1
    setPhase('loading')
    setPage(null)
    read(1)
  }, [key, enabled, read])
  useVisiblePoll(() => { if (!paging.current) read(pages.current) }, enabled)

  const loadMore = useCallback(() => {
    setMore(true)
    paging.current = true
    read(pages.current + 1).finally(() => { paging.current = false; setMore(false) })
  }, [read])

  return { page, phase, failedSince, lastGood, more, loadMore, reload: () => read(pages.current) }
}

/** One incident's summary and opened record, and every decision record about it, following the cursor to the end. */
export function useIncidentDetail(id: string, enabled: boolean) {
  const [detail, setDetail] = useState<IncidentDetail | null>(null)
  const [records, setRecords] = useState<DecisionRecord[]>([])
  const [phase, setPhase] = useState<Phase>('loading')
  const [code, setCode] = useState('')
  const gen = useRef(0)

  const read = useCallback(async () => {
    const mine = ++gen.current
    try {
      const { data } = await api.get<IncidentDetail>(`/medic/incidents/${encodeURIComponent(id)}`)
      let all: DecisionRecord[] = []
      let cursor: string | null = null
      for (let n = 0; n < MAX_PAGES; n++) {
        const params: Record<string, string | number> = { incident_id: id, limit: PAGE }
        if (cursor) params.cursor = cursor
        const { data: p }: { data: { items: DecisionRecord[]; next_cursor: string | null } } = await api.get('/medic/decisions', { params })
        all = all.concat(p.items)
        cursor = p.next_cursor
        if (!cursor) break
      }
      if (mine !== gen.current) return
      setDetail(data)
      setRecords(all)
      setPhase('ready')
      setCode('')
    } catch (e) {
      if (mine !== gen.current) return
      setCode(problemCode(e))
      if (problemCode(e) === 'not_found') setDetail(null)
      setPhase((p) => (p === 'ready' && problemCode(e) !== 'not_found' ? p : 'error'))
    }
  }, [id])

  useEffect(() => {
    if (!enabled) return
    setPhase('loading')
    setDetail(null)
    setRecords([])
    read()
  }, [read, enabled])
  useVisiblePoll(read, enabled)
  return { detail, records, phase, code, reload: read }
}

/** POST /api/medic/incidents/{id}/feedback: the stored record. The admin is the session's, so the body names no user. */
export const postFeedback = (id: string, body: FeedbackBody) =>
  api.post<{ record: DecisionRecord }>(`/medic/incidents/${encodeURIComponent(id)}/feedback`, body).then((r) => r.data.record)

/** Medic's problem+json for a pack read or write: the code, and for a refusal the `pack_code` that says which check. */
export const packProblem = (e: unknown): { status: number; code: string; packCode: string } => {
  const r = (e as { response?: { status?: number; data?: { code?: unknown; pack_code?: unknown } } })?.response
  return { status: r?.status ?? 0, code: r?.data?.code?.toString() ?? '', packCode: r?.data?.pack_code?.toString() ?? '' }
}

/** GET /api/medic/pack. `unavailable`: Medic has no pack lifecycle (dev mode). A failed read keeps the last good facts. */
export function usePack(enabled: boolean) {
  const [pack, setPack] = useState<PackState | null>(null)
  const [phase, setPhase] = useState<Phase>('loading')
  const [unavailable, setUnavailable] = useState(false)
  const gen = useRef(0)
  const load = useCallback(async (): Promise<PackState | null> => {
    const mine = ++gen.current
    try {
      const { data } = await api.get<PackState>('/medic/pack')
      if (mine !== gen.current) return null
      if (!data || typeof data !== 'object' || !Array.isArray(data.withdrawn)) throw new Error('not a pack state') // the proxy passes bytes through
      setPack(data)
      setPhase('ready')
      setUnavailable(false)
      return data
    } catch (e) {
      if (mine !== gen.current) return null
      if (packProblem(e).code === 'pack_unavailable') { setUnavailable(true); setPack(null); setPhase('ready') } else setPhase((p) => (p === 'ready' ? p : 'error'))
      return null
    }
  }, [])
  useEffect(() => { if (enabled) load() }, [enabled, load])
  useVisiblePoll(load, enabled)
  // a write's own answer is the truth; it replaces whatever an older read left
  const set = useCallback((p: PackState) => { gen.current++; setPack(p); setPhase('ready'); setUnavailable(false) }, [])
  return { pack, phase, unavailable, reload: load, set }
}

/** The pack writes. Each waits for Medic's next cycle (up to about 20 s) and is never retried; the answer is the new PackState. */
export const importPack = (file: File, sha256: string, override: boolean) =>
  api.post<PackState>('/medic/pack/import', file, {
    params: override ? { sha256, override: true } : { sha256 },
    headers: { 'Content-Type': 'application/json' },
    timeout: 45_000,
  }).then((r) => r.data)
export const revertPack = () => api.post<PackState>('/medic/pack/revert', null, { timeout: 45_000 }).then((r) => r.data)
export const switchToBundled = () => api.post<PackState>('/medic/pack/switch-to-bundled', null, { timeout: 45_000 }).then((r) => r.data)
