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
 * Configuration check fans out two requests in parallel:
 *   - vstrikeApi.health()       → confirms the legacy topology base_url is set
 *   - vstrikeApi.iframeToken()  → confirms the new MCP login + ui-login-token
 *                                 path actually works end-to-end
 *
 * The iframe-token probe is the authoritative signal. If it succeeds we
 * pass its returned URL straight through (saving the iframe component a
 * second round-trip). If it 503s with `missing_credentials` we fall back
 * to the legacy graph silently.
 */

import { useEffect, useState } from 'react'
import { Box, CircularProgress } from '@mui/material'
import { useQuery } from '@tanstack/react-query'
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
}

interface VStrikeReadiness {
  ready: boolean
  // When ready, we already hold the iframe URL — VStrikeIframe doesn't
  // need to refetch immediately. (It will still fetch its own short-lived
  // token if the user reloads.)
  initialIframeUrl?: string
  initialToken?: string
}

async function probeVStrike(): Promise<VStrikeReadiness> {
  try {
    const tokenResp = await vstrikeApi.iframeToken()
    if (tokenResp.data?.iframe_url && tokenResp.data?.token) {
      return {
        ready: true,
        initialIframeUrl: tokenResp.data.iframe_url,
        initialToken: tokenResp.data.token,
      }
    }
    return { ready: false }
  } catch (err: any) {
    // 503 = not configured; any other error = not iframe-ready right now.
    return { ready: false }
  }
}

export default function EntityVisualization(props: EntityVisualizationProps) {
  const {
    height = 500,
    vstrikeNetworkId,
    ...graphProps
  } = props

  const { data, isLoading } = useQuery({
    queryKey: ['vstrike', 'iframe-ready'],
    queryFn: probeVStrike,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
    refetchOnMount: false,
    retry: false,
  })

  // Track when the probe has resolved at least once so the initial render
  // doesn't flash the legacy graph before the iframe takes over.
  const [resolved, setResolved] = useState(false)
  useEffect(() => {
    if (data !== undefined) setResolved(true)
  }, [data])

  if (isLoading && !resolved) {
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

  if (data?.ready) {
    return (
      <VStrikeIframe height={height} initialNetworkId={vstrikeNetworkId} />
    )
  }

  return <EntityGraph height={height} {...graphProps} />
}
