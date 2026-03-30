/**
 * Analytics Dashboard - AI-driven insights and SOC metrics
 * 
 * Features:
 * - Key SOC metrics and KPIs
 * - Trend analysis and forecasting
 * - AI-powered insights and recommendations
 * - Performance monitoring
 * - Anomaly detection
 */

import { useState, useEffect, useMemo } from 'react'
import {
  Box,
  Grid,
  Paper,
  Typography,
  Card,
  CardContent,
  CardHeader,
  IconButton,
  Chip,
  Alert,
  ToggleButtonGroup,
  ToggleButton,
  Skeleton,
} from '@mui/material'
import {
  TrendingUp as TrendingUpIcon,
  TrendingDown as TrendingDownIcon,
  Psychology as AIIcon,
  Refresh as RefreshIcon,
  Warning as WarningIcon,
  CheckCircle as SuccessIcon,
  Info as InfoIcon,
  Speed as SpeedIcon,
} from '@mui/icons-material'
import {
  LineChart,
  Line,
  BarChart,
  Bar,
  PieChart,
  Pie,
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
  Cell,
} from 'recharts'
import api from '../services/api'
import { format } from 'date-fns'

interface MetricCard {
  title: string
  value: number | string
  change: number // percentage
  trend: 'up' | 'down' | 'stable'
  icon: React.ReactNode
  color: string
  lowerIsBetter?: boolean // For metrics where a decrease is positive
}

interface AIInsight {
  id: string
  type: 'recommendation' | 'warning' | 'info' | 'anomaly'
  title: string
  description: string
  confidence: number
  timestamp: string
  actionable: boolean
}

interface AnalyticsData {
  metrics: {
    totalFindings: number
    totalCases: number
    avgResponseTime: number
    falsePositiveRate: number
    findingsChange: number
    casesChange: number
    responseTimeChange: number
    falsePositiveChange: number
  }
  timeSeriesData: Array<{
    timestamp: string
    findings: number
    cases: number
    alerts: number
  }>
  severityDistribution: Array<{
    name: string
    value: number
    color: string
  }>
  topSources: Array<{
    name: string
    count: number
  }>
  responseTimeData: Array<{
    period: string
    avgTime: number
    target: number
  }>
  affectedEntities: Array<{
    entity: string
    count: number
    critical: number
    high: number
    medium: number
    low: number
    riskScore: number
  }>
  attackHeatmap: Array<{
    day: string
    dayNum: number
    hour: number
    count: number
    critical: number
    high: number
    intensity: number
  }>
  mitreTechniques: Array<{
    techniqueId: string
    techniqueName: string
    tactic: string
    count: number
  }>
  insights: AIInsight[]
}

const COLORS = {
  critical: '#d32f2f',
  high: '#f57c00',
  medium: '#fbc02d',
  low: '#388e3c',
  primary: '#1976d2',
  success: '#4caf50',
  warning: '#ff9800',
  info: '#2196f3',
}

