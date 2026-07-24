/**
 * EntityVisualization — picks between the legacy EntityGraph and the
 * embedded VStrike iframe based on whether VStrike is configured for the
 * UI control plane (i.e. has username + password creds).
 *
 * When VStrike is iframe-ready, this component renders an empty `<div>`
 * **anchor** and registers it with the persistent `VStrikeIframeProvider`
 * (mounted once at MainLayout level). The actual iframe lives on the
 * provider — never unmounts, never re-auths between case clicks. We just
 * tell the provider "the iframe should overlay this rect now".
 *
 * When VStrike is not configured, we render the existing EntityGraph with
 * the original props passed through, so callers don't need to know which
 * view is active.
 *
 * The VStrike-readiness probe first checks the MCP server enabled state, then
 * verifies the iframe token route. The provider is always mounted, but we
 * shouldn't anchor into it on surfaces where VStrike is disabled.
 */

import { ReactNode, useEffect, useRef, useState } from 'react'
import { Box, CircularProgress } from '@mui/material'
import EntityGraph, { GraphLink, GraphNode } from './EntityGraph'
import { mcpApi, vstrikeApi } from '../../services/api'
import { useVStrikeIframe } from '../../contexts/VStrikeIframeContext'

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

  // Optional findings list — used by the toolbar Play button to build a
  // kill-chain step list. When omitted, Play stays disabled.
  vstrikeFindings?: Array<Record<string, any>>

  // Rendered on the legacy (non-VStrike) path when `nodes.length === 0`.
  emptyState?: ReactNode
}

type ProbeState =
  | { kind: 'pending' }
  | { kind: 'ready' }
  | { kind: 'unavailable' }

interface CachedProbe {
  state: ProbeState
  inFlight?: Promise<ProbeState>
}

const VSTRIKE_MCP_SERVER = 'vstrike'

const _cache: { entry: CachedProbe | null } = { entry: null }

async function probeVStrike(): Promise<ProbeState> {
  try {
    const statusesResp = await mcpApi.getStatuses()
    const statusList = statusesResp.data?.statuses || []
    const vstrikeStatus = Array.isArray(statusList)
      ? statusList.find((item: { name?: string }) => item.name === VSTRIKE_MCP_SERVER)
      : null
    if (!vstrikeStatus?.enabled) {
      return { kind: 'unavailable' }
    }

    const tokenResp = await vstrikeApi.iframeToken()
    if (tokenResp.data?.iframe_url && tokenResp.data?.token) {
      return { kind: 'ready' }
    }
    return { kind: 'unavailable' }
  } catch {
    return { kind: 'unavailable' }
  }
}

function getCachedOrFetch(): { state: ProbeState; promise?: Promise<ProbeState> } {
  const cached = _cache.entry
  if (cached?.inFlight) {
    return { state: cached.state, promise: cached.inFlight }
  }
  const promise = probeVStrike().then((next) => {
    _cache.entry = { state: next }
    return next
  })
  _cache.entry = {
    state: { kind: 'pending' },
    inFlight: promise,
  }
  return { state: { kind: 'pending' }, promise }
}

export default function EntityVisualization(props: EntityVisualizationProps) {
  const {
    height = 500,
    vstrikeNetworkId,
    vstrikeFindings,
    emptyState,
    ...graphProps
  } = props
  const vstrike = useVStrikeIframe()
  const anchorRef = useRef<HTMLDivElement | null>(null)

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
      setState(result.state)
    }
    return () => {
      cancelled = true
    }
  }, [])

  // Attach the anchor to the persistent host whenever we're ready + mounted.
  useEffect(() => {
    if (state.kind !== 'ready') return
    const anchor = anchorRef.current
    if (!anchor) return
    vstrike.attach(anchor, {
      networkId: vstrikeNetworkId,
      findings: vstrikeFindings,
    })
    return () => {
      vstrike.detach(anchor)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.kind])

  // Push opts updates without re-attaching when only the network or findings change.
  useEffect(() => {
    if (state.kind !== 'ready') return
    const anchor = anchorRef.current
    if (!anchor) return
    vstrike.updateOpts(anchor, {
      networkId: vstrikeNetworkId,
      findings: vstrikeFindings,
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.kind, vstrikeNetworkId, vstrikeFindings])

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
      <Box
        ref={anchorRef}
        sx={{
          width: '100%',
          height,
          // The anchor is just a positioning target — the iframe rendered by
          // VStrikeIframeHost overlays this rect via fixed positioning.
          position: 'relative',
          minHeight: 0,
        }}
      />
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
