import { useState, useEffect } from 'react'
import {
  Box,
  Container,
  Typography,
  Paper,
  Grid,
  Button,
  Select,
  MenuItem,
  FormControl,
  InputLabel,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Chip,
  LinearProgress,
  Alert,
} from '@mui/material'
import {
  TrendingUp as TrendingIcon,
  AccessTime as TimeIcon,
  Assessment as MetricsIcon,
  Person as PersonIcon,
  Refresh as RefreshIcon,
} from '@mui/icons-material'
import {
  BarChart,
  Bar,
  LineChart,
  Line,
  PieChart,
  Pie,
  Cell,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from 'recharts'
import { caseMetricsApi } from '../services/api'
import StatCard from '../components/ui/StatCard'

const COLORS = ['#f44336', '#ff9800', '#4caf50', '#2196f3', '#9c27b0']

export default function CaseMetricsDashboard() {
  const [loading, setLoading] = useState(false)
  const [summary, setSummary] = useState<any>(null)
  const [mttd, setMTTD] = useState<any>(null)
  const [mttr, setMTTR] = useState<any>(null)
  const [byPriority, setByPriority] = useState<any[]>([])
  const [byStatus, setByStatus] = useState<any[]>([])
  const [analystPerformance, setAnalystPerformance] = useState<any[]>([])
  const [timeRange, setTimeRange] = useState('30')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    loadAllMetrics()
  }, [timeRange])

  const loadAllMetrics = async () => {
    setLoading(true)
    try {
      // Compute date range from timeRange selection
      const endDate = new Date().toISOString()
      const startDate = new Date(Date.now() - parseInt(timeRange) * 24 * 60 * 60 * 1000).toISOString()
      const dateParams = { start_date: startDate, end_date: endDate }

      const [summaryRes, mttdRes, mttrRes, priorityRes, statusRes, analystRes] = await Promise.all([
        caseMetricsApi.getSummary(),
        caseMetricsApi.getMTTD(dateParams),
        caseMetricsApi.getMTTR(dateParams),
        caseMetricsApi.getByPriority(),
        caseMetricsApi.getByStatus(),
        caseMetricsApi.getAnalystPerformance(),
      ])

      setSummary(summaryRes.data)
      setMTTD(mttdRes.data)
      setMTTR(mttrRes.data)
      setByPriority(priorityRes.data.priority_breakdown || [])
      setByStatus(statusRes.data.status_breakdown || [])
      setAnalystPerformance(analystRes.data.analyst_performance || [])
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to load metrics'
      setError(message)
      console.error('Failed to load metrics:', err)
    } finally {
      setLoading(false)
    }
  }

  const formatDuration = (hours: number) => {
    if (hours < 1) return `${Math.round(hours * 60)}m`
    if (hours < 24) return `${hours.toFixed(1)}h`
    return `${(hours / 24).toFixed(1)}d`
  }

  const getSuccessRate = (closed: number, total: number): number => {
    if (!total || total === 0) return 0
    return Math.round((closed / total) * 100)
  }

  return (
    <Container maxWidth="xl" sx={{ py: 4 }}>
      <Box display="flex" justifyContent="space-between" alignItems="center" mb={4}>
        <Box>
          <Typography variant="h4" gutterBottom>
            Case Metrics Dashboard
          </Typography>
          <Typography variant="body2" color="text.secondary">
            Real-time SOC performance analytics and key metrics
          </Typography>
        </Box>
        <Box display="flex" gap={2}>
          <FormControl sx={{ minWidth: 150 }}>
            <InputLabel>Time Range</InputLabel>
            <Select
              value={timeRange}
              label="Time Range"
              onChange={(e) => setTimeRange(e.target.value)}
            >
              <MenuItem value="7">Last 7 days</MenuItem>
              <MenuItem value="30">Last 30 days</MenuItem>
              <MenuItem value="90">Last 90 days</MenuItem>
            </Select>
          </FormControl>
          <Button
            variant="outlined"
            startIcon={<RefreshIcon />}
            onClick={loadAllMetrics}
            disabled={loading}
          >
            Refresh
          </Button>
        </Box>
      </Box>

      {loading && <LinearProgress sx={{ mb: 2 }} />}

      {error && (
        <Alert 
          severity="error" 
          sx={{ mb: 2 }}
          action={
            <Button color="inherit" size="small" onClick={loadAllMetrics}>
              Retry
            </Button>
          }
          onClose={() => setError(null)}
        >
          {error}
        </Alert>
      )}

      {/* Key Metrics Cards */}
      <Grid container spacing={3} mb={4}>
        <Grid item xs={12} sm={6} md={3}>
          <StatCard
            title="Total Cases"
            value={summary?.total_cases || 0}
            icon={<MetricsIcon />}
            color="#2196f3"
          />
        </Grid>
        <Grid item xs={12} sm={6} md={3}>
          <StatCard
            title="Open Cases"
            value={summary?.open_cases || 0}
            icon={<TrendingIcon />}
            color="#ff9800"
            subtitle={`${summary?.critical_cases || 0} Critical`}
          />
        </Grid>
        <Grid item xs={12} sm={6} md={3}>
          <StatCard
            title="MTTD"
            value={mttd ? formatDuration(mttd.average_mttd_hours) : 'N/A'}
            icon={<TimeIcon />}
            color="#4caf50"
            subtitle="Mean Time to Detect"
          />
        </Grid>
        <Grid item xs={12} sm={6} md={3}>
          <StatCard
            title="MTTR"
            value={mttr ? formatDuration(mttr.average_mttr_hours) : 'N/A'}
            icon={<TimeIcon />}
            color="#f44336"
            subtitle="Mean Time to Respond"
          />
        </Grid>
      </Grid>

      {/* Charts Row 1 */}
      <Grid container spacing={3} mb={4}>
        <Grid item xs={12} md={8}>
          <Paper sx={{ p: 3 }}>
            <Typography variant="h6" gutterBottom>
              Cases by Priority
            </Typography>
            <ResponsiveContainer width="100%" height={300}>
              <BarChart data={byPriority}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="priority" />
                <YAxis />
                <Tooltip />
                <Legend />
                <Bar dataKey="count" fill="#2196f3" />
                <Bar dataKey="closed_count" fill="#4caf50" />
              </BarChart>
            </ResponsiveContainer>
          </Paper>
        </Grid>
        <Grid item xs={12} md={4}>
          <Paper sx={{ p: 3 }}>
            <Typography variant="h6" gutterBottom>
              Status Distribution
            </Typography>
            <ResponsiveContainer width="100%" height={300}>
              <PieChart>
                <Pie
                  data={byStatus}
                  cx="50%"
                  cy="50%"
                  labelLine={false}
                  label={(entry: any) => `${entry.status}: ${entry.count}`}
                  outerRadius={80}
                  fill="#8884d8"
                  dataKey="count"
                >
                  {byStatus.map((_entry, index) => (
                    <Cell key={`cell-${index}`} fill={COLORS[index % COLORS.length]} />
                  ))}
                </Pie>
                <Tooltip />
              </PieChart>
            </ResponsiveContainer>
          </Paper>
        </Grid>
      </Grid>

      {/* MTTD/MTTR Trends */}
      <Grid container spacing={3} mb={4}>
        <Grid item xs={12}>
          <Paper sx={{ p: 3 }}>
            <Typography variant="h6" gutterBottom>
              Response Time Trends (MTTD vs MTTR)
            </Typography>
            <ResponsiveContainer width="100%" height={300}>
              <LineChart
                data={[
                  { name: 'Critical', mttd: mttd?.by_priority?.critical || 0, mttr: mttr?.by_priority?.critical || 0 },
                  { name: 'High', mttd: mttd?.by_priority?.high || 0, mttr: mttr?.by_priority?.high || 0 },
                  { name: 'Medium', mttd: mttd?.by_priority?.medium || 0, mttr: mttr?.by_priority?.medium || 0 },
                  { name: 'Low', mttd: mttd?.by_priority?.low || 0, mttr: mttr?.by_priority?.low || 0 },
                ]}
              >
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="name" />
                <YAxis label={{ value: 'Hours', angle: -90, position: 'insideLeft' }} />
                <Tooltip formatter={(value: any) => formatDuration(value)} />
                <Legend />
                <Line type="monotone" dataKey="mttd" stroke="#4caf50" name="MTTD" strokeWidth={2} />
                <Line type="monotone" dataKey="mttr" stroke="#f44336" name="MTTR" strokeWidth={2} />
              </LineChart>
            </ResponsiveContainer>
          </Paper>
        </Grid>
      </Grid>

      {/* Analyst Performance */}
      <Grid container spacing={3}>
        <Grid item xs={12}>
          <Paper sx={{ p: 3 }}>
            <Typography variant="h6" gutterBottom>
              Analyst Performance
            </Typography>
            <TableContainer>
              <Table>
                <TableHead>
                  <TableRow>
                    <TableCell>Analyst</TableCell>
                    <TableCell align="right">Assigned Cases</TableCell>
                    <TableCell align="right">Closed Cases</TableCell>
                    <TableCell align="right">Avg Resolution Time</TableCell>
                    <TableCell align="right">Success Rate</TableCell>
                    <TableCell align="right">Performance</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {analystPerformance.map((analyst: any, index: number) => (
                    <TableRow key={analyst.analyst ?? `unassigned-${index}`}>
                      <TableCell>
                        <Box display="flex" alignItems="center" gap={1}>
                          <PersonIcon fontSize="small" color="primary" />
                          <Typography variant="body2" fontWeight="medium">
                            {analyst.analyst || 'Unassigned'}
                          </Typography>
                        </Box>
                      </TableCell>
                      <TableCell align="right">{analyst.total_cases}</TableCell>
                      <TableCell align="right">{analyst.closed_cases}</TableCell>
                      <TableCell align="right">
                        {formatDuration(analyst.avg_resolution_hours)}
                      </TableCell>
                      <TableCell align="right">
                        <Chip
                          label={`${getSuccessRate(analyst.closed_cases, analyst.total_cases)}%`}
                          color={
                            getSuccessRate(analyst.closed_cases, analyst.total_cases) > 80
                              ? 'success'
                              : getSuccessRate(analyst.closed_cases, analyst.total_cases) > 50
                              ? 'warning'
                              : 'error'
                          }
                          size="small"
                        />
                      </TableCell>
                      <TableCell align="right">
                        <LinearProgress
                          variant="determinate"
                          value={Math.min(100, getSuccessRate(analyst.closed_cases, analyst.total_cases))}
                          color={
                            getSuccessRate(analyst.closed_cases, analyst.total_cases) > 80
                              ? 'success'
                              : getSuccessRate(analyst.closed_cases, analyst.total_cases) > 50
                              ? 'warning'
                              : 'error'
                          }
                          sx={{ width: 100 }}
                        />
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableContainer>
          </Paper>
        </Grid>
      </Grid>
    </Container>
  )
}

