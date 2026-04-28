import React, { useEffect, useState, useRef, useMemo } from 'react'
import { 
  Box, 
  CircularProgress, 
  Typography, 
  Paper, 
  Grid,
  Card,
  CardContent,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Chip,
  Slider,
  Button,
  Collapse,
  IconButton,
  Alert,
  Stack,
  ToggleButton,
  ToggleButtonGroup,
} from '@mui/material'
import { 
  BarChart, 
  Bar, 
  XAxis, 
  YAxis, 
  CartesianGrid, 
  Tooltip, 
  Legend, 
  ResponsiveContainer,
  PieChart,
  Pie,
  Cell,
} from 'recharts'
import { 
  ExpandMore as ExpandMoreIcon,
  ExpandLess as ExpandLessIcon,
  Refresh as RefreshIcon,
} from '@mui/icons-material'
import { attackApi } from '../../services/api'
import { severityColors as themeSeverityColors } from '../../theme'

interface TechniqueData {
  technique_id: string
  technique_name: string
  tactic: string
  count: number
  severities: {
    critical: number
    high: number
    medium: number
    low: number
  }
}

type TimeRange = '24h' | '7d' | '30d' | 'all'

interface TacticData {
  tactic: string
  count: number
}

export default function AttackChart() {
  const [tacticsData, setTacticsData] = useState<TacticData[]>([])
  const [techniquesData, setTechniquesData] = useState<TechniqueData[]>([])
  const [loading, setLoading] = useState(true)
  const [minConfidence, setMinConfidence] = useState(0.0)
  const [timeRange, setTimeRange] = useState<TimeRange>('all')
  const [expandedTechnique, setExpandedTechnique] = useState<string | null>(null)
  const [techniqueFindings, setTechniqueFindings] = useState<any[]>([])
  const [loadingFindings, setLoadingFindings] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    loadData()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [minConfidence, timeRange])

  const loadData = async () => {
    setLoading(true)
    setError(null)
    try {
      const [tacticsResponse, techniquesResponse] = await Promise.all([
        attackApi.getTacticsSummary(),
        attackApi.getTechniqueRollup(minConfidence, timeRange),
      ])
      
      setTacticsData(tacticsResponse.data.tactics || [])
      setTechniquesData(techniquesResponse.data.techniques || [])
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to load ATT&CK data'
      setError(message)
      console.error('Failed to load ATT&CK data:', err)
    } finally {
      setLoading(false)
    }
  }

  // Track active request for cancellation
  const abortControllerRef = useRef<AbortController | null>(null)

  const handleTechniqueClick = async (techniqueId: string) => {
    if (expandedTechnique === techniqueId) {
      setExpandedTechnique(null)
      setTechniqueFindings([])
      return
    }

    // Cancel any in-flight request
    if (abortControllerRef.current) {
      abortControllerRef.current.abort()
    }

    setExpandedTechnique(techniqueId)
    setTechniqueFindings([]) // Clear stale findings immediately
    setLoadingFindings(true)

    const controller = new AbortController()
    abortControllerRef.current = controller

    try {
      const response = await attackApi.getFindingsByTechnique(techniqueId)
      // Only update if this request wasn't cancelled
      if (!controller.signal.aborted) {
        setTechniqueFindings(response.data.findings || [])
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        console.error('Failed to load findings:', error)
      }
    } finally {
      if (!controller.signal.aborted) {
        setLoadingFindings(false)
      }
    }
  }

  const getSeverityColor = (severity: string) => {
    switch (severity?.toLowerCase()) {
      case 'critical': return 'error'
      case 'high': return 'warning'
      case 'medium': return 'info'
      case 'low': return 'success'
      default: return 'default'
    }
  }

  const { totalTechniques, totalDetections, totalCritical, totalHigh, totalMedium, totalLow } = useMemo(() => ({
    totalTechniques: techniquesData.length,
    totalDetections: techniquesData.reduce((sum, t) => sum + t.count, 0),
    totalCritical: techniquesData.reduce((sum, t) => sum + (t.severities?.critical || 0), 0),
    totalHigh: techniquesData.reduce((sum, t) => sum + (t.severities?.high || 0), 0),
    totalMedium: techniquesData.reduce((sum, t) => sum + (t.severities?.medium || 0), 0),
    totalLow: techniquesData.reduce((sum, t) => sum + (t.severities?.low || 0), 0),
  }), [techniquesData])

  const severityPieData = useMemo(() => [
    { name: 'Critical', value: totalCritical },
    { name: 'High', value: totalHigh },
    { name: 'Medium', value: totalMedium },
    { name: 'Low', value: totalLow },
  ].filter(d => d.value > 0), [totalCritical, totalHigh, totalMedium, totalLow])

  const severityColors = [
    themeSeverityColors.critical,
    themeSeverityColors.high,
    themeSeverityColors.medium,
    themeSeverityColors.low,
  ]

  if (loading) {
    return (
      <Box display="flex" justifyContent="center" p={3}>
        <CircularProgress />
      </Box>
    )
  }

  if (error) {
    return (
      <Alert 
        severity="error" 
        action={
          <Button color="inherit" size="small" onClick={loadData}>
            Retry
          </Button>
        }
      >
        {error}
      </Alert>
    )
  }

  if (tacticsData.length === 0 && techniquesData.length === 0) {
    return (
      <Paper sx={{ p: 3 }}>
        <Typography color="textSecondary" align="center">
          No ATT&CK data available. Import findings with MITRE ATT&CK technique mappings to see the attack overview.
        </Typography>
      </Paper>
    )
  }

  return (
    <Box>
      {/* Summary Statistics */}
      <Grid container spacing={2} sx={{ mb: 3 }}>
        <Grid item xs={12} sm={6} md={3}>
          <Card>
            <CardContent>
              <Typography color="textSecondary" gutterBottom variant="body2">
                Unique Techniques
              </Typography>
              <Typography variant="h4">{totalTechniques}</Typography>
            </CardContent>
          </Card>
        </Grid>
        <Grid item xs={12} sm={6} md={3}>
          <Card>
            <CardContent>
              <Typography color="textSecondary" gutterBottom variant="body2">
                Total Detections
              </Typography>
              <Typography variant="h4">{totalDetections}</Typography>
            </CardContent>
          </Card>
        </Grid>
        <Grid item xs={12} sm={6} md={3}>
          <Card sx={{ bgcolor: '#ffebee' }}>
            <CardContent>
              <Typography color="textSecondary" gutterBottom variant="body2">
                Critical Severity
              </Typography>
              <Typography variant="h4" color="error">{totalCritical}</Typography>
            </CardContent>
          </Card>
        </Grid>
        <Grid item xs={12} sm={6} md={3}>
          <Card sx={{ bgcolor: '#fff3e0' }}>
            <CardContent>
              <Typography color="textSecondary" gutterBottom variant="body2">
                High Severity
              </Typography>
              <Typography variant="h4" color="warning">{totalHigh}</Typography>
            </CardContent>
          </Card>
        </Grid>
      </Grid>

      {/* Filters */}
      <Paper sx={{ p: 2, mb: 3 }}>
        <Stack direction="column" spacing={2}>
          <Box display="flex" alignItems="center" gap={2} flexWrap="wrap">
            <Typography variant="body2" sx={{ minWidth: 100 }}>
              Time Range:
            </Typography>
            <ToggleButtonGroup
              value={timeRange}
              exclusive
              size="small"
              onChange={(_, value) => {
                if (value !== null) setTimeRange(value as TimeRange)
              }}
              aria-label="Time range"
            >
              <ToggleButton value="24h">24 Hours</ToggleButton>
              <ToggleButton value="7d">7 Days</ToggleButton>
              <ToggleButton value="30d">30 Days</ToggleButton>
              <ToggleButton value="all">All Time</ToggleButton>
            </ToggleButtonGroup>
          </Box>
          <Box display="flex" alignItems="center" gap={2}>
            <Typography variant="body2" sx={{ minWidth: 150 }}>
              Min Confidence: {minConfidence.toFixed(2)}
            </Typography>
            <Slider
              value={minConfidence}
              onChange={(_, value) => setMinConfidence(value as number)}
              min={0}
              max={1}
              step={0.1}
              marks
              aria-label="Minimum confidence threshold"
              sx={{ flex: 1 }}
            />
            <Button
              startIcon={loading ? <CircularProgress size={16} /> : <RefreshIcon />}
              onClick={loadData}
              size="small"
              variant="outlined"
              disabled={loading}
            >
              Refresh
            </Button>
          </Box>
        </Stack>
      </Paper>

      {/* Charts Row */}
      <Grid container spacing={3} sx={{ mb: 3 }}>
        {/* Tactics Distribution */}
        {tacticsData.length > 0 && (
          <Grid item xs={12} md={6}>
            <Paper sx={{ p: 2 }}>
              <Typography variant="h6" gutterBottom>
                MITRE ATT&CK Tactics Distribution
              </Typography>
              <ResponsiveContainer width="100%" height={300}>
                <BarChart data={tacticsData}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="tactic" angle={-45} textAnchor="end" height={100} />
                  <YAxis />
                  <Tooltip />
                  <Legend />
                  <Bar dataKey="count" fill="#1976d2" />
                </BarChart>
              </ResponsiveContainer>
            </Paper>
          </Grid>
        )}

        {/* Severity Distribution */}
        {totalDetections > 0 && (
          <Grid item xs={12} md={6}>
            <Paper sx={{ p: 2 }}>
              <Typography variant="h6" gutterBottom>
                Severity Distribution
              </Typography>
              <ResponsiveContainer width="100%" height={300}>
                <PieChart>
                  <Pie
                    data={severityPieData}
                    cx="50%"
                    cy="50%"
                    labelLine={false}
                    label={({ name, percent }) => `${name}: ${(percent * 100).toFixed(0)}%`}
                    outerRadius={80}
                    fill="#8884d8"
                    dataKey="value"
                  >
                    {severityColors.map((color, index) => (
                      <Cell key={`cell-${index}`} fill={color} />
                    ))}
                  </Pie>
                  <Tooltip />
                </PieChart>
              </ResponsiveContainer>
            </Paper>
          </Grid>
        )}
      </Grid>

      {/* MITRE Techniques Table */}
      {techniquesData.length > 0 && (
        <Paper sx={{ width: '100%' }}>
          <Box sx={{ p: 2 }}>
            <Typography variant="h6" gutterBottom>
              MITRE ATT&CK Techniques by Occurrence
            </Typography>
            <Typography variant="body2" color="textSecondary" gutterBottom>
              {techniquesData.length} distinct technique{techniquesData.length === 1 ? '' : 's'} observed. Click a row to see associated findings.
            </Typography>
          </Box>
          <TableContainer sx={{ maxHeight: 600 }}>
            <Table stickyHeader>
              <TableHead>
                <TableRow>
                  <TableCell>Technique ID</TableCell>
                  <TableCell>Technique Name</TableCell>
                  <TableCell>Tactic</TableCell>
                  <TableCell align="right">Total Detections</TableCell>
                  <TableCell align="center">Critical</TableCell>
                  <TableCell align="center">High</TableCell>
                  <TableCell align="center">Medium</TableCell>
                  <TableCell align="center">Low</TableCell>
                  <TableCell align="center">Actions</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {techniquesData.map((technique) => (
                  <React.Fragment key={technique.technique_id}>
                    <TableRow
                      hover
                      sx={{ cursor: 'pointer' }}
                      onClick={() => handleTechniqueClick(technique.technique_id)}
                    >
                      <TableCell>
                        <Typography variant="body2" fontWeight="medium">
                          {technique.technique_id}
                        </Typography>
                      </TableCell>
                      <TableCell>
                        <Typography variant="body2" noWrap sx={{ maxWidth: 240 }}>
                          {technique.technique_name}
                        </Typography>
                      </TableCell>
                      <TableCell>
                        <Chip
                          label={technique.tactic}
                          size="small"
                          variant="outlined"
                        />
                      </TableCell>
                      <TableCell align="right">
                        <Chip label={technique.count} size="small" />
                      </TableCell>
                      <TableCell align="center">
                        {technique.severities?.critical > 0 && (
                          <Chip 
                            label={technique.severities.critical} 
                            size="small" 
                            color="error"
                          />
                        )}
                      </TableCell>
                      <TableCell align="center">
                        {technique.severities?.high > 0 && (
                          <Chip 
                            label={technique.severities.high} 
                            size="small" 
                            color="warning"
                          />
                        )}
                      </TableCell>
                      <TableCell align="center">
                        {technique.severities?.medium > 0 && (
                          <Chip 
                            label={technique.severities.medium} 
                            size="small" 
                            color="info"
                          />
                        )}
                      </TableCell>
                      <TableCell align="center">
                        {technique.severities?.low > 0 && (
                          <Chip 
                            label={technique.severities.low} 
                            size="small" 
                            color="success"
                          />
                        )}
                      </TableCell>
                      <TableCell align="center">
                        <IconButton 
                          size="small"
                          aria-label={expandedTechnique === technique.technique_id ? `Collapse ${technique.technique_id}` : `Expand ${technique.technique_id}`}
                        >
                          {expandedTechnique === technique.technique_id ? (
                            <ExpandLessIcon />
                          ) : (
                            <ExpandMoreIcon />
                          )}
                        </IconButton>
                      </TableCell>
                    </TableRow>
                    {/* Expanded Findings Row */}
                    <TableRow>
                      <TableCell colSpan={9} sx={{ p: 0 }}>
                        <Collapse 
                          in={expandedTechnique === technique.technique_id} 
                          timeout="auto" 
                          unmountOnExit
                        >
                          <Box sx={{ p: 2, bgcolor: 'grey.50' }}>
                            {loadingFindings ? (
                              <Box display="flex" justifyContent="center" p={2}>
                                <CircularProgress size={24} />
                              </Box>
                            ) : (
                              <>
                                <Typography variant="subtitle2" gutterBottom>
                                  Associated Findings ({techniqueFindings.length})
                                </Typography>
                                {techniqueFindings.length > 0 ? (
                                  <Table size="small">
                                    <TableHead>
                                      <TableRow>
                                        <TableCell>Finding ID</TableCell>
                                        <TableCell>Severity</TableCell>
                                        <TableCell>Data Source</TableCell>
                                        <TableCell>Anomaly Score</TableCell>
                                      </TableRow>
                                    </TableHead>
                                    <TableBody>
                                      {techniqueFindings.slice(0, 10).map((finding) => (
                                        <TableRow key={finding.finding_id}>
                                          <TableCell>
                                            <Typography variant="body2" noWrap sx={{ maxWidth: 200 }}>
                                              {finding.finding_id}
                                            </Typography>
                                          </TableCell>
                                          <TableCell>
                                            <Chip 
                                              label={finding.severity} 
                                              size="small"
                                              color={getSeverityColor(finding.severity)}
                                            />
                                          </TableCell>
                                          <TableCell>{finding.data_source}</TableCell>
                                          <TableCell>
                                            {finding.anomaly_score?.toFixed(2) || 'N/A'}
                                          </TableCell>
                                        </TableRow>
                                      ))}
                                    </TableBody>
                                  </Table>
                                ) : (
                                  <Typography variant="body2" color="textSecondary">
                                    No findings available
                                  </Typography>
                                )}
                                {techniqueFindings.length > 10 && (
                                  <Typography variant="caption" color="textSecondary" sx={{ mt: 1, display: 'block' }}>
                                    Showing 10 of {techniqueFindings.length} findings
                                  </Typography>
                                )}
                              </>
                            )}
                          </Box>
                        </Collapse>
                      </TableCell>
                    </TableRow>
                  </React.Fragment>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
        </Paper>
      )}
    </Box>
  )
}

