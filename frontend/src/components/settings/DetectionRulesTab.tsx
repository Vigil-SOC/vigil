import { useState, useEffect, useCallback } from 'react'
import {
  Box,
  Typography,
  Button,
  Alert,
  Chip,
  Card,
  CardContent,
  CardActions,
  Grid,
  IconButton,
  Tooltip,
  LinearProgress,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  TextField,
  MenuItem,
  Select,
  FormControl,
  InputLabel,
  CircularProgress,
  alpha,
} from '@mui/material'
import {
  Refresh as RefreshIcon,
  Add as AddIcon,
  Delete as DeleteIcon,
  CloudDownload as UpdateIcon,
  Security as SecurityIcon,
  Folder as FolderIcon,
  GitHub as GitHubIcon,
  CheckCircle as CheckIcon,
  Error as ErrorIcon,
  HourglassEmpty as PendingIcon,
} from '@mui/icons-material'
import { detectionRulesApi } from '../../services/api'

interface Source {
  id: string
  name: string
  type: 'git' | 'local'
  git_url: string
  format: string
  subdirectory: string
  story_subdirectory: string
  clone_name: string
  local_path: string
  rule_count: number
  last_updated: string | null
  status: string
}

interface Stats {
  total_rules: number
  sources_count: number
  by_format: Record<string, number>
  sources: Array<{
    name: string
    format: string
    count: number
    status: string
  }>
}

const FORMAT_COLORS: Record<string, string> = {
  sigma: '#2196f3',
  splunk: '#4caf50',
  elastic: '#ff9800',
  kql: '#9c27b0',
  auto: '#607d8b',
}

const RULE_FORMATS = ['sigma', 'splunk', 'elastic', 'kql', 'auto'] as const
type RuleFormat = typeof RULE_FORMATS[number]
const isRuleFormat = (v: string): v is RuleFormat => (RULE_FORMATS as readonly string[]).includes(v)

const STATUS_ICON: Record<string, React.ReactNode> = {
  ready: <CheckIcon color="success" sx={{ fontSize: 18 }} />,
  error: <ErrorIcon color="error" sx={{ fontSize: 18 }} />,
  not_cloned: <PendingIcon color="warning" sx={{ fontSize: 18 }} />,
  unknown: <PendingIcon color="disabled" sx={{ fontSize: 18 }} />,
}

