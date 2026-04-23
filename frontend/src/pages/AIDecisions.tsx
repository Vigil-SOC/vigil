import { useState, useEffect } from 'react'
import { useSearchParams, useNavigate } from 'react-router-dom'
import {
  Box,
  Typography,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Chip,
  Button,
  Grid,
  CircularProgress,
  Alert,
  Tabs,
  Tab,
  LinearProgress,
  Stack,
  IconButton,
  Tooltip,
  Select,
  MenuItem,
  FormControl,
  alpha,
  useTheme,
  Link,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  TextField,
} from '@mui/material'
import {
  Feedback as FeedbackIcon,
  CheckCircle as CheckIcon,
  Refresh as RefreshIcon,
  Analytics as AnalyticsIcon,
  TrendingUp as TrendingUpIcon,
  Timer as TimerIcon,
  ThumbUp as ThumbUpIcon,
  ThumbDown as ThumbDownIcon,
} from '@mui/icons-material'
import { aiDecisionsApi, approvalsApi } from '../services/api'
import AIDecisionFeedback from '../components/ai/AIDecisionFeedback'
import { StatCard, StatusBadge } from '../components/ui'
import { severityColors } from '../theme'

interface TabPanelProps {
  children?: React.ReactNode
  index: number
  value: number
}

function TabPanel({ children, value, index }: TabPanelProps) {
  return (
    <div hidden={value !== index}>
      {value === index && <Box sx={{ pt: 2 }}>{children}</Box>}
    </div>
  )
}

