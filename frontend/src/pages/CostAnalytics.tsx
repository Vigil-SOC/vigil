import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  Grid,
  IconButton,
  Paper,
  Skeleton,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  ToggleButton,
  ToggleButtonGroup,
  Tooltip as MuiTooltip,
  Typography,
} from '@mui/material'
import {
  AttachMoney as CostIcon,
  Cached as CacheIcon,
  Psychology as AgentIcon,
  Refresh as RefreshIcon,
  SmartToy as ModelIcon,
  Calculate as RecalculateIcon,
} from '@mui/icons-material'
import { analyticsApi } from '../services/api'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  ResponsiveContainer,
  Tooltip as RechartsTooltip,
  XAxis,
  YAxis,
} from 'recharts'
import api from '../services/api'

type TimeRange = '24h' | '7d' | '30d' | 'all'

interface AgentBreakdown {
  agent_id: string
  calls: number
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_creation_tokens: number
  cost_usd: number
  cache_hit_rate: number
}

interface ModelBreakdown {
  model: string
  // #184 Phase 3: provider_type is inferred from the model id server-side
  // when the row is built; pricing_source tells the UI whether cost_usd
  // came from an exact catalog rate, a tier-regex heuristic, the
  // self-hosted "$0" branch, or no data at all.
  provider_type: string
  pricing_source: 'exact' | 'heuristic' | 'zero' | 'unknown'
  calls: number
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_creation_tokens: number
  cost_usd: number
  cache_hit_rate: number
}

interface TopInvestigation {
  investigation_id: string
  calls: number
  input_tokens: number
  output_tokens: number
  cost_usd: number
}

interface CostAnalyticsResponse {
  window: { start: string; end: string; seconds: number }
  totals: {
    calls: number
    input_tokens: number
    output_tokens: number
    cache_read_tokens: number
    cache_creation_tokens: number
    cost_usd: number
    cache_hit_rate: number
  }
  by_agent: AgentBreakdown[]
  by_model: ModelBreakdown[]
  top_investigations: TopInvestigation[]
}

