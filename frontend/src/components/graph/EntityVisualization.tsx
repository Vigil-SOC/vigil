/**
 * EntityVisualization — picks between the legacy EntityGraph and the
 * embedded VStrike iframe based on whether VStrike is configured for the
 * UI control plane (i.e. has username + password creds).
 *
 * Behavior: when VStrike is iframe-ready, we hard-replace the EntityGraph
 * with a VStrikeIframe — no toggle, no fallback. When VStrike is not
 * configured, we render the existing EntityGraph with the original props
 * passed through, so callers don't need to know which view is active.
 *
 * The probe (`POST /api/integrations/vstrike/ui/iframe-token`) decides
 * iframe-readiness. Result is cached at module level for `STALE_TIME_MS`
 * so multiple mounts on the same page (Investigation graph + Case dialog
 * + EventVisualizationDialog opening together) share one round-trip.
 *
 * The component intentionally does NOT depend on `@tanstack/react-query` —
 * the rest of the app does not have a `QueryClientProvider` at the root,
 * so any react-query hook crashes the host tree. Plain `useState` +
 * `useEffect` with a module cache covers the same ground.
 */

import { ReactNode, useEffect, useState } from 'react'
import { Box, CircularProgress } from '@mui/material'
import EntityGraph, { GraphLink, GraphNode } from './EntityGraph'
import VStrikeIframe from './VStrikeIframe'
import { vstrikeApi } from '../../services/api'

export interface EntityVisualizationProps {
  // Legacy EntityGraph props — pass-through when VStrike is not configured.
  nodes: GraphNode[]
  links: GraphLink[]
  onNodeClick?: (node: GraphNode) => void
  onLinkClick?: (link: GraphLink) => void
  height?: string | number
  width?: string | number
  showControls?: boolean
  highlightedNodes?: string[]
  maxNodes?: number

  // VStrike-specific: when present, the iframe auto-loads this network
  // on mount (user can still override via the dropdown).
  vstrikeNetworkId?: string

  // Rendered on the legacy (non-VStrike) path when `nodes.length === 0`.
  // Lets call sites keep their existing empty-state copy without gating
  // on node count themselves (which would skip the iframe path entirely
  // when the case happens to have zero entities).
  emptyState?: ReactNode
}

type ProbeState =
  | { kind: 'pending' }
  | { kind: 'ready' }
  | { kind: 'unavailable' }

interface CachedProbe {
  state: ProbeState
  fetchedAt: number
  inFlight?: Promise<ProbeState>
}

const STALE_TIME_MS = 60_000

// Module-level cache. Mounting EntityVisualization in three places at
// once should result in ONE network call, not three. The cache is
// process-local so a refresh / route change starts fresh.
const _cache: { entry: CachedProbe | null } = { entry: null }

async function probeVStrike(): Promise<ProbeState> {
  try {
    const tokenResp = await vstrikeApi.iframeToken()
    if (tokenResp.data?.iframe_url && tokenResp.data?.token) {
      return { kind: 'ready' }
    }
    return { kind: 'unavailable' }
  } catch {
    // 503 = not configured; transport error = not ready right now.
    return { kind: 'unavailable' }
  }
}

function getCachedOrFetch(): { state: ProbeState; promise?: Promise<ProbeState> } {
  const now = Date.now()
  const cached = _cache.entry
  if (cached && now - cached.fetchedAt < STALE_TIME_MS) {
    if (cached.inFlight) {
      return { state: cached.state, promise: cached.inFlight }
    }
    return { state: cached.state }
  }
  const promise = probeVStrike().then((next) => {
    _cache.entry = { state: next, fetchedAt: Date.now() }
    return next
  })
  _cache.entry = {
    state: { kind: 'pending' },
    fetchedAt: now,
    inFlight: promise,
  }
  return { state: { kind: 'pending' }, promise }
}

export default function EntityVisualization(props: EntityVisualizationProps) {
  const { height = 500, vstrikeNetworkId, emptyState, ...graphProps } = props

  // Initialize state from the cache so a remount after a recent probe
  // doesn't flash the legacy graph for a frame.
  const [state, setState] = useState<ProbeState>(
    () => getCachedOrFetch().state,
  )

  useEffect(() => {
    let cancelled = false
    const result = getCachedOrFetch()
    if (result.promise) {
      result.promise.then((next) => {
        if (!cancelled) setState(next)
      })
    } else {
      // Cache hit — adopt the latest known state synchronously.
      setState(result.state)
    }
    return () => {
      cancelled = true
    }
  }, [])

  if (state.kind === 'pending') {
    return (
      <Box
        sx={{
          height,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
        }}
      >
        <CircularProgress />
      </Box>
    )
  }

  if (state.kind === 'ready') {
    return (
      <VStrikeIframe height={height} initialNetworkId={vstrikeNetworkId} />
    )
  }

  if (
    emptyState !== undefined &&
    (!graphProps.nodes || graphProps.nodes.length === 0)
  ) {
    return <>{emptyState}</>
  }

  return <EntityGraph height={height} {...graphProps} />
}
