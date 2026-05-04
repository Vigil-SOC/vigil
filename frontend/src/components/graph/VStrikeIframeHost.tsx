import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  FormControl,
  IconButton,
  InputLabel,
  MenuItem,
  Paper,
  Select,
  SelectChangeEvent,
  Snackbar,
  Stack,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import {
  Fullscreen as FullscreenIcon,
  FullscreenExit as FullscreenExitIcon,
  PlayArrow as PlayIcon,
  SkipNext as SkipNextIcon,
  SkipPrevious as SkipPreviousIcon,
  Search as SearchIcon,
} from '@mui/icons-material'
import {
  useVStrikeIframe,
  useVStrikeIframeInternals,
} from '../../contexts/VStrikeIframeContext'
import { buildKillchainSteps } from './buildKillchainSteps'

interface Rect {
  top: number
  left: number
  width: number
  height: number
}

const HIDDEN_RECT: Rect = { top: -10000, left: -10000, width: 1, height: 1 }

const TOP_BAR_OFFSET_PX = 64

/**
 * Host for the single, app-wide VStrike iframe.
 *
 * Mounted once at the layout root. Owns the `<iframe>` element and tracks the
 * currently registered anchor (a `<div>` rendered by whichever surface wants
 * to display VStrike). The iframe is positioned absolutely over the anchor's
 * bounding rect via a `ResizeObserver` + `scroll` listener; it never unmounts,
 * so the VStrike session inside the iframe survives every navigation.
 *
 * The toolbar floats at the top of the iframe rect and is only visible when an
 * anchor is active. It includes network selection, storyline/legend controls,
 * VCR playback, node search, and the legacy kill-chain play button.
 */