export default function DetectionRulesTab() {
  const [sources, setSources] = useState<Source[]>([])
  const [stats, setStats] = useState<Stats | null>(null)
  const [loading, setLoading] = useState(false)
  const [updating, setUpdating] = useState<string | null>(null)
  const [message, setMessage] = useState<{ type: 'success' | 'error' | 'info'; text: string } | null>(null)
  const [addDialogOpen, setAddDialogOpen] = useState(false)
  const [deleteDialogOpen, setDeleteDialogOpen] = useState<{ open: boolean; sourceId: string; sourceName: string }>({
    open: false, sourceId: '', sourceName: '',
  })
  
  // Add source form state
  const [newSource, setNewSource] = useState({
    name: '',
    source_type: 'git' as 'git' | 'local',
    format: 'sigma' as RuleFormat,

    url: '',
    path: '',
    subdirectory: '',
    story_subdirectory: '',
  })

  const loadData = useCallback(async () => {
    setLoading(true)
    try {
      const [sourcesResp, statsResp] = await Promise.all([
        detectionRulesApi.listSources(),
        detectionRulesApi.getStats(),
      ])
      setSources(sourcesResp.data.sources || [])
      setStats(statsResp.data)
    } catch (error) {
      console.error('Error loading detection rules data:', error)
      setMessage({ type: 'error', text: 'Failed to load detection rules data' })
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    loadData()
  }, [loadData])

  const handleUpdateSource = async (sourceId: string) => {
    setUpdating(sourceId)
    try {
      await detectionRulesApi.updateSource(sourceId)
      setMessage({ type: 'success', text: 'Source updated and MCP server restarted' })
      await loadData()
    } catch (error: any) {
      setMessage({ type: 'error', text: error.response?.data?.detail || 'Failed to update source' })
    } finally {
      setUpdating(null)
      setTimeout(() => setMessage(null), 5000)
    }
  }

  const handleUpdateAll = async () => {
    setUpdating('all')
    try {
      const resp = await detectionRulesApi.updateAll()
      const results = resp.data.results || []
      const successes = results.filter((r: any) => r.success).length
      const failures = results.filter((r: any) => !r.success).length
      setMessage({
        type: failures > 0 ? 'error' : 'success',
        text: `Updated ${successes}/${results.length} sources${failures > 0 ? ` (${failures} failed)` : ''}`,
      })
      await loadData()
    } catch (error: any) {
      setMessage({ type: 'error', text: error.response?.data?.detail || 'Failed to update sources' })
    } finally {
      setUpdating(null)
      setTimeout(() => setMessage(null), 5000)
    }
  }

  const handleAddSource = async () => {
    try {
      await detectionRulesApi.addSource({
        name: newSource.name,
        source_type: newSource.source_type,
        format: newSource.format,
        url: newSource.source_type === 'git' ? newSource.url : undefined,
        path: newSource.source_type === 'local' ? newSource.path : undefined,
        subdirectory: newSource.subdirectory,
        story_subdirectory: newSource.story_subdirectory,
      })
      setMessage({ type: 'success', text: `Added source: ${newSource.name}` })
      setAddDialogOpen(false)
      setNewSource({ name: '', source_type: 'git', format: 'sigma', url: '', path: '', subdirectory: '', story_subdirectory: '' })
      await loadData()
    } catch (error: any) {
      setMessage({ type: 'error', text: error.response?.data?.detail || 'Failed to add source' })
    }
    setTimeout(() => setMessage(null), 5000)
  }

  const handleDeleteSource = async (sourceId: string, deleteFiles: boolean) => {
    try {
      await detectionRulesApi.removeSource(sourceId, deleteFiles)
      setMessage({ type: 'success', text: 'Source removed' })
      setDeleteDialogOpen({ open: false, sourceId: '', sourceName: '' })
      await loadData()
    } catch (error: any) {
      setMessage({ type: 'error', text: error.response?.data?.detail || 'Failed to remove source' })
    }
    setTimeout(() => setMessage(null), 5000)
  }

  const formatNumber = (n: number) => n.toLocaleString()

  return (
    <Box>
      {/* Header with stats */}
      <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', mb: 2 }}>
        <Box>
          <Typography variant="subtitle1" sx={{ fontWeight: 600, display: 'flex', alignItems: 'center', gap: 1 }}>
            <SecurityIcon sx={{ fontSize: 20 }} />
            Detection Rule Sources
          </Typography>
          {stats && (
            <Box sx={{ display: 'flex', gap: 1, mt: 1, flexWrap: 'wrap' }}>
              <Chip
                label={`${formatNumber(stats.total_rules)} total rules`}
                color="primary"
                size="small"
              />
              {Object.entries(stats.by_format).map(([fmt, count]) => (
                <Chip
                  key={fmt}
                  label={`${fmt}: ${formatNumber(count)}`}
                  size="small"
                  sx={{
                    bgcolor: alpha(FORMAT_COLORS[fmt] || '#607d8b', 0.15),
                    color: FORMAT_COLORS[fmt] || '#607d8b',
                    fontWeight: 500,
                  }}
                />
              ))}
            </Box>
          )}
        </Box>
        <Box sx={{ display: 'flex', gap: 1 }}>
          <Button size="small" startIcon={<RefreshIcon />} onClick={loadData} disabled={loading}>
            Refresh
          </Button>
          <Button
            size="small"
            variant="outlined"
            startIcon={updating === 'all' ? <CircularProgress size={16} /> : <UpdateIcon />}
            onClick={handleUpdateAll}
            disabled={!!updating}
          >
            Update All
          </Button>
          <Button size="small" variant="contained" startIcon={<AddIcon />} onClick={() => setAddDialogOpen(true)}>
            Add Source
          </Button>
        </Box>
      </Box>

      {loading && <LinearProgress sx={{ mb: 2 }} />}

      {message && (
        <Alert severity={message.type} sx={{ mb: 2 }} onClose={() => setMessage(null)}>
          {message.text}
        </Alert>
      )}

      {/* Source cards */}
      {sources.length === 0 ? (
        <Alert severity="info">
          No detection rule sources configured. Click "Add Source" to get started, or the service will seed defaults on first load.
        </Alert>
      ) : (
        <Grid container spacing={2}>
          {sources.map((source) => (
            <Grid item xs={12} sm={6} key={source.id}>
              <Card
                variant="outlined"
                sx={{
                  borderColor: source.status === 'ready' ? 'success.main' : source.status === 'error' ? 'error.main' : 'divider',
                  borderWidth: source.status === 'ready' ? 1.5 : 1,
                }}
              >
                <CardContent sx={{ pb: 1 }}>
                  <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mb: 1 }}>
                    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                      {source.type === 'git' ? (
                        <GitHubIcon sx={{ fontSize: 18, color: 'text.secondary' }} />
                      ) : (
                        <FolderIcon sx={{ fontSize: 18, color: 'text.secondary' }} />
                      )}
                      <Typography variant="body1" sx={{ fontWeight: 600 }}>
                        {source.name}
                      </Typography>
                    </Box>
                    {STATUS_ICON[source.status] || STATUS_ICON.unknown}
                  </Box>

                  <Box sx={{ display: 'flex', gap: 1, mb: 1, flexWrap: 'wrap' }}>
                    <Chip
                      label={source.format}
                      size="small"
                      sx={{
                        bgcolor: alpha(FORMAT_COLORS[source.format] || '#607d8b', 0.15),
                        color: FORMAT_COLORS[source.format] || '#607d8b',
                        fontWeight: 500,
                      }}
                    />
                    <Chip
                      label={`${formatNumber(source.rule_count)} rules`}
                      size="small"
                      variant="outlined"
                    />
                    <Chip
                      label={source.status}
                      size="small"
                      color={source.status === 'ready' ? 'success' : source.status === 'error' ? 'error' : 'default'}
                    />
                  </Box>

                  {source.git_url && (
                    <Typography variant="caption" color="text.secondary" sx={{ display: 'block', wordBreak: 'break-all' }}>
                      {source.git_url}
                    </Typography>
                  )}
                  {source.last_updated && (
                    <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
                      Last updated: {new Date(source.last_updated).toLocaleString()}
                    </Typography>
                  )}
                </CardContent>
                <CardActions sx={{ pt: 0, justifyContent: 'space-between' }}>
                  <Button
                    size="small"
                    startIcon={updating === source.id ? <CircularProgress size={14} /> : <UpdateIcon />}
                    onClick={() => handleUpdateSource(source.id)}
                    disabled={!!updating}
                  >
                    {source.status === 'not_cloned' ? 'Clone' : 'Update'}
                  </Button>
                  <Tooltip title="Remove source">
                    <IconButton
                      size="small"
                      color="error"
                      onClick={() => setDeleteDialogOpen({ open: true, sourceId: source.id, sourceName: source.name })}
                    >
                      <DeleteIcon sx={{ fontSize: 18 }} />
                    </IconButton>
                  </Tooltip>
                </CardActions>
              </Card>
            </Grid>
          ))}
        </Grid>
      )}

      {/* MCP Integration Info */}
      <Alert severity="info" sx={{ mt: 3 }}>
        <Typography variant="body2" sx={{ fontWeight: 500 }}>
          Security-Detections MCP Integration
        </Typography>
        <Typography variant="caption" color="text.secondary">
          Detection rule sources are automatically provided to the Security-Detections-MCP server.
          When Claude analyzes security findings, it can search across {stats ? formatNumber(stats.total_rules) : '...'} detection
          rules via 71+ specialized tools (search, coverage analysis, gap identification, and more).
          Updating sources automatically restarts the MCP server to rebuild its index.
        </Typography>
      </Alert>

      {/* Add Source Dialog */}
      <Dialog open={addDialogOpen} onClose={() => setAddDialogOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>Add Detection Rule Source</DialogTitle>
        <DialogContent>
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, mt: 1 }}>
            <TextField
              fullWidth
              label="Source Name"
              value={newSource.name}
              onChange={(e) => setNewSource({ ...newSource, name: e.target.value })}
              placeholder="e.g., My Custom Rules"
            />
            <FormControl fullWidth>
              <InputLabel>Source Type</InputLabel>
              <Select
                value={newSource.source_type}
                label="Source Type"
                onChange={(e) => setNewSource({ ...newSource, source_type: e.target.value as 'git' | 'local' })}
              >
                <MenuItem value="git">Git Repository</MenuItem>
                <MenuItem value="local">Local Directory</MenuItem>
              </Select>
            </FormControl>
            <FormControl fullWidth>
              <InputLabel>Rule Format</InputLabel>
              <Select
                value={newSource.format}
                label="Rule Format"

                onChange={(e) => {
                  if (isRuleFormat(e.target.value)) {
                    setNewSource({ ...newSource, format: e.target.value })
                  }
                }}

              >
                <MenuItem value="sigma">Sigma (YAML)</MenuItem>
                <MenuItem value="splunk">Splunk ESCU (YAML)</MenuItem>
                <MenuItem value="elastic">Elastic (TOML)</MenuItem>
                <MenuItem value="kql">KQL (MD/YAML/KQL)</MenuItem>
                <MenuItem value="auto">Auto-detect</MenuItem>
              </Select>
            </FormControl>
            {newSource.source_type === 'git' ? (
              <TextField
                fullWidth
                label="Git Repository URL"
                value={newSource.url}
                onChange={(e) => setNewSource({ ...newSource, url: e.target.value })}
                placeholder="https://github.com/org/repo.git"
              />
            ) : (
              <TextField
                fullWidth
                label="Local Directory Path"
                value={newSource.path}
                onChange={(e) => setNewSource({ ...newSource, path: e.target.value })}
                placeholder="/path/to/rules"
              />
            )}
            <TextField
              fullWidth
              label="Subdirectory (optional)"
              value={newSource.subdirectory}
              onChange={(e) => setNewSource({ ...newSource, subdirectory: e.target.value })}
              placeholder="e.g., rules"
              helperText="Subdirectory within the repo/path that contains the rules"
            />
            {newSource.format === 'splunk' && (
              <TextField
                fullWidth
                label="Story Subdirectory (optional)"
                value={newSource.story_subdirectory}
                onChange={(e) => setNewSource({ ...newSource, story_subdirectory: e.target.value })}
                placeholder="e.g., stories"
                helperText="Subdirectory for Splunk story files"
              />
            )}
          </Box>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setAddDialogOpen(false)}>Cancel</Button>
          <Button
            variant="contained"
            onClick={handleAddSource}
            disabled={!newSource.name || (newSource.source_type === 'git' ? !newSource.url : !newSource.path)}
          >
            Add Source
          </Button>
        </DialogActions>
      </Dialog>

      {/* Delete Confirmation Dialog */}
      <Dialog open={deleteDialogOpen.open} onClose={() => setDeleteDialogOpen({ ...deleteDialogOpen, open: false })}>
        <DialogTitle>Remove Detection Rule Source</DialogTitle>
        <DialogContent>
          <Typography variant="body2">
            Remove <strong>{deleteDialogOpen.sourceName}</strong> from detection rule sources?
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDeleteDialogOpen({ ...deleteDialogOpen, open: false })}>Cancel</Button>
          <Button
            color="warning"
            onClick={() => handleDeleteSource(deleteDialogOpen.sourceId, false)}
          >
            Remove (Keep Files)
          </Button>
          <Button
            color="error"
            variant="contained"
            onClick={() => handleDeleteSource(deleteDialogOpen.sourceId, true)}
          >
            Remove & Delete Files
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  )
}
