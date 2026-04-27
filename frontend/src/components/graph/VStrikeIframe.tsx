/**
 * VStrikeIframe — embeds VStrike's network visualization with auto-login
 * and remote network selection.
 *
 * Flow on mount:
 *   1. POST /integrations/vstrike/ui/iframe-token → short-lived token + URL
 *   2. GET  /integrations/vstrike/ui/networks    → populate dropdown
 *   3. Render <iframe src=iframe_url>
 *   4. After iframe load, if `initialNetworkId` is set, POST /load-network
 *
 * The dropdown calls /load-network on change. We do NOT reload the iframe
 * when the network changes — VStrike pushes that update over its own
 * WebSocket inside the iframe.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  FormControl,
  InputLabel,
  MenuItem,
  Paper,
  Select,
  SelectChangeEvent,
  Stack,
  Typography,
} from '@mui/material'
import { vstrikeApi } from '../../services/api'

interface VStrikeIframeProps {
  height?: number | string
  initialNetworkId?: string
  showControls?: boolean
}

interface NetworkOption {
  id: string
  label: string
  raw: Record<string, any>
}

interface ErrorState {
  message: string
  missingCredentials?: boolean
}

function pickNetworkId(raw: Record<string, any>): string | null {
  // Tolerate several shapes the engineer may settle on.
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

function extractError(err: any): ErrorState {
  const status = err?.response?.status
  const detail = err?.response?.data?.detail
  if (status === 503 && detail && typeof detail === 'object') {
    const missing = Array.isArray(detail.missing) ? detail.missing : []
    return {
      message:
        detail.message ||
        'VStrike UI credentials are not configured. Add your MCP username and password in Settings → Integrations → CloudCurrent VStrike.',
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

export default function VStrikeIframe({
  height = 600,
  initialNetworkId,
  showControls = true,
}: VStrikeIframeProps) {
  const [iframeUrl, setIframeUrl] = useState<string | null>(null)
  const [networks, setNetworks] = useState<NetworkOption[]>([])
  const [selectedNetwork, setSelectedNetwork] = useState<string>(
    initialNetworkId || '',
  )
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<ErrorState | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const iframeReadyRef = useRef(false)
  const pendingNetworkRef = useRef<string | null>(null)

  // Fetch token + networks in parallel on mount / retry.
  useEffect(() => {
    let cancelled = false
    iframeReadyRef.current = false
    setLoading(true)
    setError(null)
    setIframeUrl(null)

    Promise.allSettled([
      vstrikeApi.iframeToken(),
      vstrikeApi.listNetworks(),
    ]).then(([tokenResult, networksResult]) => {
      if (cancelled) return

      if (tokenResult.status === 'rejected') {
        setError(extractError(tokenResult.reason))
        setLoading(false)
        return
      }
      const url = tokenResult.value.data?.iframe_url
      if (!url) {
        setError({ message: 'VStrike returned no iframe URL.' })
        setLoading(false)
        return
      }
      setIframeUrl(url)

      if (networksResult.status === 'fulfilled') {
        const items = networksResult.value.data?.networks || []
        const options: NetworkOption[] = []
        for (const raw of items) {
          const id = pickNetworkId(raw)
          if (!id) continue
          options.push({ id, label: pickNetworkLabel(raw, id), raw })
        }
        setNetworks(options)
        if (!selectedNetwork && initialNetworkId) {
          // Honor initial network even if it's not in the list (loads anyway).
          setSelectedNetwork(initialNetworkId)
          pendingNetworkRef.current = initialNetworkId
        } else if (initialNetworkId && !options.some((o) => o.id === initialNetworkId)) {
          pendingNetworkRef.current = initialNetworkId
        }
      } else {
        // Networks list is best-effort — don't fail the whole iframe.
        console.warn('VStrike network-list failed:', networksResult.reason)
      }

      setLoading(false)
    })

    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reloadKey, initialNetworkId])

  // Apply pending or selected network once the iframe finishes loading.
  const applyNetwork = (networkId: string) => {
    if (!networkId) return
    vstrikeApi.loadNetwork(networkId).catch((err) => {
      console.warn('VStrike loadNetwork failed:', err)
    })
  }

  const handleIframeLoad = () => {
    iframeReadyRef.current = true
    const target = pendingNetworkRef.current || selectedNetwork
    if (target) {
      applyNetwork(target)
      pendingNetworkRef.current = null
    }
  }

  const handleNetworkChange = (event: SelectChangeEvent<string>) => {
    const value = event.target.value
    setSelectedNetwork(value)
    if (iframeReadyRef.current) {
      applyNetwork(value)
    } else {
      pendingNetworkRef.current = value
    }
  }

  const containerSx = useMemo(
    () => ({
      width: '100%',
      height,
      display: 'flex',
      flexDirection: 'column' as const,
    }),
    [height],
  )

  if (error) {
    return (
      <Paper variant="outlined" sx={containerSx}>
        <Box
          sx={{
            flex: 1,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            p: 3,
          }}
        >
          <Stack spacing={2} alignItems="center" maxWidth={520}>
            <Alert severity={error.missingCredentials ? 'warning' : 'error'}>
              {error.message}
            </Alert>
            {error.missingCredentials ? (
              <Button
                variant="outlined"
                href="/settings"
                onClick={(e) => {
                  // Allow the link to navigate via SPA when possible.
                  // (Settings page lives at /settings; the link still works
                  // for full reloads in older browsers.)
                  e.preventDefault()
                  window.location.assign('/settings')
                }}
              >
                Open Settings
              </Button>
            ) : (
              <Button
                variant="outlined"
                onClick={() => setReloadKey((k) => k + 1)}
              >
                Retry
              </Button>
            )}
          </Stack>
        </Box>
      </Paper>
    )
  }

  return (
    <Box sx={containerSx}>
      {showControls && (
        <Box
          sx={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            px: 1,
            py: 0.5,
          }}
        >
          <Typography variant="subtitle2">VStrike Network View</Typography>
          <FormControl size="small" sx={{ minWidth: 240 }} disabled={loading}>
            <InputLabel id="vstrike-network-label">Network</InputLabel>
            <Select
              labelId="vstrike-network-label"
              label="Network"
              value={selectedNetwork}
              onChange={handleNetworkChange}
              displayEmpty
            >
              <MenuItem value="">
                <em>{networks.length ? 'Select a network…' : 'No networks'}</em>
              </MenuItem>
              {networks.map((opt) => (
                <MenuItem key={opt.id} value={opt.id}>
                  {opt.label}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
        </Box>
      )}
      <Box sx={{ flex: 1, position: 'relative', minHeight: 0 }}>
        {loading && (
          <Box
            sx={{
              position: 'absolute',
              inset: 0,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              bgcolor: 'background.default',
              zIndex: 1,
            }}
          >
            <CircularProgress />
          </Box>
        )}
        {iframeUrl && (
          <iframe
            key={iframeUrl}
            src={iframeUrl}
            title="VStrike Network Visualization"
            onLoad={handleIframeLoad}
            sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
            referrerPolicy="no-referrer"
            style={{
              border: 0,
              width: '100%',
              height: '100%',
              display: 'block',
            }}
          />
        )}
      </Box>
    </Box>
  )
}
