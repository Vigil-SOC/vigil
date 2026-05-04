import {
  createContext,
  ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { vstrikeApi } from '../services/api'

export interface KillchainStep {
  node_id: string
  timestamp: string
  technique?: string
  label?: string
  dwell_ms?: number
}

export interface AnchorOpts {
  networkId?: string
  // Findings the anchor wants the Play button to walk through. The host
  // copies the reference; the anchor doesn't need to memoize.
  findings?: Array<Record<string, any>>
}

export type ProbeState = 'pending' | 'ready' | 'unavailable'

export interface VStrikeIframeError {
  message: string
  missingCredentials?: boolean
}

export interface VStrikeIframeContextValue {
  state: ProbeState
  error: VStrikeIframeError | null
  iframeUrl: string | null
  /** Networks fetched via /ui/networks; populated lazily on first attach. */
  networks: Array<{ id: string; label: string; raw: Record<string, any> }>
  selectedNetwork: string
  setNetwork: (networkId: string) => void
  fullscreen: boolean
  setFullscreen: (on: boolean) => void
  /** True when an anchor is currently registered. */
  hasAnchor: boolean
  /** Findings the active anchor exposed (used to gate the Play button). */
  activeFindings: Array<Record<string, any>>
  /** Register a DOM anchor; the iframe is repositioned to overlay it. */
  attach: (anchor: HTMLDivElement, opts?: AnchorOpts) => void
  /**
   * Update the opts associated with the current anchor (e.g. `findings` arrived
   * later). No-op when `anchor` isn't the active one.
   */
  updateOpts: (anchor: HTMLDivElement, opts: AnchorOpts) => void
  /** Detach the anchor; the iframe hides until another one registers. */
  detach: (anchor: HTMLDivElement) => void
  /** Force a token + URL refresh (e.g. after the user re-saves Settings). */
  reload: () => void
  /** Fire the kill-chain replay over the active session. */
  triggerKillchain: (
    steps: KillchainStep[],
    opts?: { networkId?: string; loop?: boolean; autoPlay?: boolean },
  ) => Promise<{ ok: true } | { ok: false; status: number; message: string }>
  // -------------------------------------------------------------------------
  // Storylines & legends
  // -------------------------------------------------------------------------
  storylines: Array<{ id: string; label: string; raw: Record<string, any> }>
  selectedStoryline: string
  setStoryline: (storylineId: string) => void
  legendRuns: Array<{ id: string; label: string; raw: Record<string, any> }>
  selectedLegendRun: string
  setLegendRun: (legendRunId: string) => void
  // -------------------------------------------------------------------------
  // Iframe → toolbar sync (no API calls)
  // -------------------------------------------------------------------------
  syncNetworkFromIframe: (networkId: string) => void
  syncStorylineFromIframe: (storylineId: string) => void
  syncLegendRunFromIframe: (legendRunId: string) => void
  // -------------------------------------------------------------------------
  // Camera control
  // -------------------------------------------------------------------------
  cameraNode: (nodeIds: string[]) => Promise<{ ok: true } | { ok: false; message: string }>
  cameraPosition: (
    position: Record<string, number>,
    rotation?: Record<string, number>,
  ) => Promise<{ ok: true } | { ok: false; message: string }>
  // -------------------------------------------------------------------------
  // Storyline VCR playback
  // -------------------------------------------------------------------------
  applyStoryline: (storylineId: string) => Promise<{ ok: true } | { ok: false; message: string }>
  setStorylineMode: (mode: string) => Promise<{ ok: true } | { ok: false; message: string }>
  stepForward: () => Promise<{ ok: true } | { ok: false; message: string }>
  stepBackward: () => Promise<{ ok: true } | { ok: false; message: string }>
  // -------------------------------------------------------------------------
  // Node search / drift
  // -------------------------------------------------------------------------
  searchNodes: (query: string) => Promise<Array<Record<string, any>>>
  getNodeDrift: (nodeId: string) => Promise<Array<Record<string, any>>>
}

const VStrikeIframeContext = createContext<VStrikeIframeContextValue | null>(null)

export function useVStrikeIframe(): VStrikeIframeContextValue {
  const ctx = useContext(VStrikeIframeContext)
  if (!ctx) {
    throw new Error('useVStrikeIframe must be used inside <VStrikeIframeProvider>')
  }
  return ctx
}

function pickNetworkId(raw: Record<string, any>): string | null {
  for (const key of ['id', 'network_id', 'networkId', 'uuid']) {
    const value = raw?.[key]
    if (typeof value === 'string' && value) return value
    if (typeof value === 'number') return String(value)
  }
  return null
}

function pickNetworkLabel(raw: Record<string, any>, fallbackId: string): string {
  for (const key of ['name', 'label', 'display_name', 'title']) {
    const value = raw?.[key]
    if (typeof value === 'string' && value) return value
  }
  return fallbackId
}

function pickId(raw: Record<string, any>): string | null {
  for (const key of ['id', 'storyline_id', 'legend_run_id', 'uuid', 'runId']) {
    const value = raw?.[key]
    if (typeof value === 'string' && value) return value
    if (typeof value === 'number') return String(value)
  }
  return null
}

function pickLabel(raw: Record<string, any>, fallbackId: string): string {
  for (const key of ['name', 'label', 'display_name', 'title', 'description']) {
    const value = raw?.[key]
    if (typeof value === 'string' && value) return value
  }
  return fallbackId
}

function extractError(err: any): VStrikeIframeError {
  const status = err?.response?.status
  const detail = err?.response?.data?.detail
  if (status === 503 && detail && typeof detail === 'object') {
    const missing = Array.isArray(detail.missing) ? detail.missing : []
    return {
      message:
        detail.message ||
        'VStrike UI credentials are not configured. Add your username and password in Settings → Integrations → CloudCurrent VStrike.',
      missingCredentials:
        missing.includes('username') || missing.includes('password'),
    }
  }
  return {
    message:
      typeof detail === 'string'
        ? detail
        : err?.message || 'Failed to reach VStrike.',
  }
}

interface ProviderProps {
  children: ReactNode
}

/**
 * Wraps the app with the persistent VStrike iframe context.
 *
 * The iframe element is owned by `<VStrikeIframeHost>` (rendered as a sibling
 * of the layout root). Surfaces that want to display the iframe register a
 * DOM anchor via `attach()`; the host repositions the iframe to overlay the
 * anchor's bounding rect via a `ResizeObserver`. The iframe element never
 * unmounts, so the VStrike session cookie + JS state survive every case-click.
 */
export function VStrikeIframeProvider({ children }: ProviderProps) {
  const [state, setState] = useState<ProbeState>('pending')
  const [error, setError] = useState<VStrikeIframeError | null>(null)
  const [iframeUrl, setIframeUrl] = useState<string | null>(null)
  const [networks, setNetworks] = useState<
    Array<{ id: string; label: string; raw: Record<string, any> }>
  >([])
  const [selectedNetwork, setSelectedNetwork] = useState<string>('')
  const [fullscreen, setFullscreen] = useState(false)
  const [activeAnchor, setActiveAnchor] = useState<HTMLDivElement | null>(null)
  const [activeFindings, setActiveFindings] = useState<
    Array<Record<string, any>>
  >([])
  const [reloadKey, setReloadKey] = useState(0)

  const [storylines, setStorylines] = useState<
    Array<{ id: string; label: string; raw: Record<string, any> }>
  >([])
  const [selectedStoryline, setSelectedStoryline] = useState<string>('')
  const [legendRuns, setLegendRuns] = useState<
    Array<{ id: string; label: string; raw: Record<string, any> }>
  >([])
  const [selectedLegendRun, setSelectedLegendRun] = useState<string>('')

  const iframeReadyRef = useRef(false)
  const pendingNetworkRef = useRef<string | null>(null)
  const anchorOptsRef = useRef<Map<HTMLDivElement, AnchorOpts>>(new Map())
  const currentNetworkRef = useRef<string>('')
  const syncingFromIframeRef = useRef(false)

  // Fetch token + networks once per `reloadKey`. Mounted-once at provider
  // scope, so case-dialog opens never trigger a re-fetch.
  useEffect(() => {
    let cancelled = false
    iframeReadyRef.current = false
    setState('pending')
    setError(null)
    setIframeUrl(null)

    Promise.allSettled([
      vstrikeApi.iframeToken(),
      vstrikeApi.listNetworks(),
    ]).then(([tokenResult, networksResult]) => {
      if (cancelled) return

      if (tokenResult.status === 'rejected') {
        setError(extractError(tokenResult.reason))
        setState('unavailable')
        return
      }
      const url = tokenResult.value.data?.iframe_url
      if (!url) {
        setError({ message: 'VStrike returned no iframe URL.' })
        setState('unavailable')
        return
      }
      setIframeUrl(url)
      setState('ready')

      if (networksResult.status === 'fulfilled') {
        const items = networksResult.value.data?.networks || []
        const opts: Array<{ id: string; label: string; raw: Record<string, any> }> =
          []
        for (const raw of items) {
          const id = pickNetworkId(raw)
          if (!id) continue
          opts.push({ id, label: pickNetworkLabel(raw, id), raw })
        }
        setNetworks(opts)
      } else {
        // Best-effort — log but don't fail.
        // eslint-disable-next-line no-console
        console.warn('VStrike network-list failed:', networksResult.reason)
      }
    })

    return () => {
      cancelled = true
    }
  }, [reloadKey])

  const applyNetwork = useCallback((networkId: string) => {
    if (!networkId || syncingFromIframeRef.current) return
    vstrikeApi.loadNetwork(networkId).catch((err) => {
      // eslint-disable-next-line no-console
      console.warn('VStrike loadNetwork failed:', err)
    })
  }, [])

  const setNetwork = useCallback(
    (networkId: string) => {
      setSelectedNetwork(networkId)
      currentNetworkRef.current = networkId
      if (iframeReadyRef.current) {
        applyNetwork(networkId)
      } else {
        pendingNetworkRef.current = networkId
      }
    },
    [applyNetwork],
  )

  const syncNetworkFromIframe = useCallback((networkId: string) => {
    syncingFromIframeRef.current = true
    currentNetworkRef.current = networkId
    setSelectedNetwork(networkId)
    // Reset dependent selections when network changes from iframe.
    setSelectedStoryline('')
    setSelectedLegendRun('')
    Promise.resolve().then(() => {
      syncingFromIframeRef.current = false
    })
  }, [])

  const syncStorylineFromIframe = useCallback((storylineId: string) => {
    syncingFromIframeRef.current = true
    setSelectedStoryline(storylineId)
    Promise.resolve().then(() => {
      syncingFromIframeRef.current = false
    })
  }, [])

  const syncLegendRunFromIframe = useCallback((legendRunId: string) => {
    syncingFromIframeRef.current = true
    setSelectedLegendRun(legendRunId)
    Promise.resolve().then(() => {
      syncingFromIframeRef.current = false
    })
  }, [])

  const attach = useCallback(
    (anchor: HTMLDivElement, opts?: AnchorOpts) => {
      anchorOptsRef.current.set(anchor, opts ?? {})
      setActiveAnchor(anchor)
      setActiveFindings(opts?.findings ?? [])
      if (opts?.networkId) {
        setNetwork(opts.networkId)
      }
    },
    [setNetwork],
  )

  const updateOpts = useCallback(
    (anchor: HTMLDivElement, opts: AnchorOpts) => {
      anchorOptsRef.current.set(anchor, opts)
      setActiveAnchor((current) => {
        if (current === anchor) {
          setActiveFindings(opts.findings ?? [])
          if (opts.networkId) setNetwork(opts.networkId)
        }
        return current
      })
    },
    [setNetwork],
  )

  const detach = useCallback((anchor: HTMLDivElement) => {
    anchorOptsRef.current.delete(anchor)
    setActiveAnchor((current) => (current === anchor ? null : current))
  }, [])

  const reload = useCallback(() => {
    setReloadKey((k) => k + 1)
  }, [])

  const handleIframeLoad = useCallback(() => {
    iframeReadyRef.current = true
    const target = pendingNetworkRef.current || selectedNetwork
    if (target) {
      applyNetwork(target)
      pendingNetworkRef.current = null
    }
  }, [applyNetwork, selectedNetwork])

  const triggerKillchain = useCallback(
    async (
      steps: KillchainStep[],
      opts?: { networkId?: string; loop?: boolean; autoPlay?: boolean },
    ) => {
      const networkId = opts?.networkId || selectedNetwork
      if (!networkId) {
        return {
          ok: false as const,
          status: 0,
          message: 'No VStrike network selected.',
        }
      }
      if (!steps || steps.length === 0) {
        return {
          ok: false as const,
          status: 0,
          message: 'No kill-chain steps to play.',
        }
      }
      try {
        await vstrikeApi.killchainReplay(networkId, steps, {
          loop: opts?.loop ?? false,
          autoPlay: opts?.autoPlay ?? true,
        })
        return { ok: true as const }
      } catch (err: any) {
        const status = err?.response?.status ?? 0
        const detail = err?.response?.data?.detail
        const message =
          typeof detail === 'string'
            ? detail
            : detail?.message ||
              err?.message ||
              'Kill-chain replay failed.'
        return { ok: false as const, status, message }
      }
    },
    [selectedNetwork],
  )

  // -------------------------------------------------------------------------
  // Data fetching (storylines, legend runs)
  // -------------------------------------------------------------------------

  const fetchStorylines = useCallback(
    async (networkId: string) => {
      if (!networkId) {
        setStorylines([])
        return
      }
      try {
        // eslint-disable-next-line no-console
        console.log('[VStrike] fetching storylines for', networkId)
        const res = await vstrikeApi.listStorylines(networkId)
        // Cancel if user switched networks while we were in-flight.
        if (currentNetworkRef.current !== networkId) return
        const items = res.data?.storylines || []
        // eslint-disable-next-line no-console
        console.log('[VStrike] storylines response', items.length, 'items')
        const opts: Array<{ id: string; label: string; raw: Record<string, any> }> =
          []
        for (const raw of items) {
          const id = pickId(raw)
          if (!id) continue
          opts.push({ id, label: pickLabel(raw, id), raw })
        }
        setStorylines(opts)
      } catch (err) {
        if (currentNetworkRef.current !== networkId) return
        // eslint-disable-next-line no-console
        console.warn('VStrike listStorylines failed:', err)
        setStorylines([])
      }
    },
    [],
  )

  const fetchLegendRuns = useCallback(
    async (networkId: string) => {
      if (!networkId) {
        setLegendRuns([])
        return
      }
      try {
        // eslint-disable-next-line no-console
        console.log('[VStrike] fetching legend runs for', networkId)
        const res = await vstrikeApi.listLegendRuns(networkId)
        // Cancel if user switched networks while we were in-flight.
        if (currentNetworkRef.current !== networkId) return
        const items = res.data?.legend_runs || []
        // eslint-disable-next-line no-console
        console.log('[VStrike] legend runs response', items.length, 'items')
        const opts: Array<{ id: string; label: string; raw: Record<string, any> }> =
          []
        for (const raw of items) {
          const id = pickId(raw)
          if (!id) continue
          opts.push({ id, label: pickLabel(raw, id), raw })
        }
        setLegendRuns(opts)
      } catch (err) {
        if (currentNetworkRef.current !== networkId) return
        // eslint-disable-next-line no-console
        console.warn('VStrike listLegendRuns failed:', err)
        setLegendRuns([])
      }
    },
    [],
  )

  // When network changes, fetch storylines and legend runs.
  useEffect(() => {
    let cancelled = false
    if (selectedNetwork) {
      fetchStorylines(selectedNetwork).then(() => {
        if (cancelled) return
      })
      fetchLegendRuns(selectedNetwork).then(() => {
        if (cancelled) return
      })
    } else {
      setStorylines([])
      setLegendRuns([])
    }
    return () => {
      cancelled = true
    }
  }, [selectedNetwork, fetchStorylines, fetchLegendRuns])

  const setStoryline = useCallback(
    (storylineId: string) => {
      setSelectedStoryline(storylineId)
    },
    [],
  )

  const setLegendRun = useCallback(
    (legendRunId: string) => {
      setSelectedLegendRun(legendRunId)
    },
    [],
  )

  // -------------------------------------------------------------------------
  // Camera control
  // -------------------------------------------------------------------------

  const cameraNode = useCallback(
    async (nodeIds: string[]) => {
      const networkId = selectedNetwork
      if (!networkId) {
        return { ok: false as const, message: 'No VStrike network selected.' }
      }
      try {
        await vstrikeApi.uiCameraNode(nodeIds, networkId)
        return { ok: true as const }
      } catch (err: any) {
        const detail = err?.response?.data?.detail
        const message =
          typeof detail === 'string'
            ? detail
            : detail?.message || err?.message || 'Camera node focus failed.'
        return { ok: false as const, message }
      }
    },
    [selectedNetwork],
  )

  const cameraPosition = useCallback(
    async (
      position: Record<string, number>,
      rotation?: Record<string, number>,
    ) => {
      const networkId = selectedNetwork
      if (!networkId) {
        return { ok: false as const, message: 'No VStrike network selected.' }
      }
      try {
        await vstrikeApi.uiCameraPosition(position, rotation, networkId)
        return { ok: true as const }
      } catch (err: any) {
        const detail = err?.response?.data?.detail
        const message =
          typeof detail === 'string'
            ? detail
            : detail?.message || err?.message || 'Camera position failed.'
        return { ok: false as const, message }
      }
    },
    [selectedNetwork],
  )

  // -------------------------------------------------------------------------
  // Storyline VCR playback
  // -------------------------------------------------------------------------

  const applyStoryline = useCallback(
    async (storylineId: string) => {
      const networkId = selectedNetwork
      if (!networkId) {
        return { ok: false as const, message: 'No VStrike network selected.' }
      }
      try {
        await vstrikeApi.uiStorylineApply(storylineId, networkId)
        return { ok: true as const }
      } catch (err: any) {
        const detail = err?.response?.data?.detail
        const message =
          typeof detail === 'string'
            ? detail
            : detail?.message || err?.message || 'Apply storyline failed.'
        return { ok: false as const, message }
      }
    },
    [selectedNetwork],
  )

  const setStorylineMode = useCallback(
    async (mode: string) => {
      const networkId = selectedNetwork
      if (!networkId) {
        return { ok: false as const, message: 'No VStrike network selected.' }
      }
      try {
        await vstrikeApi.uiStorylineMode(mode, networkId)
        return { ok: true as const }
      } catch (err: any) {
        const detail = err?.response?.data?.detail
        const message =
          typeof detail === 'string'
            ? detail
            : detail?.message || err?.message || 'Set storyline mode failed.'
        return { ok: false as const, message }
      }
    },
    [selectedNetwork],
  )

  const stepForward = useCallback(async () => {
    const networkId = selectedNetwork
    if (!networkId) {
      return { ok: false as const, message: 'No VStrike network selected.' }
    }
    try {
      await vstrikeApi.uiStorylineForward(networkId)
      return { ok: true as const }
    } catch (err: any) {
      const detail = err?.response?.data?.detail
      const message =
        typeof detail === 'string'
          ? detail
          : detail?.message || err?.message || 'Step forward failed.'
      return { ok: false as const, message }
    }
  }, [selectedNetwork])

  const stepBackward = useCallback(async () => {
    const networkId = selectedNetwork
    if (!networkId) {
      return { ok: false as const, message: 'No VStrike network selected.' }
    }
    try {
      await vstrikeApi.uiStorylineBackward(networkId)
      return { ok: true as const }
    } catch (err: any) {
      const detail = err?.response?.data?.detail
      const message =
        typeof detail === 'string'
          ? detail
          : detail?.message || err?.message || 'Step backward failed.'
      return { ok: false as const, message }
    }
  }, [selectedNetwork])

  // -------------------------------------------------------------------------
  // Node search / drift
  // -------------------------------------------------------------------------

  const searchNodes = useCallback(
    async (query: string) => {
      const networkId = selectedNetwork
      try {
        const res = await vstrikeApi.nodeSearch(query, networkId)
        return res.data?.results || []
      } catch (err: any) {
        // eslint-disable-next-line no-console
        console.warn('VStrike nodeSearch failed:', err)
        return []
      }
    },
    [selectedNetwork],
  )

  const getNodeDrift = useCallback(
    async (nodeId: string) => {
      const networkId = selectedNetwork
      try {
        const res = await vstrikeApi.nodeDrift(nodeId, networkId)
        return res.data?.drift || []
      } catch (err: any) {
        // eslint-disable-next-line no-console
        console.warn('VStrike nodeDrift failed:', err)
        return []
      }
    },
    [selectedNetwork],
  )

  const value = useMemo<VStrikeIframeContextValue>(
    () => ({
      state,
      error,
      iframeUrl,
      networks,
      selectedNetwork,
      setNetwork,
      fullscreen,
      setFullscreen,
      hasAnchor: activeAnchor !== null,
      activeFindings,
      attach,
      updateOpts,
      detach,
      reload,
      triggerKillchain,
      storylines,
      selectedStoryline,
      setStoryline,
      legendRuns,
      selectedLegendRun,
      setLegendRun,
      syncNetworkFromIframe,
      syncStorylineFromIframe,
      syncLegendRunFromIframe,
      cameraNode,
      cameraPosition,
      applyStoryline,
      setStorylineMode,
      stepForward,
      stepBackward,
      searchNodes,
      getNodeDrift,
    }),
    [
      state,
      error,
      iframeUrl,
      networks,
      selectedNetwork,
      setNetwork,
      fullscreen,
      activeAnchor,
      activeFindings,
      attach,
      updateOpts,
      detach,
      reload,
      triggerKillchain,
      storylines,
      selectedStoryline,
      setStoryline,
      legendRuns,
      selectedLegendRun,
      setLegendRun,
      syncNetworkFromIframe,
      syncStorylineFromIframe,
      syncLegendRunFromIframe,
      cameraNode,
      cameraPosition,
      applyStoryline,
      setStorylineMode,
      stepForward,
      stepBackward,
      searchNodes,
      getNodeDrift,
    ],
  )

  // Expose internal refs to the host through a sub-context so we don't
  // re-render the whole subtree on every iframe load tick.
  return (
    <VStrikeIframeContext.Provider value={value}>
      <VStrikeIframeInternals.Provider
        value={{ activeAnchor, handleIframeLoad }}
      >
        {children}
      </VStrikeIframeInternals.Provider>
    </VStrikeIframeContext.Provider>
  )
}

interface InternalsValue {
  activeAnchor: HTMLDivElement | null
  handleIframeLoad: () => void
}

const VStrikeIframeInternals = createContext<InternalsValue | null>(null)

export function useVStrikeIframeInternals(): InternalsValue {
  const ctx = useContext(VStrikeIframeInternals)
  if (!ctx) {
    throw new Error(
      'useVStrikeIframeInternals must be used inside <VStrikeIframeProvider>',
    )
  }
  return ctx
}