export default function VStrikeIframeHost() {
  const ctx = useVStrikeIframe()
  const internals = useVStrikeIframeInternals()
  const iframeRef = useRef<HTMLIFrameElement | null>(null)
  const [rect, setRect] = useState<Rect>(HIDDEN_RECT)
  const [snackbar, setSnackbar] = useState<{
    severity: 'success' | 'error' | 'info'
    message: string
  } | null>(null)

  // Node search local state
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState<Array<Record<string, any>>>([])
  const [showSearchResults, setShowSearchResults] = useState(false)

  const anchor = internals.activeAnchor
  const fullscreen = ctx.fullscreen
  const visible = anchor !== null

  // Track the anchor's bounding rect. ResizeObserver covers anchor resize +
  // layout shifts; window resize/scroll covers viewport changes.
  useLayoutEffect(() => {
    if (fullscreen) {
      const update = () => {
        setRect({
          top: TOP_BAR_OFFSET_PX,
          left: 0,
          width: window.innerWidth,
          height: window.innerHeight - TOP_BAR_OFFSET_PX,
        })
      }
      update()
      window.addEventListener('resize', update)
      return () => window.removeEventListener('resize', update)
    }

    if (!anchor) {
      setRect(HIDDEN_RECT)
      return
    }

    const update = () => {
      const r = anchor.getBoundingClientRect()
      setRect({ top: r.top, left: r.left, width: r.width, height: r.height })
    }
    update()

    const ro = new ResizeObserver(update)
    ro.observe(anchor)
    // Catch scroll inside any ancestor (case dialog body, etc.).
    window.addEventListener('scroll', update, true)
    window.addEventListener('resize', update)
    return () => {
      ro.disconnect()
      window.removeEventListener('scroll', update, true)
      window.removeEventListener('resize', update)
    }
  }, [anchor, fullscreen])

  // Keep latest selection values inside a ref so the postMessage handler
  // can read them without re-registering the listener on every change.
  const selectedNetworkRef = useRef(ctx.selectedNetwork)
  const selectedStorylineRef = useRef(ctx.selectedStoryline)
  const selectedLegendRunRef = useRef(ctx.selectedLegendRun)
  selectedNetworkRef.current = ctx.selectedNetwork
  selectedStorylineRef.current = ctx.selectedStoryline
  selectedLegendRunRef.current = ctx.selectedLegendRun

  // Listen for state-change messages from the VStrike iframe.
  useEffect(() => {
    if (!ctx.iframeUrl) return
    let expectedOrigin: string
    try {
      expectedOrigin = new URL(ctx.iframeUrl).origin
    } catch {
      return
    }

    const handleMessage = (event: MessageEvent) => {
      if (event.origin !== expectedOrigin) return
      const data = event.data
      if (data?.type !== 'vstrike:state') return

      const networkId = typeof data.networkId === 'string' ? data.networkId : undefined
      const storylineId =
        data.storylineId === null ? '' : typeof data.storylineId === 'string' ? data.storylineId : undefined
      const legendRunId =
        data.legendRunId === null ? '' : typeof data.legendRunId === 'string' ? data.legendRunId : undefined

      if (networkId !== undefined && networkId !== selectedNetworkRef.current) {
        ctx.syncNetworkFromIframe(networkId)
      }
      if (storylineId !== undefined && storylineId !== selectedStorylineRef.current) {
        ctx.syncStorylineFromIframe(storylineId)
      }
      if (legendRunId !== undefined && legendRunId !== selectedLegendRunRef.current) {
        ctx.syncLegendRunFromIframe(legendRunId)
      }
    }

    window.addEventListener('message', handleMessage)
    return () => window.removeEventListener('message', handleMessage)
  }, [ctx.iframeUrl, ctx.syncNetworkFromIframe, ctx.syncStorylineFromIframe, ctx.syncLegendRunFromIframe])

  // Bridge the iframe's `onLoad` event into the context.
  const handleLoad = () => {
    internals.handleIframeLoad()
  }

  const handleNetworkChange = (event: SelectChangeEvent<string>) => {
    ctx.setNetwork(event.target.value)
  }

  const handlePlay = async () => {
    const steps = buildKillchainSteps(ctx.activeFindings)
    if (steps.length === 0) {
      setSnackbar({
        severity: 'info',
        message:
          'No kill-chain to play — none of the visible findings carry VStrike attack-path data.',
      })
      return
    }
    const result = await ctx.triggerKillchain(steps)
    if (result.ok) {
      setSnackbar({
        severity: 'success',
        message: `VStrike is replaying ${steps.length} step${
          steps.length === 1 ? '' : 's'
        }.`,
      })
      return
    }
    setSnackbar({ severity: 'error', message: result.message })
  }

  const playDisabled =
    ctx.state !== 'ready' ||
    !ctx.hasAnchor ||
    ctx.activeFindings.length === 0

  // -------------------------------------------------------------------------
  // Storyline controls
  // -------------------------------------------------------------------------

  const handleStorylineChange = (event: SelectChangeEvent<string>) => {
    ctx.setStoryline(event.target.value)
  }

  const handleApplyStoryline = async () => {
    if (!ctx.selectedStoryline) {
      setSnackbar({ severity: 'info', message: 'Select a storyline first.' })
      return
    }
    const result = await ctx.applyStoryline(ctx.selectedStoryline)
    if (result.ok) {
      setSnackbar({ severity: 'success', message: 'Storyline applied.' })
      return
    }
    setSnackbar({ severity: 'error', message: result.message })
  }

  // -------------------------------------------------------------------------
  // Legend run controls
  // -------------------------------------------------------------------------

  const handleLegendRunChange = (event: SelectChangeEvent<string>) => {
    ctx.setLegendRun(event.target.value)
  }

  // -------------------------------------------------------------------------
  // VCR playback controls
  // -------------------------------------------------------------------------

  const handleStepBackward = async () => {
    const result = await ctx.stepBackward()
    if (!result.ok) {
      setSnackbar({ severity: 'error', message: result.message })
    }
  }

  const handleStepForward = async () => {
    const result = await ctx.stepForward()
    if (!result.ok) {
      setSnackbar({ severity: 'error', message: result.message })
    }
  }

  const vcrDisabled = ctx.state !== 'ready' || !ctx.hasAnchor

  // -------------------------------------------------------------------------
  // Node search
  // -------------------------------------------------------------------------

  const handleSearch = async () => {
    if (!searchQuery.trim()) return
    const results = await ctx.searchNodes(searchQuery.trim())
    setSearchResults(results)
    setShowSearchResults(true)
    if (results.length === 0) {
      setSnackbar({ severity: 'info', message: 'No nodes matched your search.' })
    }
  }

  const handleSearchKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      handleSearch()
    }
  }

  const handleFocusNode = async (nodeId: string) => {
    const result = await ctx.cameraNode([nodeId])
    if (result.ok) {
      setShowSearchResults(false)
      setSearchQuery('')
      return
    }
    setSnackbar({ severity: 'error', message: result.message })
  }

  // Error overlay positioned over the anchor.
  if (ctx.error && visible) {
    return (
      <Paper
        variant="outlined"
        sx={{
          position: 'fixed',
          top: rect.top,
          left: rect.left,
          width: rect.width,
          height: rect.height,
          zIndex: 1301,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          p: 3,
          pointerEvents: 'auto',
        }}
      >
        <Stack spacing={2} alignItems="center" maxWidth={520}>
          <Alert severity={ctx.error.missingCredentials ? 'warning' : 'error'}>
            {ctx.error.message}
          </Alert>
          {ctx.error.missingCredentials ? (
            <Button
              variant="outlined"
              href="/settings"
              onClick={(e) => {
                e.preventDefault()
                window.location.assign('/settings')
              }}
            >
              Open Settings
            </Button>
          ) : (
            <Button variant="outlined" onClick={ctx.reload}>
              Retry
            </Button>
          )}
        </Stack>
      </Paper>
    )
  }

  return (
    <>
      <Box
        sx={{
          position: 'fixed',
          top: rect.top,
          left: rect.left,
          width: rect.width,
          height: rect.height,
          visibility: visible ? 'visible' : 'hidden',
          pointerEvents: visible ? 'auto' : 'none',
          opacity: visible ? 1 : 0,
          zIndex: 1301,
          transition: 'opacity 120ms ease',
          display: 'flex',
          flexDirection: 'column',
          bgcolor: 'background.paper',
          border: fullscreen ? 0 : 1,
          borderColor: 'divider',
        }}
      >
        {/* Toolbar */}
        <Box
          sx={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            px: 1,
            py: 0.5,
            borderBottom: 1,
            borderColor: 'divider',
            bgcolor: 'background.paper',
            minHeight: 44,
            gap: 1,
            flexWrap: 'wrap',
          }}
        >
          <Typography variant="subtitle2" sx={{ pl: 1, whiteSpace: 'nowrap' }}>
            VStrike Network View
          </Typography>

          <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap">
            {/* Network selector */}
            <FormControl
              size="small"
              sx={{ minWidth: 180 }}
              disabled={ctx.state !== 'ready'}
            >
              <InputLabel id="vstrike-network-label">Network</InputLabel>
              <Select
                labelId="vstrike-network-label"
                label="Network"
                value={ctx.selectedNetwork}
                onChange={handleNetworkChange}
                displayEmpty
                MenuProps={{ sx: { zIndex: 1302 } }}
              >
                <MenuItem value="">
                  <em>
                    {ctx.networks.length ? 'Select a network…' : 'No networks'}
                  </em>
                </MenuItem>
                {ctx.networks.map((opt) => (
                  <MenuItem key={opt.id} value={opt.id}>
                    {opt.label}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>

            {/* Storyline selector */}
            <FormControl
              size="small"
              sx={{ minWidth: 160 }}
              disabled={ctx.state !== 'ready' || ctx.storylines.length === 0}
            >
              <InputLabel id="vstrike-storyline-label">Storyline</InputLabel>
              <Select
                labelId="vstrike-storyline-label"
                label="Storyline"
                value={ctx.selectedStoryline}
                onChange={handleStorylineChange}
                displayEmpty
                MenuProps={{ sx: { zIndex: 1302 } }}
              >
                <MenuItem value="">
                  <em>
                    {ctx.storylines.length ? 'Select…' : 'None'}
                  </em>
                </MenuItem>
                {ctx.storylines.map((opt) => (
                  <MenuItem key={opt.id} value={opt.id}>
                    {opt.label}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>

            <Button
              size="small"
              variant="outlined"
              disabled={
                ctx.state !== 'ready' || !ctx.selectedStoryline
              }
              onClick={handleApplyStoryline}
            >
              Apply
            </Button>

            {/* Legend run selector */}
            <FormControl
              size="small"
              sx={{ minWidth: 140 }}
              disabled={ctx.state !== 'ready' || ctx.legendRuns.length === 0}
            >
              <InputLabel id="vstrike-legend-label">Legend</InputLabel>
              <Select
                labelId="vstrike-legend-label"
                label="Legend"
                value={ctx.selectedLegendRun}
                onChange={handleLegendRunChange}
                displayEmpty
                MenuProps={{ sx: { zIndex: 1302 } }}
              >
                <MenuItem value="">
                  <em>
                    {ctx.legendRuns.length ? 'Select…' : 'None'}
                  </em>
                </MenuItem>
                {ctx.legendRuns.map((opt) => (
                  <MenuItem key={opt.id} value={opt.id}>
                    {opt.label}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>

            {/* Node search */}
            <TextField
              size="small"
              placeholder="Search nodes…"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              onKeyDown={handleSearchKeyDown}
              disabled={ctx.state !== 'ready'}
              sx={{ width: 140 }}
              InputProps={{
                endAdornment: (
                  <IconButton size="small" onClick={handleSearch} disabled={ctx.state !== 'ready'}>
                    <SearchIcon fontSize="small" />
                  </IconButton>
                ),
              }}
            />

            {/* VCR controls */}
            <Tooltip title="Step backward">
              <span>
                <IconButton
                  size="small"
                  onClick={handleStepBackward}
                  disabled={vcrDisabled}
                  aria-label="Step backward"
                >
                  <SkipPreviousIcon />
                </IconButton>
              </span>
            </Tooltip>
            <Tooltip title="Step forward">
              <span>
                <IconButton
                  size="small"
                  onClick={handleStepForward}
                  disabled={vcrDisabled}
                  aria-label="Step forward"
                >
                  <SkipNextIcon />
                </IconButton>
              </span>
            </Tooltip>

            {/* Legacy kill-chain play */}
            <Tooltip
              title={
                playDisabled
                  ? 'Play requires VStrike-enriched findings in this view.'
                  : 'Replay the kill-chain in the VStrike view'
              }
            >
              <span>
                <IconButton
                  size="small"
                  onClick={handlePlay}
                  disabled={playDisabled}
                  aria-label="Play kill-chain"
                >
                  <PlayIcon />
                </IconButton>
              </span>
            </Tooltip>

            {/* Fullscreen */}
            <Tooltip title={fullscreen ? 'Exit full screen' : 'Full screen'}>
              <IconButton
                size="small"
                onClick={() => ctx.setFullscreen(!fullscreen)}
                aria-label={fullscreen ? 'Exit full screen' : 'Full screen'}
              >
                {fullscreen ? <FullscreenExitIcon /> : <FullscreenIcon />}
              </IconButton>
            </Tooltip>
          </Stack>
        </Box>

        {/* Search results overlay */}
        {showSearchResults && searchResults.length > 0 && (
          <Paper
            elevation={3}
            sx={{
              position: 'absolute',
              top: 52,
              right: 8,
              width: 280,
              maxHeight: 300,
              overflow: 'auto',
              zIndex: 10,
              p: 1,
            }}
          >
            <Stack spacing={0.5}>
              <Typography variant="caption" color="text.secondary">
                {searchResults.length} result{searchResults.length === 1 ? '' : 's'}
              </Typography>
              {searchResults.map((r) => {
                const id = r.node_id || r.id || 'unknown'
                const name = r.node_name || r.name || id
                return (
                  <Button
                    key={id}
                    size="small"
                    variant="text"
                    sx={{ justifyContent: 'flex-start' }}
                    onClick={() => handleFocusNode(id)}
                  >
                    {name}
                  </Button>
                )
              })}
              <Button
                size="small"
                variant="text"
                color="secondary"
                onClick={() => setShowSearchResults(false)}
              >
                Close
              </Button>
            </Stack>
          </Paper>
        )}

        {/* Iframe + loading overlay */}
        <Box sx={{ flex: 1, position: 'relative', minHeight: 0 }}>
          {ctx.state === 'pending' && (
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
          {ctx.iframeUrl && (
            <iframe
              ref={iframeRef}
              src={ctx.iframeUrl}
              title="VStrike Network Visualization"
              onLoad={handleLoad}
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
      <Snackbar
        open={snackbar !== null}
        autoHideDuration={5000}
        onClose={() => setSnackbar(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
      >
        {snackbar ? (
          <Alert
            severity={snackbar.severity}
            onClose={() => setSnackbar(null)}
            sx={{ maxWidth: 560 }}
          >
            {snackbar.message}
          </Alert>
        ) : undefined}
      </Snackbar>
    </>
  )
}
