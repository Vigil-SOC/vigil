import { useState, useEffect, useCallback } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  Box,
  Grid,
  Paper,
  Typography,
  Card,
  CardContent,
  Switch,
  FormControlLabel,
  Button,
  Chip,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  IconButton,
  Tooltip,
  Alert,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  LinearProgress,
  Divider,
  Stack,
  CircularProgress,
  Tabs,
  Tab,
  TextField,
} from '@mui/material'
import {
  Refresh as RefreshIcon,
  PlayArrow as PlayIcon,
  Stop as StopIcon,
  Delete as KillIcon,
  Visibility as ViewIcon,
  AttachMoney as CostIcon,
  SmartToy as AgentIcon,
  CheckCircle as DoneIcon,
  Error as ErrorIcon,
  HourglassEmpty as QueuedIcon,
  Alarm as AlarmIcon,
  Search as ScanIcon,
  Close as CloseIcon,
  ThumbUp as ApproveIcon,
  Replay as ReworkIcon,
  Psychology as DecisionsIcon,
} from '@mui/icons-material'
import { orchestratorApi, configApi, reasoningApi } from '../services/api'

interface OrchestratorStatus {
  enabled: boolean
  active_agents: number
  max_concurrent_agents: number
  queued: number
  completed: number
  failed: number
  pending_review: number
  total_investigations: number
  cost: {
    total_cost_usd: number
    active_cost_usd: number
    hourly_cost_usd: number
    hourly_budget_remaining: number
    per_investigation_limit: number
  }
  stats: Record<string, number>
}

interface Investigation {
  investigation_id: string
  case_id: string | null
  skill_id: string
  trigger_type: string
  status: string
  current_step: number
  total_steps: number
  iteration_count: number
  cost_usd: number
  priority: string
  created_at: string
  last_activity_at: string | null
  summary: string | null
  current_activity: string | null
}

const STATUS_COLORS: Record<string, 'success' | 'warning' | 'error' | 'info' | 'default'> = {
  queued: 'default',
  assigned: 'info',
  executing: 'warning',
  review_submitted: 'info',
  completed: 'success',
  failed: 'error',
  sleeping: 'default',
  needs_rework: 'warning',
}

const PRIORITY_COLORS: Record<string, string> = {
  critical: '#f44336',
  high: '#ff9800',
  medium: '#2196f3',
  low: '#4caf50',
}

type StatusFilter = string | null

const CARD_FILTERS: Record<string, string[]> = {
  'Active Agents': ['assigned', 'executing'],
  'Queued': ['queued'],
  'Pending Review': ['review_submitted'],
  'Completed': ['completed'],
  'Failed': ['failed'],
}