export default function AIDecisionsPage() {
  const theme = useTheme()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const [tabValue, setTabValue] = useState(0)
  const [loading, setLoading] = useState(false)
  const [pendingDecisions, setPendingDecisions] = useState<any[]>([])
  const [allDecisions, setAllDecisions] = useState<any[]>([])
  const [stats, setStats] = useState<any>(null)
  const [selectedDecision, setSelectedDecision] = useState<any>(null)
  const [feedbackDialogOpen, setFeedbackDialogOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [filterAgent, setFilterAgent] = useState<string>('all')
  const [filterFeedback, setFilterFeedback] = useState<string>('all')
  const [hoveredRow, setHoveredRow] = useState<string | null>(null)
  const [investigationFilter, setInvestigationFilter] = useState<string | null>(null)

  // #128 — pending workflow phase / daemon-action approvals
  const [pendingApprovals, setPendingApprovals] = useState<any[]>([])
  const [approvalActing, setApprovalActing] = useState<string | null>(null)
  const [rejectDialog, setRejectDialog] = useState<{ open: boolean; action: any | null }>({
    open: false,
    action: null,
  })
  const [rejectReason, setRejectReason] = useState('')

  useEffect(() => {
    const agentParam = searchParams.get('agent_id')
    const invParam = searchParams.get('investigation_id')
    if (agentParam) {
      setFilterAgent(agentParam)
      setTabValue(1)
    }
    if (invParam) {
      setInvestigationFilter(invParam)
      setTabValue(1)
    }
  }, [])

  useEffect(() => { loadData() }, [filterAgent, filterFeedback, investigationFilter])

  const loadData = async () => {
    setLoading(true)
    setError(null)
    try {
      const pendingResponse = await aiDecisionsApi.getPendingFeedback(50)
      setPendingDecisions(pendingResponse.data)
      const filters: any = { limit: 100 }
      if (filterAgent !== 'all') filters.agent_id = filterAgent
      if (filterFeedback === 'pending') filters.has_feedback = false
      if (filterFeedback === 'completed') filters.has_feedback = true
      if (investigationFilter) filters.workflow_id = investigationFilter
      const allResponse = await aiDecisionsApi.list(filters)
      setAllDecisions(allResponse.data)
      const statsParams = filterAgent !== 'all' ? { agent_id: filterAgent } : {}
      const statsResponse = await aiDecisionsApi.getStats(statsParams)
      setStats(statsResponse.data)
      const approvalsResponse = await approvalsApi.listPending()
      setPendingApprovals(approvalsResponse.data?.actions || [])
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to load AI decisions')
    } finally {
      setLoading(false)
    }
  }

  const reloadApprovals = async () => {
    try {
      const response = await approvalsApi.listPending()
      setPendingApprovals(response.data?.actions || [])
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to reload approvals')
    }
  }

  const handleApprove = async (action: any) => {
    setApprovalActing(action.action_id)
    try {
      await approvalsApi.approve(action.action_id)
      await reloadApprovals()
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to approve action')
    } finally {
      setApprovalActing(null)
    }
  }

  const openRejectDialog = (action: any) => {
    setRejectDialog({ open: true, action })
    setRejectReason('')
  }

  const closeRejectDialog = () => {
    setRejectDialog({ open: false, action: null })
    setRejectReason('')
  }

  const submitReject = async () => {
    const action = rejectDialog.action
    if (!action || !rejectReason.trim()) return
    setApprovalActing(action.action_id)
    try {
      await approvalsApi.reject(action.action_id, rejectReason.trim())
      closeRejectDialog()
      await reloadApprovals()
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to reject action')
    } finally {
      setApprovalActing(null)
    }
  }

  const clearInvestigationFilter = () => {
    setInvestigationFilter(null)
    setSearchParams({})
  }

  const handleProvideFeedback = (decision: any) => {
    setSelectedDecision(decision)
    setFeedbackDialogOpen(true)
  }

  const getAgentDisplayName = (agentId: string) => {
    const names: Record<string, string> = {
      triage: 'Triage', investigation: 'Investigation', threat_hunter: 'Threat Hunter',
      correlation: 'Correlation', auto_responder: 'Auto-Response', reporting: 'Reporting',
      mitre_analyst: 'MITRE', forensics: 'Forensics', threat_intel: 'Threat Intel',
      compliance: 'Compliance', malware_analyst: 'Malware', network_analyst: 'Network',
      orchestrator: 'Orchestrator',
    }
    return names[agentId] || agentId
  }

  const getInvestigationId = (decision: any): string | null => {
    if (decision.workflow_id) return decision.workflow_id
    if (decision.decision_metadata?.investigation_id) return decision.decision_metadata.investigation_id
    return null
  }

  const getConfidenceColor = (confidence: number) => {
    if (confidence >= 0.85) return severityColors.low
    if (confidence >= 0.7) return severityColors.medium
    return severityColors.high
  }

  const formatDateTime = (timestamp: string) => new Date(timestamp).toLocaleString()
  const formatTimeSaved = (minutes: number) => {
    if (minutes < 60) return `${minutes}m`
    const hours = Math.floor(minutes / 60)
    const mins = minutes % 60
    return mins > 0 ? `${hours}h ${mins}m` : `${hours}h`
  }

  return (
    <Box>
      <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', mb: 3 }}>
        <Box>
          <Typography variant="h5" sx={{ fontWeight: 700, mb: 0.5 }}>AI Decisions</Typography>
          <Typography variant="body2" color="text.secondary">Review and provide feedback for AI decisions</Typography>
        </Box>
        <IconButton size="small" onClick={loadData} disabled={loading}>
          <RefreshIcon sx={{ fontSize: 20 }} />
        </IconButton>
      </Box>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      {loading && !stats && (
        <Box display="flex" justifyContent="center" p={4}>
          <CircularProgress size={32} />
        </Box>
      )}

      {stats && (
        <Grid container spacing={2} sx={{ mb: 3 }}>
          <Grid item xs={6} md={3}>
            <StatCard
              title="Total Decisions"
              value={stats.total_decisions}
              icon={<AnalyticsIcon />}
              color={theme.palette.primary.main}
            />
          </Grid>
          <Grid item xs={6} md={3}>
            <StatCard
              title="Feedback Rate"
              value={`${(stats.feedback_rate * 100).toFixed(0)}%`}
              subtitle={`${stats.total_with_feedback} reviewed`}
              icon={<FeedbackIcon />}
              color={theme.palette.info.main}
            />
          </Grid>
          <Grid item xs={6} md={3}>
            <StatCard
              title="Agreement Rate"
              value={`${(stats.agreement_rate * 100).toFixed(0)}%`}
              subtitle={`${(stats.avg_accuracy_grade * 100).toFixed(0)}% accuracy`}
              icon={<TrendingUpIcon />}
              color={theme.palette.success.main}
            />
          </Grid>
          <Grid item xs={6} md={3}>
            <StatCard
              title="Time Saved"
              value={`${stats.total_time_saved_hours.toFixed(1)}h`}
              subtitle={`Last ${stats.period_days} days`}
              icon={<TimerIcon />}
              color={theme.palette.warning.main}
            />
          </Grid>
        </Grid>
      )}

      <Box sx={{ bgcolor: 'background.paper', borderRadius: 3, border: 1, borderColor: 'divider' }}>
        <Box sx={{ borderBottom: 1, borderColor: 'divider', px: 2 }}>
          <Tabs value={tabValue} onChange={(_, v) => setTabValue(v)}>
            <Tab label={`Pending (${pendingDecisions.length})`} sx={{ minHeight: 48 }} />
            <Tab label="All Decisions" sx={{ minHeight: 48 }} />
            <Tab label="Analytics" sx={{ minHeight: 48 }} />
            <Tab
              label={`Pending Approvals (${pendingApprovals.length})`}
              sx={{ minHeight: 48 }}
            />
          </Tabs>
        </Box>

        <Box sx={{ p: 2 }}>
          <TabPanel value={tabValue} index={0}>
            {pendingDecisions.length === 0 ? (
              <Box sx={{ py: 6, textAlign: 'center' }}>
                <CheckIcon sx={{ fontSize: 48, color: 'success.main', mb: 1 }} />
                <Typography color="text.secondary">All decisions have been reviewed</Typography>
              </Box>
            ) : (
              <TableContainer>
                <Table size="small">
                  <TableHead>
                    <TableRow>
                      <TableCell>Agent</TableCell>
                      <TableCell>Decision Type</TableCell>
                      <TableCell>Confidence</TableCell>
                      <TableCell>Recommended Action</TableCell>
                      <TableCell>Time</TableCell>
                      <TableCell align="right" sx={{ width: 100 }}>Actions</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {pendingDecisions.map((decision) => (
                      <TableRow
                        key={decision.id}
                        onMouseEnter={() => setHoveredRow(decision.id)}
                        onMouseLeave={() => setHoveredRow(null)}
                      >
                        <TableCell>
                          <Chip label={getAgentDisplayName(decision.agent_id)} size="small" variant="outlined" />
                        </TableCell>
                        <TableCell>
                          <Typography variant="body2">{decision.decision_type}</Typography>
                        </TableCell>
                        <TableCell>
                          <Chip
                            label={`${(decision.confidence_score * 100).toFixed(0)}%`}
                            size="small"
                            sx={{
                              bgcolor: alpha(getConfidenceColor(decision.confidence_score), 0.15),
                              color: getConfidenceColor(decision.confidence_score),
                              fontWeight: 600,
                            }}
                          />
                        </TableCell>
                        <TableCell>
                          <Tooltip title={decision.recommended_action}>
                            <Typography variant="body2" noWrap sx={{ maxWidth: 200 }}>
                              {decision.recommended_action}
                            </Typography>
                          </Tooltip>
                        </TableCell>
                        <TableCell>
                          <Typography variant="caption" color="text.secondary">
                            {formatDateTime(decision.timestamp)}
                          </Typography>
                        </TableCell>
                        <TableCell align="right">
                          <Button
                            size="small"
                            variant="contained"
                            startIcon={<FeedbackIcon sx={{ fontSize: 16 }} />}
                            onClick={() => handleProvideFeedback(decision)}
                            sx={{
                              opacity: hoveredRow === decision.id ? 1 : 0.7,
                              transition: 'opacity 0.15s',
                            }}
                          >
                            Feedback
                          </Button>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableContainer>
            )}
          </TabPanel>

          <TabPanel value={tabValue} index={1}>
            {investigationFilter && (
              <Alert
                severity="info"
                sx={{ mb: 2 }}
                action={
                  <Button size="small" onClick={clearInvestigationFilter}>
                    Clear
                  </Button>
                }
              >
                Filtered to investigation: <strong>{investigationFilter}</strong>
              </Alert>
            )}
            <Box sx={{ mb: 2, display: 'flex', gap: 2 }}>
              <FormControl size="small" sx={{ minWidth: 120 }}>
                <Select value={filterAgent} onChange={(e) => setFilterAgent(e.target.value)} displayEmpty>
                  <MenuItem value="all">All Agents</MenuItem>
                  <MenuItem value="orchestrator">Orchestrator</MenuItem>
                  <MenuItem value="triage">Triage</MenuItem>
                  <MenuItem value="investigation">Investigation</MenuItem>
                  <MenuItem value="auto_responder">Auto-Response</MenuItem>
                  <MenuItem value="threat_hunter">Threat Hunter</MenuItem>
                </Select>
              </FormControl>
              <FormControl size="small" sx={{ minWidth: 120 }}>
                <Select value={filterFeedback} onChange={(e) => setFilterFeedback(e.target.value)} displayEmpty>
                  <MenuItem value="all">All Status</MenuItem>
                  <MenuItem value="pending">Pending</MenuItem>
                  <MenuItem value="completed">Completed</MenuItem>
                </Select>
              </FormControl>
            </Box>

            <TableContainer>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>Agent</TableCell>
                    <TableCell>Type</TableCell>
                    <TableCell>Investigation</TableCell>
                    <TableCell>Confidence</TableCell>
                    <TableCell>Human Decision</TableCell>
                    <TableCell>Outcome</TableCell>
                    <TableCell>Time Saved</TableCell>
                    <TableCell>Time</TableCell>
                    <TableCell align="right" sx={{ width: 60 }}>Actions</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {allDecisions.map((decision) => (
                    <TableRow
                      key={decision.id}
                      onMouseEnter={() => setHoveredRow(decision.id)}
                      onMouseLeave={() => setHoveredRow(null)}
                    >
                      <TableCell>
                        <Chip label={getAgentDisplayName(decision.agent_id)} size="small" variant="outlined" />
                      </TableCell>
                      <TableCell>
                        <Typography variant="body2">{decision.decision_type}</Typography>
                      </TableCell>
                      <TableCell>
                        {(() => {
                          const invId = getInvestigationId(decision)
                          if (!invId) return <Typography variant="caption" color="text.secondary">-</Typography>
                          return (
                            <Tooltip title="View in Orchestrator">
                              <Link
                                component="button"
                                variant="body2"
                                sx={{ fontFamily: 'monospace', fontSize: '0.7rem', cursor: 'pointer' }}
                                onClick={() => navigate(`/orchestrator?highlight=${invId}`)}
                              >
                                {invId.slice(0, 12)}...
                              </Link>
                            </Tooltip>
                          )
                        })()}
                      </TableCell>
                      <TableCell>
                        <Chip
                          label={`${(decision.confidence_score * 100).toFixed(0)}%`}
                          size="small"
                          sx={{
                            bgcolor: alpha(getConfidenceColor(decision.confidence_score), 0.15),
                            color: getConfidenceColor(decision.confidence_score),
                            fontWeight: 600,
                          }}
                        />
                      </TableCell>
                      <TableCell>
                        {decision.human_decision ? (
                          <StatusBadge status={decision.human_decision} />
                        ) : (
                          <Chip label="Pending" size="small" variant="outlined" />
                        )}
                      </TableCell>
                      <TableCell>
                        {decision.actual_outcome ? (
                          <Typography variant="body2">{decision.actual_outcome.replace('_', ' ')}</Typography>
                        ) : '-'}
                      </TableCell>
                      <TableCell>
                        {decision.time_saved_minutes ? formatTimeSaved(decision.time_saved_minutes) : '-'}
                      </TableCell>
                      <TableCell>
                        <Typography variant="caption" color="text.secondary">
                          {formatDateTime(decision.timestamp)}
                        </Typography>
                      </TableCell>
                      <TableCell align="right">
                        {!decision.human_decision && (
                          <IconButton
                            size="small"
                            onClick={() => handleProvideFeedback(decision)}
                            sx={{ opacity: hoveredRow === decision.id ? 1 : 0 }}
                          >
                            <FeedbackIcon sx={{ fontSize: 18 }} />
                          </IconButton>
                        )}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableContainer>
          </TabPanel>

          <TabPanel value={tabValue} index={3}>
            {pendingApprovals.length === 0 ? (
              <Box sx={{ py: 6, textAlign: 'center' }}>
                <CheckIcon sx={{ fontSize: 48, color: 'success.main', mb: 1 }} />
                <Typography color="text.secondary">
                  No pending approvals. Workflow phases that require approval will appear here.
                </Typography>
              </Box>
            ) : (
              <TableContainer>
                <Table size="small">
                  <TableHead>
                    <TableRow>
                      <TableCell>Action</TableCell>
                      <TableCell>Target / Run</TableCell>
                      <TableCell>Phase</TableCell>
                      <TableCell>Reason</TableCell>
                      <TableCell>Created</TableCell>
                      <TableCell align="right" sx={{ width: 200 }}>
                        Decision
                      </TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {pendingApprovals.map((action) => (
                      <TableRow key={action.action_id}>
                        <TableCell>
                          <Typography variant="body2" sx={{ fontWeight: 600 }}>
                            {action.title}
                          </Typography>
                          <Typography
                            variant="caption"
                            color="text.secondary"
                            sx={{ display: 'block', maxWidth: 320 }}
                          >
                            {action.description}
                          </Typography>
                        </TableCell>
                        <TableCell>
                          {action.workflow_run_id ? (
                            <Tooltip title="View workflow run">
                              <Link
                                component="button"
                                variant="body2"
                                sx={{
                                  fontFamily: 'monospace',
                                  fontSize: '0.7rem',
                                  cursor: 'pointer',
                                }}
                                onClick={() =>
                                  navigate(
                                    `/workflows?run_id=${action.workflow_run_id}`,
                                  )
                                }
                              >
                                {action.workflow_run_id}
                              </Link>
                            </Tooltip>
                          ) : (
                            <Typography
                              variant="caption"
                              sx={{ fontFamily: 'monospace' }}
                            >
                              {action.target}
                            </Typography>
                          )}
                        </TableCell>
                        <TableCell>
                          {action.workflow_phase_id ? (
                            <Chip
                              label={action.workflow_phase_id}
                              size="small"
                              variant="outlined"
                            />
                          ) : (
                            <Typography
                              variant="caption"
                              color="text.secondary"
                            >
                              —
                            </Typography>
                          )}
                        </TableCell>
                        <TableCell>
                          <Tooltip title={action.reason}>
                            <Typography
                              variant="body2"
                              noWrap
                              sx={{ maxWidth: 220 }}
                            >
                              {action.reason}
                            </Typography>
                          </Tooltip>
                        </TableCell>
                        <TableCell>
                          <Typography variant="caption" color="text.secondary">
                            {formatDateTime(action.created_at)}
                          </Typography>
                        </TableCell>
                        <TableCell align="right">
                          <Stack
                            direction="row"
                            spacing={1}
                            justifyContent="flex-end"
                          >
                            <Button
                              size="small"
                              variant="contained"
                              color="success"
                              startIcon={
                                <ThumbUpIcon sx={{ fontSize: 16 }} />
                              }
                              disabled={approvalActing === action.action_id}
                              onClick={() => handleApprove(action)}
                            >
                              Approve
                            </Button>
                            <Button
                              size="small"
                              variant="outlined"
                              color="error"
                              startIcon={
                                <ThumbDownIcon sx={{ fontSize: 16 }} />
                              }
                              disabled={approvalActing === action.action_id}
                              onClick={() => openRejectDialog(action)}
                            >
                              Reject
                            </Button>
                          </Stack>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableContainer>
            )}
          </TabPanel>

          <TabPanel value={tabValue} index={2}>
            <Grid container spacing={3}>
              <Grid item xs={12} md={6}>
                <Box sx={{ bgcolor: alpha(theme.palette.background.default, 0.5), borderRadius: 2, p: 2.5 }}>
                  <Typography variant="subtitle1" sx={{ fontWeight: 600, mb: 2 }}>Outcome Distribution</Typography>
                  {stats && Object.keys(stats.outcomes).length > 0 ? (
                    <Stack spacing={2}>
                      {Object.entries(stats.outcomes).map(([outcome, count]: [string, any]) => (
                        <Box key={outcome}>
                          <Box display="flex" justifyContent="space-between" mb={0.5}>
                            <Typography variant="body2">{outcome.replace('_', ' ').toUpperCase()}</Typography>
                            <Typography variant="body2" fontWeight="bold">{count}</Typography>
                          </Box>
                          <LinearProgress
                            variant="determinate"
                            value={(count / stats.total_with_feedback) * 100}
                            sx={{ height: 6, borderRadius: 3 }}
                          />
                        </Box>
                      ))}
                    </Stack>
                  ) : (
                    <Alert severity="info">No outcome data available yet</Alert>
                  )}
                </Box>
              </Grid>
              <Grid item xs={12} md={6}>
                <Box sx={{ bgcolor: alpha(theme.palette.background.default, 0.5), borderRadius: 2, p: 2.5 }}>
                  <Typography variant="subtitle1" sx={{ fontWeight: 600, mb: 2 }}>Performance Metrics</Typography>
                  <Stack spacing={2}>
                    <Box>
                      <Typography variant="caption" color="text.secondary">Decisions per Day</Typography>
                      <Typography variant="h4">{stats ? Math.round(stats.total_decisions / stats.period_days) : 0}</Typography>
                    </Box>
                    <Box>
                      <Typography variant="caption" color="text.secondary">Avg Time Saved per Decision</Typography>
                      <Typography variant="h4">
                        {stats && stats.total_decisions > 0 ? Math.round(stats.total_time_saved_minutes / stats.total_decisions) : 0}m
                      </Typography>
                    </Box>
                    <Box>
                      <Typography variant="caption" color="text.secondary">Decisions Needing Review</Typography>
                      <Typography variant="h4">{stats ? stats.total_decisions - stats.total_with_feedback : 0}</Typography>
                    </Box>
                  </Stack>
                </Box>
              </Grid>
            </Grid>
          </TabPanel>
        </Box>
      </Box>

      <AIDecisionFeedback
        open={feedbackDialogOpen}
        onClose={() => setFeedbackDialogOpen(false)}
        decision={selectedDecision}
        onFeedbackSubmitted={loadData}
      />

      <Dialog
        open={rejectDialog.open}
        onClose={closeRejectDialog}
        fullWidth
        maxWidth="sm"
      >
        <DialogTitle>Reject action</DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            {rejectDialog.action?.title}
          </Typography>
          <TextField
            autoFocus
            fullWidth
            multiline
            minRows={3}
            label="Rejection reason"
            value={rejectReason}
            onChange={(e) => setRejectReason(e.target.value)}
            helperText="Required. This is recorded on the workflow run's audit trail."
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={closeRejectDialog}>Cancel</Button>
          <Button
            variant="contained"
            color="error"
            disabled={!rejectReason.trim() || !!approvalActing}
            onClick={submitReject}
          >
            Reject
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  )
}
