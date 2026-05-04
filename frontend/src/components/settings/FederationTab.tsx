import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  FormControl,
  FormControlLabel,
  InputLabel,
  MenuItem,
  Paper,
  Select,
  Stack,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import RefreshIcon from '@mui/icons-material/Refresh'
import PlayArrowIcon from '@mui/icons-material/PlayArrow'
import { federationApi, FederationSourceView } from '../../services/api'

interface Props {
  onMessage: (msg: { type: 'success' | 'error'; text: string }) => void
}

const SEVERITY_OPTIONS: Array<{ value: string; label: string }> = [
  { value: '', label: 'Any' },
  { value: 'low', label: 'Low+' },
  { value: 'medium', label: 'Medium+' },
  { value: 'high', label: 'High+' },
  { value: 'critical', label: 'Critical only' },
]

const SOURCE_LABELS: Record<string, string> = {
  splunk: 'Splunk',
  crowdstrike: 'CrowdStrike Falcon',
  azure_sentinel: 'Azure Sentinel',
  aws_security_hub: 'AWS Security Hub',
  microsoft_defender: 'Microsoft Defender',
  elastic: 'Elastic Security',
}

function formatRelative(iso: string | null): string {
  if (!iso) return 'never'
  const t = new Date(iso).getTime()
  if (isNaN(t)) return 'never'
  const sec = Math.floor((Date.now() - t) / 1000)
  if (sec < 60) return `${sec}s ago`
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`
  return `${Math.floor(sec / 86400)}d ago`
}

export default function FederationTab({ onMessage }: Props) {
  const [globalEnabled, setGlobalEnabled] = useState(false)
  const [sources, setSources] = useState<FederationSourceView[]>([])
  const [loading, setLoading] = useState(true)
  const [savingSource, setSavingSource] = useState<string | null>(null)

  const loadAll = async () => {
    try {
      const res = await federationApi.listSources()
      setSources(res.data.sources || [])
      setGlobalEnabled(Boolean(res.data.global?.enabled))
    } catch {
      onMessage({ type: 'error', text: 'Failed to load federation sources' })
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadAll()
    // Refresh health every 10s so the user sees last_success_at advance.
    const t = setInterval(loadAll, 10_000)
    return () => clearInterval(t)
  }, [])

  const toggleGlobal = async (enabled: boolean) => {
    setGlobalEnabled(enabled)
    try {
      await federationApi.setSettings(enabled)
      onMessage({
        type: 'success',
        text: `Federated monitoring ${enabled ? 'enabled' : 'disabled'}`,
      })
    } catch {
      onMessage({ type: 'error', text: 'Failed to update global setting' })
      setGlobalEnabled(!enabled)
    }
  }

  const patchSource = async (
    sourceId: string,
    patch: Parameters<typeof federationApi.updateSource>[1],
  ) => {
    setSavingSource(sourceId)
    try {
      const res = await federationApi.updateSource(sourceId, patch)
      setSources((prev) => prev.map((s) => (s.source_id === sourceId ? res.data : s)))
    } catch {
      onMessage({ type: 'error', text: `Failed to update ${sourceId}` })
    } finally {
      setSavingSource(null)
    }
  }

  const pollNow = async (sourceId: string) => {
    try {
      await federationApi.pollNow(sourceId)
      onMessage({ type: 'success', text: `Triggered poll for ${sourceId}` })
    } catch {
      onMessage({ type: 'error', text: `Failed to trigger poll for ${sourceId}` })
    }
  }

  const counts = useMemo(() => {
    const enabled = sources.filter((s) => s.enabled).length
    const errors = sources.filter((s) => (s.consecutive_errors || 0) > 0).length
    return { enabled, errors, total: sources.length }
  }, [sources])

  if (loading) {
    return (
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, p: 2 }}>
        <CircularProgress size={16} />
        <Typography variant="body2">Loading federated monitoring…</Typography>
      </Box>
    )
  }

  return (
    <Box>
      <Typography variant="subtitle1" sx={{ fontWeight: 600, mb: 0.5 }}>
        Federated Monitoring
      </Typography>
      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 2 }}>
        Pull findings from external SIEM/EDR sources on a configurable cadence and
        feed them into the auto-investigator. Global toggle disables all federation
        polling; per-source rows control which sources are pulled and how often.
        First-run on enable starts from "now" — no historical backfill.
      </Typography>

      <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
        <Stack direction="row" alignItems="center" spacing={2}>
          <FormControlLabel
            control={
              <Switch
                checked={globalEnabled}
                onChange={(e) => toggleGlobal(e.target.checked)}
              />
            }
            label={
              <Typography variant="body2" sx={{ fontWeight: 600 }}>
                Federated monitoring is {globalEnabled ? 'ON' : 'OFF'}
              </Typography>
            }
          />
          <Box sx={{ flex: 1 }} />
          <Chip size="small" label={`${counts.enabled}/${counts.total} sources enabled`} />
          {counts.errors > 0 && (
            <Chip size="small" color="warning" label={`${counts.errors} with errors`} />
          )}
          <Tooltip title="Refresh">
            <Button size="small" startIcon={<RefreshIcon />} onClick={loadAll}>
              Refresh
            </Button>
          </Tooltip>
        </Stack>
      </Paper>

      {!globalEnabled && (
        <Alert severity="info" sx={{ mb: 2 }}>
          Federation is globally disabled. Enable the master switch above to begin
          polling enabled sources. Per-source rows can still be configured below
          while global is off.
        </Alert>
      )}

      <TableContainer component={Paper} variant="outlined">
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>Source</TableCell>
              <TableCell>Enabled</TableCell>
              <TableCell>Interval (s)</TableCell>
              <TableCell>Min severity</TableCell>
              <TableCell>Last success</TableCell>
              <TableCell>Errors</TableCell>
              <TableCell align="right">Actions</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {sources.length === 0 && (
              <TableRow>
                <TableCell colSpan={7}>
                  <Typography variant="body2" color="text.secondary">
                    No federation sources available. Configure an integration
                    (Splunk, CrowdStrike, Sentinel, etc.) under Integrations / MCP
                    first — adapters auto-seed when the daemon starts.
                  </Typography>
                </TableCell>
              </TableRow>
            )}
            {sources.map((s) => {
              const label = SOURCE_LABELS[s.source_id] || s.source_id
              const lastErr = s.last_error
              return (
                <TableRow key={s.source_id} hover>
                  <TableCell>
                    <Stack>
                      <Typography variant="body2">{label}</Typography>
                      <Typography variant="caption" color="text.secondary">
                        {s.source_id}
                        {!s.is_configured && ' · not configured'}
                      </Typography>
                    </Stack>
                  </TableCell>
                  <TableCell>
                    <Switch
                      size="small"
                      checked={s.enabled}
                      disabled={!s.is_configured || savingSource === s.source_id}
                      onChange={(e) =>
                        patchSource(s.source_id, { enabled: e.target.checked })
                      }
                    />
                  </TableCell>
                  <TableCell>
                    <TextField
                      size="small"
                      type="number"
                      sx={{ width: 90 }}
                      value={s.interval_seconds}
                      onChange={(e) =>
                        setSources((prev) =>
                          prev.map((row) =>
                            row.source_id === s.source_id
                              ? { ...row, interval_seconds: Number(e.target.value) }
                              : row,
                          ),
                        )
                      }
                      onBlur={() =>
                        patchSource(s.source_id, { interval_seconds: s.interval_seconds })
                      }
                      inputProps={{ min: 10, max: 86400 }}
                    />
                  </TableCell>
                  <TableCell>
                    <FormControl size="small" sx={{ minWidth: 110 }}>
                      <InputLabel>Floor</InputLabel>
                      <Select
                        label="Floor"
                        value={s.min_severity || ''}
                        onChange={(e) =>
                          patchSource(s.source_id, {
                            min_severity: e.target.value
                              ? String(e.target.value)
                              : null,
                          })
                        }
                      >
                        {SEVERITY_OPTIONS.map((opt) => (
                          <MenuItem key={opt.value} value={opt.value}>
                            {opt.label}
                          </MenuItem>
                        ))}
                      </Select>
                    </FormControl>
                  </TableCell>
                  <TableCell>
                    <Typography variant="body2" color="text.secondary">
                      {formatRelative(s.last_success_at)}
                    </Typography>
                  </TableCell>
                  <TableCell>
                    {(s.consecutive_errors || 0) > 0 ? (
                      <Tooltip title={lastErr || ''}>
                        <Chip
                          size="small"
                          color="warning"
                          label={s.consecutive_errors}
                        />
                      </Tooltip>
                    ) : (
                      <Typography variant="caption" color="text.secondary">
                        —
                      </Typography>
                    )}
                  </TableCell>
                  <TableCell align="right">
                    <Tooltip
                      title={
                        s.is_configured
                          ? 'Trigger an immediate poll, bypassing the interval'
                          : 'Configure the integration first'
                      }
                    >
                      <span>
                        <Button
                          size="small"
                          startIcon={<PlayArrowIcon />}
                          disabled={!s.is_configured}
                          onClick={() => pollNow(s.source_id)}
                        >
                          Poll now
                        </Button>
                      </span>
                    </Tooltip>
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </TableContainer>
    </Box>
  )
}