export default function Orchestrator() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const [status, setStatus] = useState<OrchestratorStatus | null>(null)
  const [investigations, setInvestigations] = useState<Investigation[]>([])
  const [loading, setLoading] = useState(true)
  const [toggling, setToggling] = useState(false)
  const [selectedInv, setSelectedInv] = useState<string | null>(null)
  const [detailData, setDetailData] = useState<any>(null)
  const [detailOpen, setDetailOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [scanning, setScanning] = useState(false)
  const [scanResult, setScanResult] = useState<string | null>(null)
  const [statusFilter, setStatusFilter] = useState<StatusFilter>(null)
  const [fileTab, setFileTab] = useState(0)
  const [fileContents, setFileContents] = useState<Record<string, string>>({})
  const [loadingFile, setLoadingFile] = useState(false)
  const [reviewNotes, setReviewNotes] = useState('')
  const [reviewing, setReviewing] = useState(false)
  const [savingMaxAgents, setSavingMaxAgents] = useState(false)
  // GH #79 — reasoning trace for the currently open investigation
  const [reasoningInteractions, setReasoningInteractions] = useState<any[]>([])
  const [reasoningLoading, setReasoningLoading] = useState(false)
  const [expandedInteraction, setExpandedInteraction] = useState<string | null>(null)

  const fetchData = useCallback(async () => {
    try {
      const [statusRes, invRes] = await Promise.all([
        orchestratorApi.getStatus(),
        orchestratorApi.listInvestigations(),
      ])
      setStatus(statusRes.data)
      setInvestigations(invRes.data.investigations || [])
      setError(null)
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to fetch orchestrator data')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchData()
    const interval = setInterval(fetchData, 10000)
    return () => clearInterval(interval)
  }, [fetchData])

  useEffect(() => {
    const highlightId = searchParams.get('highlight')
    if (highlightId && !loading) {
      handleViewInvestigation(highlightId)
    }
  }, [searchParams, loading])

  const handleToggle = async () => {
    if (!status) return
    setToggling(true)
    try {
      if (status.enabled) {
        await orchestratorApi.disable()
      } else {
        await orchestratorApi.enable()
      }
      await fetchData()
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Toggle failed')
    } finally {
      setToggling(false)
    }
  }

  const handleKillAll = async () => {
    try {
      await orchestratorApi.kill()
      await fetchData()
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Kill failed')
    }
  }

  const handleScanFindings = async () => {
    setScanning(true)
    setScanResult(null)
    try {
      const res = await orchestratorApi.scanFindings()
      setScanResult(res.data.message)
      await fetchData()
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Scan failed')
    } finally {
      setScanning(false)
    }
  }

  const handleViewInvestigation = async (id: string) => {
    try {
      const res = await orchestratorApi.getInvestigation(id)
      setDetailData(res.data)
      setSelectedInv(id)
      setDetailOpen(true)
      setFileTab(0)
      setFileContents({})
      setReviewNotes('')
      setExpandedInteraction(null)
      // GH #79 — load reasoning trace for this investigation
      setReasoningLoading(true)
      setReasoningInteractions([])
      try {
        const trace = await reasoningApi.listInvestigationInteractions(id, { limit: 500 })
        setReasoningInteractions(trace.interactions || [])
      } catch {
        setReasoningInteractions([])
      } finally {
        setReasoningLoading(false)
      }
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to load investigation')
    }
  }

  const loadFileContent = async (invId: string, filename: string) => {
    if (fileContents[filename]) return
    setLoadingFile(true)
    try {
      const res = await orchestratorApi.getInvestigationFile(invId, filename)
      setFileContents((prev) => ({ ...prev, [filename]: res.data.content || '(empty)' }))
    } catch {
      setFileContents((prev) => ({ ...prev, [filename]: '(failed to load)' }))
    } finally {
      setLoadingFile(false)
    }
  }

  const handleReview = async (invId: string, action: 'approve' | 'rework') => {
    setReviewing(true)
    try {
      await orchestratorApi.reviewInvestigation(invId, action, reviewNotes || undefined)
      await fetchData()
      const res = await orchestratorApi.getInvestigation(invId)
      setDetailData(res.data)
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Review failed')
    } finally {
      setReviewing(false)
    }
  }

  const handleCardClick = (label: string) => {
    if (label === 'Total Cost') {
      navigate('/analytics/cost')
      return
    }
    const filters = CARD_FILTERS[label]
    if (!filters) {
      setStatusFilter(null)
      return
    }
    const currentKey = Object.entries(CARD_FILTERS).find(
      ([, v]) => statusFilter && v.join(',') === statusFilter
    )?.[0]
    if (currentKey === label) {
      setStatusFilter(null)
    } else {
      setStatusFilter(filters.join(','))
    }
  }

  const STATUS_PRIORITY: Record<string, number> = {
    executing: 0,
    assigned: 1,
    needs_rework: 2,
    review_submitted: 3,
    queued: 4,
    completed: 5,
    failed: 6,
  }

  const filteredInvestigations = (statusFilter
    ? investigations.filter((inv) => statusFilter.split(',').includes(inv.status))
    : investigations
  ).slice().sort((a, b) => {
    const pa = STATUS_PRIORITY[a.status] ?? 4
    const pb = STATUS_PRIORITY[b.status] ?? 4
    if (pa !== pb) return pa - pb
    return new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
  })

  const handleWake = async (id: string) => {
    try {
      await orchestratorApi.wakeInvestigation(id)
      await fetchData()
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Wake failed')
    }
  }

  const handleKillInv = async (id: string) => {
    try {
      await orchestratorApi.killInvestigation(id)
      await fetchData()
      setDetailOpen(false)
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Kill failed')
    }
  }

  const handleMaxAgentsChange = async (newValue: number) => {
    if (newValue < 1 || newValue > 10) return
    setSavingMaxAgents(true)
    try {
      const current = (await configApi.getOrchestrator()).data
      await configApi.setOrchestrator({ ...current, max_concurrent_agents: newValue })
      await fetchData()
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to update agent limit')
    } finally {
      setSavingMaxAgents(false)
    }
  }

  if (loading) {
    return (
      <Box sx={{ p: 3, display: 'flex', justifyContent: 'center', alignItems: 'center', height: '50vh' }}>
        <CircularProgress />
      </Box>
    )
  }

  const costData = status?.cost || { total_cost_usd: 0, hourly_cost_usd: 0, hourly_budget_remaining: 0, per_investigation_limit: 0, active_cost_usd: 0 }

  return (
    <Box sx={{ p: 3 }}>
      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      {scanResult && (
        <Alert severity="success" sx={{ mb: 2 }} onClose={() => setScanResult(null)}>
          {scanResult}
        </Alert>
      )}

      {/* Header */}
      <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 3 }}>
        <Box>
          <Typography variant="h4" sx={{ fontWeight: 600 }}>
            Autonomous Operations
          </Typography>
          <Typography variant="body2" color="text.secondary">
            Master agent orchestrator and sub-agent investigation management
          </Typography>
        </Box>
        <Stack direction="row" spacing={2} alignItems="center">
          <FormControlLabel
            control={
              <Switch
                checked={status?.enabled || false}
                onChange={handleToggle}
                disabled={toggling}
                color="success"
              />
            }
            label={status?.enabled ? 'Enabled' : 'Disabled'}
          />
          <TextField
            label="Max Agents"
            type="number"
            size="small"
            value={status?.max_concurrent_agents ?? 3}
            onChange={(e) => handleMaxAgentsChange(Number(e.target.value))}
            disabled={savingMaxAgents}
            inputProps={{ min: 1, max: 10, style: { width: 40, textAlign: 'center' } }}
            sx={{ width: 110 }}
          />
          <Button
            variant="outlined"
            startIcon={scanning ? <CircularProgress size={16} /> : <ScanIcon />}
            onClick={handleScanFindings}
            disabled={scanning || !status?.enabled}
            size="small"
          >
            Scan Findings
          </Button>
          <Button
            variant="outlined"
            color="error"
            startIcon={<StopIcon />}
            onClick={handleKillAll}
            disabled={!status?.active_agents}
            size="small"
          >
            Kill All
          </Button>
          <IconButton onClick={fetchData} size="small">
            <RefreshIcon />
          </IconButton>
        </Stack>
      </Box>

      {/* Stats Cards */}
      <Grid container spacing={2} sx={{ mb: 3 }}>
        {[
          { icon: <AgentIcon sx={{ fontSize: 28, color: '#2196f3' }} />, value: String(status?.active_agents || 0), label: 'Active Agents' },
          { icon: <QueuedIcon sx={{ fontSize: 28, color: '#9e9e9e' }} />, value: String(status?.queued || 0), label: 'Queued' },
          { icon: <AlarmIcon sx={{ fontSize: 28, color: '#ff9800' }} />, value: String(status?.pending_review || 0), label: 'Pending Review' },
          { icon: <DoneIcon sx={{ fontSize: 28, color: '#4caf50' }} />, value: String(status?.completed || 0), label: 'Completed' },
          { icon: <ErrorIcon sx={{ fontSize: 28, color: '#f44336' }} />, value: String(status?.failed || 0), label: 'Failed' },
          { icon: <CostIcon sx={{ fontSize: 28, color: '#9c27b0' }} />, value: `$${costData.total_cost_usd?.toFixed(2) || '0.00'}`, label: 'Total Cost' },
        ].map((card) => {
          const filterKey = CARD_FILTERS[card.label]?.join(',')
          const isActive = statusFilter != null && filterKey === statusFilter
          return (
            <Grid item xs={6} sm={4} md={2} key={card.label}>
              <Card
                sx={{
                  height: '100%',
                  cursor: 'pointer',
                  border: isActive ? '2px solid' : '2px solid transparent',
                  borderColor: isActive ? 'primary.main' : 'transparent',
                  transition: 'border-color 0.2s',
                  '&:hover': { borderColor: 'primary.dark' },
                }}
                onClick={() => handleCardClick(card.label)}
              >
                <CardContent sx={{ textAlign: 'center', py: 2, px: 1.5, '&:last-child': { pb: 2 } }}>
                  {card.icon}
                  <Typography variant="h5" sx={{ fontWeight: 700, mt: 0.5, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                    {card.value}
                  </Typography>
                  <Typography variant="caption" color="text.secondary" sx={{ whiteSpace: 'nowrap' }}>
                    {card.label}
                  </Typography>
                </CardContent>
              </Card>
            </Grid>
          )
        })}
      </Grid>

      {/* Cost Bar */}
      {status?.enabled && (
        <Paper sx={{ p: 2, mb: 3 }}>
          <Box sx={{ display: 'flex', justifyContent: 'space-between', mb: 1 }}>
            <Typography variant="body2" color="text.secondary">
              Hourly Budget: ${costData.hourly_cost_usd?.toFixed(2)} / ${(costData.hourly_cost_usd + costData.hourly_budget_remaining)?.toFixed(2)}
            </Typography>
            <Typography variant="body2" color="text.secondary">
              ${costData.hourly_budget_remaining?.toFixed(2)} remaining
            </Typography>
          </Box>
          <LinearProgress
            variant="determinate"
            value={
              costData.hourly_budget_remaining > 0
                ? (costData.hourly_cost_usd / (costData.hourly_cost_usd + costData.hourly_budget_remaining)) * 100
                : 100
            }
            sx={{ height: 8, borderRadius: 4 }}
            color={costData.hourly_budget_remaining < 5 ? 'error' : 'primary'}
          />
        </Paper>
      )}

      {/* Investigations Table */}
      <Paper sx={{ overflow: 'hidden' }}>
        <Box sx={{ p: 2, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <Stack direction="row" spacing={1} alignItems="center">
            <Typography variant="h6">Investigations</Typography>
            {statusFilter && (
              <Chip
                label={`Showing: ${statusFilter.split(',').join(', ')}`}
                size="small"
                onDelete={() => setStatusFilter(null)}
                deleteIcon={<CloseIcon />}
                color="primary"
                variant="outlined"
              />
            )}
          </Stack>
          <Typography variant="body2" color="text.secondary">
            {filteredInvestigations.length}{statusFilter ? ` of ${investigations.length}` : ''} total
          </Typography>
        </Box>
        <Divider />
        <TableContainer sx={{ maxHeight: 600 }}>
          <Table stickyHeader size="small">
            <TableHead>
              <TableRow>
                <TableCell>ID</TableCell>
                <TableCell>Skill</TableCell>
                <TableCell>Priority</TableCell>
                <TableCell>Status</TableCell>
                <TableCell>Activity</TableCell>
                <TableCell>Iterations</TableCell>
                <TableCell>Cost</TableCell>
                <TableCell>Created</TableCell>
                <TableCell align="right">Actions</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {filteredInvestigations.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={9} align="center" sx={{ py: 4 }}>
                    <Typography color="text.secondary">
                      {statusFilter ? 'No investigations match this filter' : 'No investigations yet'}
                    </Typography>
                  </TableCell>
                </TableRow>
              ) : (
                filteredInvestigations.map((inv) => (
                  <TableRow
                    key={inv.investigation_id}
                    hover
                    sx={{
                      bgcolor:
                        inv.status === 'failed' ? 'rgba(244, 67, 54, 0.08)'
                        : inv.status === 'review_submitted' ? 'rgba(255, 152, 0, 0.08)'
                        : inv.status === 'completed' ? 'rgba(76, 175, 80, 0.08)'
                        : undefined,
                    }}
                  >
                    <TableCell>
                      <Typography variant="body2" sx={{ fontFamily: 'monospace', fontSize: '0.75rem' }}>
                        {inv.investigation_id}
                      </Typography>
                    </TableCell>
                    <TableCell>
                      <Chip label={inv.skill_id} size="small" variant="outlined" />
                    </TableCell>
                    <TableCell>
                      <Chip
                        label={inv.priority}
                        size="small"
                        sx={{
                          bgcolor: PRIORITY_COLORS[inv.priority] || '#9e9e9e',
                          color: 'white',
                          fontWeight: 600,
                          fontSize: '0.7rem',
                        }}
                      />
                    </TableCell>
                    <TableCell>
                      <Chip
                        label={inv.status}
                        size="small"
                        color={STATUS_COLORS[inv.status] || 'default'}
                      />
                    </TableCell>
                    <TableCell>
                      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75 }}>
                        {(inv.status === 'assigned' || inv.status === 'executing') && (
                          <CircularProgress size={14} thickness={5} />
                        )}
                        <Typography variant="caption" sx={{ maxWidth: 160, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {inv.current_activity || inv.status}
                        </Typography>
                      </Box>
                    </TableCell>
                    <TableCell>
                      <Typography variant="body2">{inv.iteration_count}</Typography>
                    </TableCell>
                    <TableCell>
                      <Typography variant="body2">${inv.cost_usd?.toFixed(3)}</Typography>
                    </TableCell>
                    <TableCell>
                      <Typography variant="caption">
                        {inv.created_at ? new Date(inv.created_at).toLocaleString() : '-'}
                      </Typography>
                    </TableCell>
                    <TableCell align="right">
                      <Tooltip title="View Details">
                        <IconButton size="small" onClick={() => handleViewInvestigation(inv.investigation_id)}>
                          <ViewIcon fontSize="small" />
                        </IconButton>
                      </Tooltip>
                      {['sleeping', 'needs_rework', 'failed', 'completed'].includes(inv.status) ? (
                        <Tooltip title="Restart">
                          <IconButton size="small" color="primary" onClick={() => handleWake(inv.investigation_id)}>
                            <PlayIcon fontSize="small" />
                          </IconButton>
                        </Tooltip>
                      ) : null}
                      {inv.status === 'executing' || inv.status === 'assigned' ? (
                        <Tooltip title="Kill">
                          <IconButton size="small" color="error" onClick={() => handleKillInv(inv.investigation_id)}>
                            <KillIcon fontSize="small" />
                          </IconButton>
                        </Tooltip>
                      ) : null}
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </TableContainer>
      </Paper>

      {/* Investigation Detail Dialog */}
      <Dialog open={detailOpen} onClose={() => setDetailOpen(false)} maxWidth="lg" fullWidth>
        <DialogTitle sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span>Investigation: {selectedInv}</span>
          <Tooltip title="View AI Decisions for this investigation">
            <Button
              size="small"
              variant="outlined"
              startIcon={<DecisionsIcon />}
              onClick={() => navigate(`/ai-decisions?agent_id=orchestrator&investigation_id=${selectedInv}`)}
            >
              AI Decisions
            </Button>
          </Tooltip>
        </DialogTitle>
        <DialogContent>
          {detailData && (
            <Box>
              <Grid container spacing={2} sx={{ mb: 2 }}>
                <Grid item xs={3}>
                  <Typography variant="caption" color="text.secondary">Status</Typography>
                  <Box><Chip label={detailData.status} size="small" color={STATUS_COLORS[detailData.status] || 'default'} /></Box>
                </Grid>
                <Grid item xs={3}>
                  <Typography variant="caption" color="text.secondary">Skill</Typography>
                  <Typography variant="body2">{detailData.skill_id}</Typography>
                </Grid>
                <Grid item xs={3}>
                  <Typography variant="caption" color="text.secondary">Iterations</Typography>
                  <Typography variant="body2">{detailData.iteration_count}</Typography>
                </Grid>
                <Grid item xs={3}>
                  <Typography variant="caption" color="text.secondary">Cost</Typography>
                  <Typography variant="body2">${detailData.cost_usd?.toFixed(4)}</Typography>
                </Grid>
              </Grid>

              {detailData.summary && (
                <Alert severity="info" sx={{ mb: 2 }}>
                  <Typography variant="subtitle2">Summary</Typography>
                  <Typography variant="body2">{detailData.summary}</Typography>
                </Alert>
              )}

              {detailData.master_review_notes && (
                <Alert severity="warning" sx={{ mb: 2 }}>
                  <Typography variant="subtitle2">Master Review Notes</Typography>
                  <Typography variant="body2">{detailData.master_review_notes}</Typography>
                </Alert>
              )}

              {detailData.last_error && (
                <Alert severity="error" sx={{ mb: 2 }}>
                  <Typography variant="body2">{detailData.last_error}</Typography>
                </Alert>
              )}

              {/* Proposed Actions */}
              {detailData.proposed_actions && detailData.proposed_actions.length > 0 && (
                <Box sx={{ mb: 2 }}>
                  <Typography variant="subtitle2" sx={{ mb: 1 }}>Proposed Actions</Typography>
                  <Paper variant="outlined" sx={{ overflow: 'hidden' }}>
                    <Table size="small">
                      <TableHead>
                        <TableRow>
                          <TableCell>Action</TableCell>
                          <TableCell>Target</TableCell>
                          <TableCell>Reason</TableCell>
                        </TableRow>
                      </TableHead>
                      <TableBody>
                        {detailData.proposed_actions.map((action: any, i: number) => (
                          <TableRow key={i}>
                            <TableCell>
                              <Chip label={action.action || action.type || 'action'} size="small" color="info" variant="outlined" />
                            </TableCell>
                            <TableCell>
                              <Typography variant="body2" sx={{ fontFamily: 'monospace', fontSize: '0.75rem' }}>
                                {action.target || action.entity || '-'}
                              </Typography>
                            </TableCell>
                            <TableCell>
                              <Typography variant="body2">{action.reason || action.description || '-'}</Typography>
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </Paper>
                </Box>
              )}

              {/* Human Review Controls */}
              {detailData.status === 'review_submitted' && (
                <Paper variant="outlined" sx={{ p: 2, mb: 2, bgcolor: 'action.hover' }}>
                  <Typography variant="subtitle2" sx={{ mb: 1.5 }}>Human Review</Typography>
                  <TextField
                    label="Review notes (optional)"
                    multiline
                    rows={2}
                    fullWidth
                    size="small"
                    value={reviewNotes}
                    onChange={(e) => setReviewNotes(e.target.value)}
                    sx={{ mb: 1.5 }}
                  />
                  <Stack direction="row" spacing={1}>
                    <Button
                      variant="contained"
                      color="success"
                      startIcon={reviewing ? <CircularProgress size={16} /> : <ApproveIcon />}
                      disabled={reviewing}
                      onClick={() => handleReview(detailData.investigation_id, 'approve')}
                    >
                      Approve
                    </Button>
                    <Button
                      variant="outlined"
                      color="warning"
                      startIcon={reviewing ? <CircularProgress size={16} /> : <ReworkIcon />}
                      disabled={reviewing}
                      onClick={() => handleReview(detailData.investigation_id, 'rework')}
                    >
                      Request Rework
                    </Button>
                  </Stack>
                </Paper>
              )}

              {/* File Content Tabs */}
              {(detailData.files || []).length > 0 && (
                <Box sx={{ mb: 2 }}>
                  <Typography variant="subtitle2" sx={{ mb: 1 }}>Investigation Files</Typography>
                  <Paper variant="outlined">
                    <Tabs
                      value={fileTab}
                      onChange={(_, v) => {
                        setFileTab(v)
                        const files = detailData.files || []
                        if (files[v] && selectedInv) {
                          loadFileContent(selectedInv, files[v])
                        }
                      }}
                      variant="scrollable"
                      scrollButtons="auto"
                      sx={{ borderBottom: 1, borderColor: 'divider' }}
                    >
                      {(detailData.files || []).map((f: string) => (
                        <Tab key={f} label={f} sx={{ fontFamily: 'monospace', fontSize: '0.75rem', textTransform: 'none' }} />
                      ))}
                    </Tabs>
                    <Box sx={{ p: 1.5, maxHeight: 300, overflow: 'auto' }}>
                      {loadingFile ? (
                        <Box sx={{ display: 'flex', justifyContent: 'center', py: 2 }}><CircularProgress size={20} /></Box>
                      ) : (
                        <Typography
                          variant="body2"
                          component="pre"
                          sx={{ fontFamily: 'monospace', fontSize: '0.75rem', whiteSpace: 'pre-wrap', wordBreak: 'break-word', m: 0 }}
                        >
                          {fileContents[(detailData.files || [])[fileTab]] || 'Click a tab to load file content'}
                        </Typography>
                      )}
                    </Box>
                  </Paper>
                </Box>
              )}

              {detailData.recent_log && detailData.recent_log.length > 0 && (
                <>
                  <Typography variant="subtitle2" sx={{ mb: 1 }}>Recent Activity</Typography>
                  <Paper variant="outlined" sx={{ p: 1, maxHeight: 200, overflow: 'auto', bgcolor: '#1a1a2e' }}>
                    {detailData.recent_log.map((entry: any, i: number) => (
                      <Typography key={i} variant="caption" sx={{ display: 'block', fontFamily: 'monospace', color: '#e0e0e0', lineHeight: 1.6 }}>
                        <span style={{ color: '#888' }}>{entry.ts?.split('T')[1]?.split('.')[0] || ''}</span>{' '}
                        <span style={{ color: entry.event === 'error' ? '#f44336' : '#4caf50' }}>{entry.event}</span>{' '}
                        {entry.iteration ? `iter=${entry.iteration}` : ''}
                        {entry.cost_usd ? ` $${entry.cost_usd}` : ''}
                        {entry.reason || ''}
                      </Typography>
                    ))}
                  </Paper>
                </>
              )}

              {/* GH #79 — Reasoning trace per iteration */}
              <Box sx={{ mt: 2 }}>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 1 }}>
                  <DecisionsIcon sx={{ fontSize: 16 }} />
                  <Typography variant="subtitle2">Reasoning Trace</Typography>
                  {reasoningInteractions.length > 0 && (
                    <Chip size="small" label={`${reasoningInteractions.length} call${reasoningInteractions.length === 1 ? '' : 's'}`} />
                  )}
                </Box>
                {reasoningLoading ? (
                  <Box sx={{ display: 'flex', justifyContent: 'center', py: 2 }}><CircularProgress size={18} /></Box>
                ) : reasoningInteractions.length === 0 ? (
                  <Typography variant="caption" color="text.secondary">
                    No reasoning traces recorded for this investigation.
                  </Typography>
                ) : (
                  <Paper variant="outlined" sx={{ maxHeight: 420, overflow: 'auto' }}>
                    {reasoningInteractions.map((it: any, idx: number) => {
                      const expanded = expandedInteraction === it.interaction_id
                      return (
                        <Box key={it.interaction_id} sx={{ borderBottom: idx < reasoningInteractions.length - 1 ? 1 : 0, borderColor: 'divider' }}>
                          <Box
                            sx={{ px: 1.5, py: 1, display: 'flex', alignItems: 'center', gap: 1, cursor: 'pointer', '&:hover': { bgcolor: 'action.hover' } }}
                            onClick={() => setExpandedInteraction(expanded ? null : it.interaction_id)}
                          >
                            <Typography variant="caption" sx={{ fontFamily: 'monospace', minWidth: 72 }}>
                              #{idx + 1}
                            </Typography>
                            <Typography variant="caption" sx={{ fontFamily: 'monospace', color: 'text.secondary', minWidth: 80 }}>
                              {it.created_at ? new Date(it.created_at).toLocaleTimeString() : ''}
                            </Typography>
                            <Typography variant="caption" sx={{ flex: 1 }}>
                              {it.stop_reason || '—'}
                              {it.thinking_content && ` · 💭 ${it.thinking_content.length}c`}
                              {Array.isArray(it.tool_calls) && it.tool_calls.length > 0 && ` · 🔧 ${it.tool_calls.length}`}
                            </Typography>
                            <Typography variant="caption" sx={{ color: 'text.secondary' }}>
                              {it.input_tokens}/{it.output_tokens} tok · ${Number(it.cost_usd || 0).toFixed(4)}
                            </Typography>
                          </Box>
                          {expanded && (
                            <Box sx={{ px: 1.5, py: 1, bgcolor: 'action.hover' }}>
                              {it.thinking_content && (
                                <Box sx={{ mb: 1.5 }}>
                                  <Typography variant="caption" sx={{ fontWeight: 600, display: 'block', mb: 0.5, color: 'info.main' }}>
                                    💭 Thinking ({it.thinking_content.length} chars)
                                  </Typography>
                                  <Typography variant="body2" component="pre" sx={{ whiteSpace: 'pre-wrap', fontFamily: 'monospace', fontSize: '0.7rem', m: 0 }}>
                                    {it.thinking_content}
                                  </Typography>
                                </Box>
                              )}
                              {it.response_content && (
                                <Box sx={{ mb: 1.5 }}>
                                  <Typography variant="caption" sx={{ fontWeight: 600, display: 'block', mb: 0.5 }}>Response</Typography>
                                  <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap', fontSize: '0.8rem' }}>
                                    {it.response_content}
                                  </Typography>
                                </Box>
                              )}
                              {Array.isArray(it.tool_calls) && it.tool_calls.length > 0 && (
                                <Box sx={{ mb: 1 }}>
                                  <Typography variant="caption" sx={{ fontWeight: 600, display: 'block', mb: 0.5 }}>Tool Calls</Typography>
                                  {it.tool_calls.map((tc: any, ti: number) => (
                                    <Box key={ti} sx={{ mb: 0.5, pl: 1, borderLeft: 2, borderColor: 'warning.main' }}>
                                      <Typography variant="caption" sx={{ fontFamily: 'monospace', fontWeight: 600 }}>🔧 {tc.name}</Typography>
                                      <Typography variant="body2" component="pre" sx={{ fontFamily: 'monospace', fontSize: '0.7rem', whiteSpace: 'pre-wrap', m: 0 }}>
                                        {JSON.stringify(tc.input, null, 2)}
                                      </Typography>
                                    </Box>
                                  ))}
                                </Box>
                              )}
                              {Array.isArray(it.tool_results) && it.tool_results.length > 0 && (
                                <Box>
                                  <Typography variant="caption" sx={{ fontWeight: 600, display: 'block', mb: 0.5 }}>Tool Results (input)</Typography>
                                  {it.tool_results.map((tr: any, ri: number) => (
                                    <Box key={ri} sx={{ mb: 0.5, pl: 1, borderLeft: 2, borderColor: tr.is_error ? 'error.main' : 'success.main' }}>
                                      <Typography variant="caption" sx={{ fontFamily: 'monospace' }}>
                                        {tr.is_error ? '❌' : '✅'} {tr.tool_use_id}
                                      </Typography>
                                      <Typography variant="body2" component="pre" sx={{ fontFamily: 'monospace', fontSize: '0.7rem', whiteSpace: 'pre-wrap', m: 0 }}>
                                        {typeof tr.content === 'string' ? tr.content : JSON.stringify(tr.content, null, 2)}
                                      </Typography>
                                    </Box>
                                  ))}
                                </Box>
                              )}
                            </Box>
                          )}
                        </Box>
                      )
                    })}
                  </Paper>
                )}
              </Box>
            </Box>
          )}
        </DialogContent>
        <DialogActions>
          {detailData?.status === 'executing' && (
            <Button color="error" onClick={() => handleKillInv(detailData.investigation_id)}>
              Kill Agent
            </Button>
          )}
          {(detailData?.status === 'sleeping' || detailData?.status === 'needs_rework') && (
            <Button color="primary" onClick={() => handleWake(detailData.investigation_id)}>
              Wake Agent
            </Button>
          )}
          <Button onClick={() => setDetailOpen(false)}>Close</Button>
        </DialogActions>
      </Dialog>
    </Box>
  )
}