export default function Analytics() {
  const [timeRange, setTimeRange] = useState<'24h' | '7d' | '30d' | 'all'>('7d')
  const [loading, setLoading] = useState(true)
  const [analyticsData, setAnalyticsData] = useState<AnalyticsData | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetchAnalytics()
  }, [timeRange])

  const fetchAnalytics = async () => {
    setLoading(true)
    setError(null)
    try {
      const response = await api.get(`/analytics?timeRange=${timeRange}`)
      setAnalyticsData(response.data)
    } catch (error) {
      console.error('Error fetching analytics:', error)
      const errorMessage = error instanceof Error ? error.message : 'Failed to load analytics data'
      setError(errorMessage)
      // Set to null on error to ensure proper fallback
      setAnalyticsData(null)
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }

  const handleRefresh = () => {
    setRefreshing(true)
    fetchAnalytics()
  }

  const handleTimeRangeChange = (_: React.MouseEvent<HTMLElement>, newValue: string | null) => {
    if (newValue) {
      setTimeRange(newValue as '24h' | '7d' | '30d' | 'all')
    }
  }

  const metricCards = useMemo<MetricCard[]>(() => {
    if (!analyticsData || !analyticsData.metrics) return []

    return [
      {
        title: 'Total Findings',
        value: analyticsData.metrics.totalFindings,
        change: analyticsData.metrics.findingsChange,
        trend: analyticsData.metrics.findingsChange > 0 ? 'up' : 'down',
        icon: <WarningIcon />,
        color: COLORS.warning,
      },
      {
        title: 'Active Cases',
        value: analyticsData.metrics.totalCases,
        change: analyticsData.metrics.casesChange,
        trend: analyticsData.metrics.casesChange > 0 ? 'up' : 'down',
        icon: <InfoIcon />,
        color: COLORS.info,
      },
      {
        title: 'Avg Response Time',
        value: `${analyticsData.metrics.avgResponseTime}m`,
        change: analyticsData.metrics.responseTimeChange,
        trend: analyticsData.metrics.responseTimeChange > 0 ? 'up' : analyticsData.metrics.responseTimeChange < 0 ? 'down' : 'stable',
        icon: <SpeedIcon />,
        color: COLORS.success,
        lowerIsBetter: true,
      },
      {
        title: 'False Positive Rate',
        value: `${analyticsData.metrics.falsePositiveRate}%`,
        change: analyticsData.metrics.falsePositiveChange,
        trend: analyticsData.metrics.falsePositiveChange > 0 ? 'up' : analyticsData.metrics.falsePositiveChange < 0 ? 'down' : 'stable',
        icon: <SuccessIcon />,
        color: COLORS.primary,
        lowerIsBetter: true,
      },
    ]
  }, [analyticsData])

  if (loading && !analyticsData) {
    return (
      <Box p={3}>
        <Typography variant="h4" gutterBottom>
          Analytics Dashboard
        </Typography>
        <Grid container spacing={3} mt={2}>
          {[1, 2, 3, 4].map((i) => (
            <Grid item xs={12} sm={6} md={3} key={i}>
              <Skeleton variant="rectangular" height={150} />
            </Grid>
          ))}
          {[1, 2, 3].map((i) => (
            <Grid item xs={12} md={4} key={i}>
              <Skeleton variant="rectangular" height={300} />
            </Grid>
          ))}
        </Grid>
      </Box>
    )
  }

  // Show empty state if no data and not loading
  if (!loading && !analyticsData) {
    return (
      <Box p={3}>
        <Box display="flex" justifyContent="space-between" alignItems="center" mb={3}>
          <Typography variant="h4">Analytics Dashboard</Typography>
          <IconButton onClick={handleRefresh}>
            <RefreshIcon />
          </IconButton>
        </Box>
        {error && (
          <Alert severity="error" sx={{ mb: 3 }} onClose={() => setError(null)}>
            <Typography variant="body1" fontWeight="bold">
              Error Loading Analytics
            </Typography>
            <Typography variant="body2">{error}</Typography>
            <Typography variant="body2" mt={1}>
              Please check that the backend server is running and try refreshing.
            </Typography>
          </Alert>
        )}
        <Box display="flex" justifyContent="center" alignItems="center" minHeight="400px">
          <Box textAlign="center">
            <InfoIcon sx={{ fontSize: 64, color: 'text.secondary', mb: 2 }} />
            <Typography variant="h6" color="text.secondary" gutterBottom>
              No Analytics Data Available
            </Typography>
            <Typography variant="body2" color="text.secondary">
              Click the refresh button to load analytics data
            </Typography>
          </Box>
        </Box>
      </Box>
    )
  }

  return (
    <Box p={3}>
      {/* Header */}
      <Box display="flex" justifyContent="space-between" alignItems="center" mb={3}>
        <Typography variant="h4">Analytics Dashboard</Typography>
        <Box display="flex" gap={2} alignItems="center">
          <ToggleButtonGroup
            value={timeRange}
            exclusive
            onChange={handleTimeRangeChange}
            size="small"
          >
            <ToggleButton value="24h">24 Hours</ToggleButton>
            <ToggleButton value="7d">7 Days</ToggleButton>
            <ToggleButton value="30d">30 Days</ToggleButton>
            <ToggleButton value="all">All Time</ToggleButton>
          </ToggleButtonGroup>
          <IconButton onClick={handleRefresh} disabled={refreshing}>
            <RefreshIcon className={refreshing ? 'spin' : ''} />
          </IconButton>
        </Box>
      </Box>

      {/* Error Alert */}
      {error && (
        <Alert severity="error" sx={{ mb: 3 }} onClose={() => setError(null)}>
          <Typography variant="body1" fontWeight="bold">
            Error Loading Analytics
          </Typography>
          <Typography variant="body2">{error}</Typography>
          <Typography variant="body2" mt={1}>
            Please check that the backend server is running and try refreshing.
          </Typography>
        </Alert>
      )}

      {/* Key Metrics */}
      <Grid container spacing={3} mb={3}>
        {metricCards.map((card, index) => (
          <Grid item xs={12} sm={6} md={3} key={index}>
            <Card>
              <CardContent>
                <Box display="flex" justifyContent="space-between" alignItems="flex-start">
                  <Box>
                    <Typography color="text.secondary" gutterBottom variant="body2">
                      {card.title}
                    </Typography>
                    <Typography variant="h4" component="div">
                      {card.value}
                    </Typography>
                    <Box display="flex" alignItems="center" mt={1}>
                      {card.trend === 'up' ? (
                        <TrendingUpIcon
                          fontSize="small"
                          sx={{
                            color: card.lowerIsBetter
                              ? 'error.main'   // Up is bad for lowerIsBetter
                              : 'success.main', // Up is good normally
                          }}
                        />
                      ) : (
                        <TrendingDownIcon
                          fontSize="small"
                          sx={{
                            color: card.lowerIsBetter
                              ? 'success.main'  // Down is good for lowerIsBetter
                              : 'error.main',   // Down is bad normally
                          }}
                        />
                      )}
                      <Typography
                        variant="body2"
                        sx={{
                          ml: 0.5,
                          color: card.lowerIsBetter
                            ? (card.change < 0 ? 'success.main' : 'error.main')
                            : (card.change > 0 ? 'success.main' : 'error.main'),
                        }}
                      >
                        {Math.abs(card.change)}%
                      </Typography>
                      <Typography variant="body2" color="text.secondary" sx={{ ml: 1 }}>
                        vs last period
                      </Typography>
                    </Box>
                  </Box>
                  <Box
                    sx={{
                      bgcolor: card.color + '20',
                      color: card.color,
                      p: 1,
                      borderRadius: 1,
                    }}
                  >
                    {card.icon}
                  </Box>
                </Box>
              </CardContent>
            </Card>
          </Grid>
        ))}
      </Grid>

      {/* AI Insights */}
      {analyticsData?.insights && analyticsData.insights.length > 0 && (
        <Box mb={3}>
          <Card>
            <CardHeader
              title={
                <Box display="flex" alignItems="center" gap={1}>
                  <AIIcon color="primary" />
                  <Typography variant="h6">AI-Powered Insights</Typography>
                </Box>
              }
            />
            <CardContent>
              <Grid container spacing={2}>
                {analyticsData.insights.map((insight) => (
                  <Grid item xs={12} md={6} key={insight.id}>
                    <Alert
                      severity={
                        insight.type === 'warning'
                          ? 'warning'
                          : insight.type === 'anomaly'
                          ? 'error'
                          : insight.type === 'recommendation'
                          ? 'success'
                          : 'info'
                      }
                      sx={{ height: '100%' }}
                    >
                      <Box>
                        <Box display="flex" justifyContent="space-between" alignItems="start">
                          <Typography variant="subtitle2" fontWeight="bold">
                            {insight.title}
                          </Typography>
                          <Chip
                            label={`${Math.round(insight.confidence * 100)}% confident`}
                            size="small"
                            variant="outlined"
                          />
                        </Box>
                        <Typography variant="body2" mt={1}>
                          {insight.description}
                        </Typography>
                        <Typography variant="caption" color="text.secondary" mt={1} display="block">
                          {format(new Date(insight.timestamp), 'PPpp')}
                        </Typography>
                      </Box>
                    </Alert>
                  </Grid>
                ))}
              </Grid>
            </CardContent>
          </Card>
        </Box>
      )}

      {/* Charts */}
      <Grid container spacing={3}>
        {/* Findings Over Time */}
        <Grid item xs={12} md={8}>
          <Paper sx={{ p: 2, height: '400px' }}>
            <Typography variant="h6" gutterBottom>
              Findings & Cases Over Time
            </Typography>
            {analyticsData?.timeSeriesData && (
              <ResponsiveContainer width="100%" height="90%">
                <AreaChart data={analyticsData.timeSeriesData}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis
                    dataKey="timestamp"
                    tickFormatter={(value) => format(new Date(value), 'MMM d')}
                  />
                  <YAxis />
                  <Tooltip labelFormatter={(value) => format(new Date(value), 'PPp')} />
                  <Legend />
                  <Area
                    type="monotone"
                    dataKey="findings"
                    stackId="1"
                    stroke={COLORS.warning}
                    fill={COLORS.warning}
                    name="Findings"
                  />
                  <Area
                    type="monotone"
                    dataKey="cases"
                    stackId="1"
                    stroke={COLORS.info}
                    fill={COLORS.info}
                    name="Cases"
                  />
                  <Area
                    type="monotone"
                    dataKey="alerts"
                    stackId="1"
                    stroke={COLORS.critical}
                    fill={COLORS.critical}
                    name="Alerts"
                  />
                </AreaChart>
              </ResponsiveContainer>
            )}
          </Paper>
        </Grid>

        {/* Severity Distribution */}
        <Grid item xs={12} md={4}>
          <Paper sx={{ p: 2, height: '400px' }}>
            <Typography variant="h6" gutterBottom>
              Severity Distribution
            </Typography>
            {analyticsData?.severityDistribution && (
              <ResponsiveContainer width="100%" height="90%">
                <PieChart>
                  <Pie
                    data={analyticsData.severityDistribution}
                    cx="50%"
                    cy="50%"
                    labelLine={false}
                    label={({ name, percent }) =>
                      `${name}: ${(percent * 100).toFixed(0)}%`
                    }
                    outerRadius={80}
                    fill="#8884d8"
                    dataKey="value"
                  >
                    {analyticsData.severityDistribution.map((entry, index) => (
                      <Cell key={`cell-${index}`} fill={entry.color} />
                    ))}
                  </Pie>
                  <Tooltip />
                </PieChart>
              </ResponsiveContainer>
            )}
          </Paper>
        </Grid>

        {/* Top Alert Sources */}
        <Grid item xs={12} md={6}>
          <Paper sx={{ p: 2, height: '400px' }}>
            <Typography variant="h6" gutterBottom>
              Top Alert Sources
            </Typography>
            {analyticsData?.topSources && (
              <ResponsiveContainer width="100%" height="90%">
                <BarChart data={analyticsData.topSources} layout="vertical">
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis type="number" />
                  <YAxis dataKey="name" type="category" width={150} />
                  <Tooltip />
                  <Bar dataKey="count" fill={COLORS.primary} />
                </BarChart>
              </ResponsiveContainer>
            )}
          </Paper>
        </Grid>

        {/* Response Time Trend */}
        <Grid item xs={12} md={6}>
          <Paper sx={{ p: 2, height: '400px' }}>
            <Typography variant="h6" gutterBottom>
              Response Time Trend
            </Typography>
            {analyticsData?.responseTimeData && (
              <ResponsiveContainer width="100%" height="90%">
                <LineChart data={analyticsData.responseTimeData}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="period" />
                  <YAxis label={{ value: 'Minutes', angle: -90, position: 'insideLeft' }} />
                  <Tooltip />
                  <Legend />
                  <Line
                    type="monotone"
                    dataKey="avgTime"
                    stroke={COLORS.primary}
                    strokeWidth={2}
                    name="Avg Response Time"
                  />
                  <Line
                    type="monotone"
                    dataKey="target"
                    stroke={COLORS.success}
                    strokeDasharray="5 5"
                    name="Target"
                  />
                </LineChart>
              </ResponsiveContainer>
            )}
          </Paper>
        </Grid>

        {/* MITRE ATT&CK Techniques */}
        <Grid item xs={12} md={6}>
          <Paper sx={{ p: 2, height: '400px' }}>
            <Typography variant="h6" gutterBottom>
              Top MITRE ATT&CK Techniques
            </Typography>
            {analyticsData?.mitreTechniques && analyticsData.mitreTechniques.length > 0 ? (
              <ResponsiveContainer width="100%" height="90%">
                <BarChart data={analyticsData.mitreTechniques} layout="vertical">
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis type="number" />
                  <YAxis 
                    dataKey="techniqueId" 
                    type="category" 
                    width={100}
                    tick={{ fontSize: 11 }}
                  />
                  <Tooltip 
                    content={({ payload }) => {
                      if (payload && payload[0]) {
                        const data = payload[0].payload
                        return (
                          <Box sx={{ bgcolor: 'background.paper', p: 1, border: 1, borderColor: 'divider' }}>
                            <Typography variant="caption" fontWeight="bold">{data.techniqueId}</Typography>
                            <Typography variant="caption" display="block">{data.techniqueName}</Typography>
                            <Typography variant="caption" display="block">Tactic: {data.tactic}</Typography>
                            <Typography variant="caption" display="block">Count: {data.count}</Typography>
                          </Box>
                        )
                      }
                      return null
                    }}
                  />
                  <Bar dataKey="count" fill={COLORS.critical} />
                </BarChart>
              </ResponsiveContainer>
            ) : (
              <Box display="flex" justifyContent="center" alignItems="center" height="90%">
                <Typography variant="body2" color="text.secondary">
                  No MITRE technique data available
                </Typography>
              </Box>
            )}
          </Paper>
        </Grid>

        {/* Affected Entities/Devices */}
        <Grid item xs={12} md={6}>
          <Paper sx={{ p: 2, height: '400px', overflow: 'auto' }}>
            <Typography variant="h6" gutterBottom>
              Most Affected Devices/Entities
            </Typography>
            {analyticsData?.affectedEntities && analyticsData.affectedEntities.length > 0 ? (
              <Box>
                {analyticsData.affectedEntities.map((entity, index) => (
                  <Box
                    key={index}
                    sx={{
                      p: 1.5,
                      mb: 1,
                      border: 1,
                      borderColor: 'divider',
                      borderRadius: 1,
                      bgcolor: 'background.default',
                    }}
                  >
                    <Box display="flex" justifyContent="space-between" alignItems="center" mb={0.5}>
                      <Typography variant="body2" fontWeight="bold" sx={{ 
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                        maxWidth: '60%'
                      }}>
                        {entity.entity}
                      </Typography>
                      <Chip 
                        label={`Risk: ${entity.riskScore}`} 
                        size="small" 
                        color={entity.riskScore > 50 ? 'error' : entity.riskScore > 20 ? 'warning' : 'default'}
                      />
                    </Box>
                    <Box display="flex" gap={1} flexWrap="wrap">
                      {entity.critical > 0 && (
                        <Chip
                          label={`Critical: ${entity.critical}`}
                          size="small"
                          sx={{ bgcolor: COLORS.critical + '20', color: COLORS.critical }}
                        />
                      )}
                      {entity.high > 0 && (
                        <Chip
                          label={`High: ${entity.high}`}
                          size="small"
                          sx={{ bgcolor: COLORS.high + '20', color: COLORS.high }}
                        />
                      )}
                      {entity.medium > 0 && (
                        <Chip
                          label={`Medium: ${entity.medium}`}
                          size="small"
                          sx={{ bgcolor: COLORS.medium + '20', color: COLORS.medium }}
                        />
                      )}
                      {entity.low > 0 && (
                        <Chip
                          label={`Low: ${entity.low}`}
                          size="small"
                          sx={{ bgcolor: COLORS.low + '20', color: COLORS.low }}
                        />
                      )}
                    </Box>
                    <Typography variant="caption" color="text.secondary" display="block" mt={0.5}>
                      Total Findings: {entity.count}
                    </Typography>
                  </Box>
                ))}
              </Box>
            ) : (
              <Box display="flex" justifyContent="center" alignItems="center" height="90%">
                <Typography variant="body2" color="text.secondary">
                  No entity data available
                </Typography>
              </Box>
            )}
          </Paper>
        </Grid>

        {/* Attack Time Heatmap */}
        <Grid item xs={12}>
          <Paper sx={{ p: 2 }}>
            <Typography variant="h6" gutterBottom>
              Attack Time Heatmap (Hour of Day vs Day of Week)
            </Typography>
            {analyticsData?.attackHeatmap && analyticsData.attackHeatmap.length > 0 ? (
              <Box sx={{ overflowX: 'auto' }}>
                <Box sx={{ minWidth: 800, height: 300 }}>
                  {/* Render heatmap using a simple grid */}
                  <Box display="flex" flexDirection="column" height="100%">
                    <Box display="flex" mb={1}>
                      <Box width="80px" />
                      {Array.from({ length: 24 }, (_, i) => (
                        <Box
                          key={i}
                          flex={1}
                          display="flex"
                          justifyContent="center"
                          alignItems="center"
                        >
                          <Typography variant="caption" fontSize="10px">
                            {i}
                          </Typography>
                        </Box>
                      ))}
                    </Box>
                    {['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'].map((day, dayIdx) => (
                      <Box key={day} display="flex" flex={1} mb={0.5}>
                        <Box
                          width="80px"
                          display="flex"
                          alignItems="center"
                          pr={1}
                        >
                          <Typography variant="caption" fontSize="11px">
                            {day}
                          </Typography>
                        </Box>
                        {Array.from({ length: 24 }, (_, hourIdx) => {
                          const dataPoint = analyticsData.attackHeatmap.find(
                            (d) => d.dayNum === dayIdx && d.hour === hourIdx
                          )
                          const intensity = dataPoint?.intensity || 0
                          const maxIntensity = Math.max(...analyticsData.attackHeatmap.map(d => d.intensity), 1)
                          const opacity = intensity / maxIntensity
                          
                          return (
                            <Box
                              key={hourIdx}
                              flex={1}
                              sx={{
                                bgcolor: intensity > 0 ? COLORS.critical : COLORS.low,
                                opacity: intensity > 0 ? 0.3 + (opacity * 0.7) : 0.1,
                                border: '1px solid',
                                borderColor: 'divider',
                                cursor: 'pointer',
                                '&:hover': {
                                  borderColor: 'primary.main',
                                  borderWidth: 2,
                                },
                              }}
                              title={`${day} ${hourIdx}:00 - ${intensity} findings (${dataPoint?.critical || 0} critical, ${dataPoint?.high || 0} high)`}
                            />
                          )
                        })}
                      </Box>
                    ))}
                  </Box>
                  <Box display="flex" justifyContent="center" mt={2} gap={2}>
                    <Box display="flex" alignItems="center" gap={0.5}>
                      <Box width={20} height={20} sx={{ bgcolor: COLORS.low, opacity: 0.1 }} />
                      <Typography variant="caption">Low</Typography>
                    </Box>
                    <Box display="flex" alignItems="center" gap={0.5}>
                      <Box width={20} height={20} sx={{ bgcolor: COLORS.critical, opacity: 0.5 }} />
                      <Typography variant="caption">Medium</Typography>
                    </Box>
                    <Box display="flex" alignItems="center" gap={0.5}>
                      <Box width={20} height={20} sx={{ bgcolor: COLORS.critical, opacity: 1 }} />
                      <Typography variant="caption">High</Typography>
                    </Box>
                  </Box>
                </Box>
              </Box>
            ) : (
              <Box display="flex" justifyContent="center" alignItems="center" height="300px">
                <Typography variant="body2" color="text.secondary">
                  No attack time data available
                </Typography>
              </Box>
            )}
          </Paper>
        </Grid>
      </Grid>

      <style>{`
        @keyframes spin {
          from { transform: rotate(0deg); }
          to { transform: rotate(360deg); }
        }
        .spin {
          animation: spin 1s linear infinite;
        }
      `}</style>
    </Box>
  )
}