const AGENT_COLORS = [
  '#1976d2',
  '#9c27b0',
  '#f57c00',
  '#388e3c',
  '#d32f2f',
  '#0097a7',
  '#7b1fa2',
  '#c62828',
  '#00796b',
  '#5d4037',
]

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`
  return String(n)
}

function formatCost(n: number): string {
  return `$${n.toFixed(2)}`
}

function formatPercent(n: number): string {
  return `${(n * 100).toFixed(1)}%`
}

// #184 Phase 3: turn the server's pricing_source string into a UI-visible
// badge. The point is to distinguish a real $0 row (Ollama, self-hosted)
// from a $0 row that's a missing-data fallback ("unknown") so an operator
// can tell whether the dashboard is reporting reality or hiding gaps.
function PricingSourceBadge({ source }: { source: ModelBreakdown['pricing_source'] }) {
  const config: Record<
    ModelBreakdown['pricing_source'],
    { label: string; color: 'success' | 'warning' | 'info' | 'error'; tooltip: string }
  > = {
    exact: {
      label: 'exact',
      color: 'success',
      tooltip: 'Pricing from hand-verified catalog entry.',
    },
    heuristic: {
      label: 'heuristic',
      color: 'warning',
      tooltip:
        'Pricing inferred from a tier regex (e.g. "any sonnet variant"). Approximate.',
    },
    zero: {
      label: 'free',
      color: 'info',
      tooltip: 'Self-hosted (Ollama). No upstream API spend; compute cost not tracked.',
    },
    unknown: {
      label: 'unknown',
      color: 'error',
      tooltip:
        'No catalog entry matched. Cost recorded as $0 — real spend is hidden until pricing is added.',
    },
  }
  const { label, color, tooltip } = config[source] || config.unknown
  return (
    <MuiTooltip title={tooltip}>
      <Chip size="small" label={label} color={color} variant="outlined" />
    </MuiTooltip>
  )
}

export default function CostAnalytics() {
  const [timeRange, setTimeRange] = useState<TimeRange>('7d')
  const [data, setData] = useState<CostAnalyticsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  // #185: cumulative recalc state. Bifrost caps each call at 1000 rows;
  // we loop until `remaining == 0` so the operator doesn't have to.
  const [recalcRunning, setRecalcRunning] = useState(false)
  const [recalcStatus, setRecalcStatus] = useState<string | null>(null)

  const handleRecalculate = async () => {
    setRecalcRunning(true)
    setRecalcStatus('Asking Bifrost to recompute against current pricing…')
    try {
      let totalUpdated = 0
      let totalSkipped = 0
      // Cap iterations so a stuck loop doesn't hammer Bifrost forever.
      for (let pass = 0; pass < 50; pass++) {
        const res = await analyticsApi.recalculateCost({
          missing_cost_only: true,
          limit: 1000,
        })
        totalUpdated += res.data.updated || 0
        totalSkipped += res.data.skipped || 0
        if ((res.data.remaining || 0) <= 0) break
        setRecalcStatus(
          `Reprocessed ${totalUpdated} so far, ${res.data.remaining} remaining…`,
        )
      }
      setRecalcStatus(
        `Recalculate complete — ${totalUpdated} rows updated, ${totalSkipped} skipped.`,
      )
      // Refresh the dashboard so the new cost numbers show up.
      fetchData()
    } catch (e: any) {
      setRecalcStatus(
        `Recalculate failed: ${e?.response?.data?.detail || e?.message || 'unknown error'}`,
      )
    } finally {
      setRecalcRunning(false)
    }
  }

  const fetchData = async () => {
    setLoading(true)
    setError(null)
    try {
      const response = await api.get<CostAnalyticsResponse>('/analytics/cost', {
        params: { time_range: timeRange },
      })
      setData(response.data)
    } catch (err: any) {
      setError(err?.response?.data?.detail || err?.message || 'Failed to load cost analytics')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchData()
  }, [timeRange])

  const agentChartData = useMemo(() => {
    if (!data) return []
    return data.by_agent.slice(0, 10).map((row) => ({
      agent: row.agent_id,
      cost: Number(row.cost_usd.toFixed(4)),
      calls: row.calls,
    }))
  }, [data])

  const modelChartData = useMemo(() => {
    if (!data) return []
    return data.by_model.map((row) => ({
      model: row.model,
      input: row.input_tokens,
      cached: row.cache_read_tokens,
      output: row.output_tokens,
    }))
  }, [data])

  return (
    <Box sx={{ p: 3 }}>
      <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 3 }}>
        <Box>
          <Typography variant="h4" sx={{ fontWeight: 600 }}>
            LLM Cost Analytics
          </Typography>
          <Typography variant="body2" color="text.secondary">
            Token spend + cache performance by agent, model, and investigation
          </Typography>
        </Box>
        <Box sx={{ display: 'flex', gap: 1, alignItems: 'center' }}>
          <ToggleButtonGroup
            value={timeRange}
            exclusive
            size="small"
            onChange={(_, v) => v && setTimeRange(v)}
          >
            <ToggleButton value="24h">24h</ToggleButton>
            <ToggleButton value="7d">7d</ToggleButton>
            <ToggleButton value="30d">30d</ToggleButton>
            <ToggleButton value="all">All</ToggleButton>
          </ToggleButtonGroup>
          <IconButton onClick={fetchData} size="small" disabled={loading}>
            <RefreshIcon />
          </IconButton>
          <MuiTooltip title="Re-cost historical Bifrost log rows against current pricing. Use after Anthropic/OpenAI publishes new rates.">
            <span>
              <Button
                size="small"
                variant="outlined"
                startIcon={<RecalculateIcon />}
                onClick={handleRecalculate}
                disabled={recalcRunning}
              >
                {recalcRunning ? 'Recalculating…' : 'Recalculate cost'}
              </Button>
            </span>
          </MuiTooltip>
        </Box>
      </Box>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      {recalcStatus && (
        <Alert
          severity={recalcStatus.startsWith('Recalculate failed') ? 'error' : 'info'}
          sx={{ mb: 2 }}
          onClose={() => setRecalcStatus(null)}
        >
          {recalcStatus}
        </Alert>
      )}

      <Grid container spacing={2} sx={{ mb: 3 }}>
        {[
          {
            icon: <CostIcon sx={{ fontSize: 28, color: '#9c27b0' }} />,
            label: 'Total Cost',
            value: data ? formatCost(data.totals.cost_usd) : '—',
          },
          {
            icon: <AgentIcon sx={{ fontSize: 28, color: '#2196f3' }} />,
            label: 'LLM Calls',
            value: data ? data.totals.calls.toLocaleString() : '—',
          },
          {
            icon: <CacheIcon sx={{ fontSize: 28, color: '#388e3c' }} />,
            label: 'Cache Hit Rate',
            value: data ? formatPercent(data.totals.cache_hit_rate) : '—',
            help: 'cached_input / (cached_input + new_input). Expected 0 until prompt caching ships (GH #84 PR-C).',
          },
          {
            icon: <ModelIcon sx={{ fontSize: 28, color: '#f57c00' }} />,
            label: 'Input Tokens',
            value: data ? formatTokens(data.totals.input_tokens + data.totals.cache_read_tokens) : '—',
          },
          {
            icon: <ModelIcon sx={{ fontSize: 28, color: '#757575' }} />,
            label: 'Output Tokens',
            value: data ? formatTokens(data.totals.output_tokens) : '—',
          },
        ].map((card) => (
          <Grid item xs={6} sm={4} md={2.4} key={card.label}>
            <MuiTooltip title={card.help || ''} placement="top" arrow disableHoverListener={!card.help}>
              <Card sx={{ height: '100%' }}>
                <CardContent sx={{ textAlign: 'center', py: 2, px: 1.5, '&:last-child': { pb: 2 } }}>
                  {card.icon}
                  <Typography variant="h5" sx={{ fontWeight: 700, mt: 0.5 }}>
                    {loading && !data ? <Skeleton width={80} sx={{ mx: 'auto' }} /> : card.value}
                  </Typography>
                  <Typography variant="caption" color="text.secondary">
                    {card.label}
                  </Typography>
                </CardContent>
              </Card>
            </MuiTooltip>
          </Grid>
        ))}
      </Grid>

      <Grid container spacing={2} sx={{ mb: 3 }}>
        <Grid item xs={12} md={6}>
          <Paper sx={{ p: 2, height: 380 }}>
            <Typography variant="h6" sx={{ mb: 2 }}>
              Cost by Agent
            </Typography>
            {loading && !data ? (
              <Skeleton variant="rectangular" height={300} />
            ) : agentChartData.length === 0 ? (
              <Typography color="text.secondary">No LLM calls in this window.</Typography>
            ) : (
              <ResponsiveContainer width="100%" height={300}>
                <BarChart data={agentChartData}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="agent" angle={-30} textAnchor="end" height={80} fontSize={11} />
                  <YAxis tickFormatter={(v) => `$${v}`} />
                  <RechartsTooltip formatter={(v: number) => formatCost(v)} />
                  <Bar dataKey="cost" name="Cost (USD)">
                    {agentChartData.map((_, idx) => (
                      <Cell key={idx} fill={AGENT_COLORS[idx % AGENT_COLORS.length]} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            )}
          </Paper>
        </Grid>

        <Grid item xs={12} md={6}>
          <Paper sx={{ p: 2, height: 380 }}>
            <Typography variant="h6" sx={{ mb: 2 }}>
              Tokens by Model
            </Typography>
            {loading && !data ? (
              <Skeleton variant="rectangular" height={300} />
            ) : modelChartData.length === 0 ? (
              <Typography color="text.secondary">No LLM calls in this window.</Typography>
            ) : (
              <ResponsiveContainer width="100%" height={300}>
                <BarChart data={modelChartData}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="model" angle={-30} textAnchor="end" height={80} fontSize={11} />
                  <YAxis tickFormatter={(v) => formatTokens(v)} />
                  <RechartsTooltip formatter={(v: number) => formatTokens(v)} />
                  <Legend />
                  <Bar dataKey="input" stackId="tokens" name="New input" fill="#1976d2" />
                  <Bar dataKey="cached" stackId="tokens" name="Cache read" fill="#388e3c" />
                  <Bar dataKey="output" stackId="tokens" name="Output" fill="#f57c00" />
                </BarChart>
              </ResponsiveContainer>
            )}
          </Paper>
        </Grid>
      </Grid>

      <Paper sx={{ mb: 3 }}>
        <Box sx={{ p: 2 }}>
          <Typography variant="h6">Per-Agent Breakdown</Typography>
        </Box>
        <TableContainer>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>Agent</TableCell>
                <TableCell align="right">Calls</TableCell>
                <TableCell align="right">Input</TableCell>
                <TableCell align="right">Cache read</TableCell>
                <TableCell align="right">Cache write</TableCell>
                <TableCell align="right">Output</TableCell>
                <TableCell align="right">Cache hit</TableCell>
                <TableCell align="right">Cost</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {(data?.by_agent || []).map((row) => (
                <TableRow key={row.agent_id} hover>
                  <TableCell>
                    <Chip size="small" label={row.agent_id} />
                  </TableCell>
                  <TableCell align="right">{row.calls}</TableCell>
                  <TableCell align="right">{formatTokens(row.input_tokens)}</TableCell>
                  <TableCell align="right">{formatTokens(row.cache_read_tokens)}</TableCell>
                  <TableCell align="right">{formatTokens(row.cache_creation_tokens)}</TableCell>
                  <TableCell align="right">{formatTokens(row.output_tokens)}</TableCell>
                  <TableCell align="right">{formatPercent(row.cache_hit_rate)}</TableCell>
                  <TableCell align="right">{formatCost(row.cost_usd)}</TableCell>
                </TableRow>
              ))}
              {(!data || data.by_agent.length === 0) && !loading && (
                <TableRow>
                  <TableCell colSpan={8} align="center">
                    <Typography color="text.secondary" sx={{ py: 2 }}>
                      No LLM calls in this window.
                    </Typography>
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </TableContainer>
      </Paper>

      <Paper sx={{ mb: 3 }}>
        <Box sx={{ p: 2 }}>
          <Typography variant="h6">Per-Model Breakdown</Typography>
          <Typography variant="caption" color="text.secondary">
            Rows badged "heuristic" or "unknown" use approximate pricing — see the
            tooltip on each badge for what that means.
          </Typography>
        </Box>
        <TableContainer>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>Model</TableCell>
                <TableCell>Provider</TableCell>
                <TableCell>Pricing</TableCell>
                <TableCell align="right">Calls</TableCell>
                <TableCell align="right">Input</TableCell>
                <TableCell align="right">Cache read</TableCell>
                <TableCell align="right">Cache write</TableCell>
                <TableCell align="right">Output</TableCell>
                <TableCell align="right">Cache hit</TableCell>
                <TableCell align="right">Cost</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {(data?.by_model || []).map((row) => (
                <TableRow key={row.model} hover>
                  <TableCell>
                    <Typography variant="body2" sx={{ fontFamily: 'monospace' }}>
                      {row.model}
                    </Typography>
                  </TableCell>
                  <TableCell>
                    <Chip size="small" label={row.provider_type} variant="outlined" />
                  </TableCell>
                  <TableCell>
                    <PricingSourceBadge source={row.pricing_source} />
                  </TableCell>
                  <TableCell align="right">{row.calls}</TableCell>
                  <TableCell align="right">{formatTokens(row.input_tokens)}</TableCell>
                  <TableCell align="right">{formatTokens(row.cache_read_tokens)}</TableCell>
                  <TableCell align="right">{formatTokens(row.cache_creation_tokens)}</TableCell>
                  <TableCell align="right">{formatTokens(row.output_tokens)}</TableCell>
                  <TableCell align="right">{formatPercent(row.cache_hit_rate)}</TableCell>
                  <TableCell align="right">{formatCost(row.cost_usd)}</TableCell>
                </TableRow>
              ))}
              {(!data || data.by_model.length === 0) && !loading && (
                <TableRow>
                  <TableCell colSpan={10} align="center">
                    <Typography color="text.secondary" sx={{ py: 2 }}>
                      No LLM calls in this window.
                    </Typography>
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </TableContainer>
      </Paper>

      <Paper>
        <Box sx={{ p: 2 }}>
          <Typography variant="h6">Top Investigations by Cost</Typography>
        </Box>
        <TableContainer>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>Investigation</TableCell>
                <TableCell align="right">Calls</TableCell>
                <TableCell align="right">Input tokens</TableCell>
                <TableCell align="right">Output tokens</TableCell>
                <TableCell align="right">Cost</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {(data?.top_investigations || []).map((row) => (
                <TableRow key={row.investigation_id} hover>
                  <TableCell>
                    <Typography variant="body2" sx={{ fontFamily: 'monospace' }}>
                      {row.investigation_id}
                    </Typography>
                  </TableCell>
                  <TableCell align="right">{row.calls}</TableCell>
                  <TableCell align="right">{formatTokens(row.input_tokens)}</TableCell>
                  <TableCell align="right">{formatTokens(row.output_tokens)}</TableCell>
                  <TableCell align="right">{formatCost(row.cost_usd)}</TableCell>
                </TableRow>
              ))}
              {(!data || data.top_investigations.length === 0) && !loading && (
                <TableRow>
                  <TableCell colSpan={5} align="center">
                    <Typography color="text.secondary" sx={{ py: 2 }}>
                      No investigations with recorded LLM cost in this window.
                    </Typography>
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </TableContainer>
      </Paper>
    </Box>
  )
}
