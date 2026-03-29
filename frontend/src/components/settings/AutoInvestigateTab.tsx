import { useState, useEffect } from 'react'
import {
  Box,
  Typography,
  TextField,
  Button,
  Switch,
  FormControlLabel,
  Divider,
  Chip,
  Card,
  CardContent,
  Alert,
  InputAdornment,
  CircularProgress,
  Grid,
} from '@mui/material'
import {
  Save as SaveIcon,
  RestartAlt as ResetIcon,
  SmartToy as AgentIcon,
} from '@mui/icons-material'
import { configApi, orchestratorApi } from '../../services/api'

interface OrchestratorConfig {
  enabled: boolean
  dry_run: boolean
  auto_assign_findings: boolean
  auto_assign_severities: string[]
  max_concurrent_agents: number
  max_iterations_per_agent: number
  max_runtime_per_investigation: number
  max_cost_per_investigation: number
  max_total_hourly_cost: number
  max_total_daily_cost: number
  loop_interval: number
  agent_loop_delay: number
  stale_threshold: number
  dedup_window_minutes: number
  context_max_chars: number
  plan_model: string
  review_model: string
  workdir_base: string
}

const DEFAULTS: OrchestratorConfig = {
  enabled: false,
  dry_run: false,
  auto_assign_findings: true,
  auto_assign_severities: ['critical', 'high'],
  max_concurrent_agents: 3,
  max_iterations_per_agent: 50,
  max_runtime_per_investigation: 3600,
  max_cost_per_investigation: 5.0,
  max_total_hourly_cost: 20.0,
  max_total_daily_cost: 100.0,
  loop_interval: 60,
  agent_loop_delay: 2,
  stale_threshold: 300,
  dedup_window_minutes: 30,
  context_max_chars: 10000,
  plan_model: 'claude-sonnet-4-5-20250929',
  review_model: 'claude-sonnet-4-5-20250929',
  workdir_base: 'data/investigations',
}

const ALL_SEVERITIES = ['critical', 'high', 'medium', 'low']

interface Props {
  onMessage: (msg: { type: 'success' | 'error'; text: string }) => void
  showConfirm: (title: string, msg: string, onConfirm: () => void) => void
}

export default function AutoInvestigateTab({ onMessage, showConfirm }: Props) {
  const [config, setConfig] = useState<OrchestratorConfig>(DEFAULTS)
  const [loading, setLoading] = useState(true)
  const [status, setStatus] = useState<any>(null)

  const loadConfig = async () => {
    try {
      const [cfgRes, statusRes] = await Promise.all([
        configApi.getOrchestrator().catch(() => ({ data: DEFAULTS })),
        orchestratorApi.getStatus().catch(() => ({ data: null })),
      ])
      setConfig({ ...DEFAULTS, ...cfgRes.data })
      setStatus(statusRes.data)
    } catch {
      /* use defaults */
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { loadConfig() }, [])

  const doSave = async () => {
    try {
      await configApi.setOrchestrator(config)
      onMessage({ type: 'success', text: 'Auto Investigate settings saved' })
      loadConfig()
    } catch {
      onMessage({ type: 'error', text: 'Failed to save Auto Investigate settings' })
    }
    setTimeout(() => onMessage({ type: 'success', text: '' }), 3000)
  }

  const handleSave = () => {
    showConfirm(
      'Save Auto Investigate Settings',
      'Are you sure you want to save these settings? Changes will take effect on the next daemon restart (or immediately if you toggle the enabled switch via the Auto Ops page).',
      doSave,
    )
  }

  const handleReset = () => {
    setConfig(DEFAULTS)
    onMessage({ type: 'success', text: 'Reset to defaults (not yet saved)' })
    setTimeout(() => onMessage({ type: 'success', text: '' }), 3000)
  }

  const toggleSeverity = (sev: string) => {
    const current = config.auto_assign_severities
    if (current.includes(sev)) {
      setConfig({ ...config, auto_assign_severities: current.filter(s => s !== sev) })
    } else {
      setConfig({ ...config, auto_assign_severities: [...current, sev] })
    }
  }

  const numField = (
    label: string,
    field: keyof OrchestratorConfig,
    opts?: { min?: number; max?: number; prefix?: string; suffix?: string; helperText?: string },
  ) => (
    <TextField
      fullWidth
      label={label}
      type="number"
      size="small"
      value={config[field]}
      onChange={(e) => {
        let val = Number(e.target.value)
        if (opts?.min !== undefined && val < opts.min) val = opts.min
        if (opts?.max !== undefined && val > opts.max) val = opts.max
        setConfig({ ...config, [field]: val })
      }}
      helperText={opts?.helperText}
      InputProps={{
        ...(opts?.prefix ? { startAdornment: <InputAdornment position="start">{opts.prefix}</InputAdornment> } : {}),
        ...(opts?.suffix ? { endAdornment: <InputAdornment position="end">{opts.suffix}</InputAdornment> } : {}),
      }}
      sx={{ mb: 2 }}
    />
  )

  if (loading) {
    return (
      <Box sx={{ display: 'flex', justifyContent: 'center', py: 6 }}>
        <CircularProgress />
      </Box>
    )
  }

  return (
    <Box>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 2 }}>
        <AgentIcon />
        <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>Auto Investigate</Typography>
      </Box>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        Configure the autonomous investigation orchestrator. When enabled, the system automatically investigates high-severity findings using AI sub-agents.
      </Typography>

      {status && (
        <Alert severity={status.enabled ? 'success' : 'info'} sx={{ mb: 3 }}>
          Orchestrator is <strong>{status.enabled ? 'ENABLED' : 'DISABLED'}</strong>
          {status.active_agents !== undefined && ` | ${status.active_agents} active agent(s)`}
          {status.cost?.total_cost_usd !== undefined && ` | Total cost: $${status.cost.total_cost_usd.toFixed(2)}`}
        </Alert>
      )}

      {/* Master Controls */}
      <Card variant="outlined" sx={{ mb: 3 }}>
        <CardContent>
          <Typography variant="subtitle2" sx={{ fontWeight: 600, mb: 2 }}>Master Controls</Typography>

          <FormControlLabel
            control={
              <Switch
                checked={config.enabled}
                onChange={(e) => setConfig({ ...config, enabled: e.target.checked })}
              />
            }
            label="Enable autonomous investigations"
          />

          <FormControlLabel
            control={
              <Switch
                checked={config.dry_run}
                onChange={(e) => setConfig({ ...config, dry_run: e.target.checked })}
              />
            }
            label="Dry run mode (agents gather data but skip write actions)"
          />

          <FormControlLabel
            control={
              <Switch
                checked={config.auto_assign_findings}
                onChange={(e) => setConfig({ ...config, auto_assign_findings: e.target.checked })}
              />
            }
            label="Auto-assign new findings for investigation"
          />

          <Divider sx={{ my: 2 }} />

          <Typography variant="body2" sx={{ mb: 1, fontWeight: 500 }}>Auto-investigate severities</Typography>
          <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
            {ALL_SEVERITIES.map(sev => (
              <Chip
                key={sev}
                label={sev.charAt(0).toUpperCase() + sev.slice(1)}
                variant={config.auto_assign_severities.includes(sev) ? 'filled' : 'outlined'}
                color={
                  sev === 'critical' ? 'error' :
                  sev === 'high' ? 'warning' :
                  sev === 'medium' ? 'info' : 'default'
                }
                onClick={() => toggleSeverity(sev)}
                sx={{ cursor: 'pointer' }}
              />
            ))}
          </Box>
        </CardContent>
      </Card>

      {/* Agent Limits */}
      <Card variant="outlined" sx={{ mb: 3 }}>
        <CardContent>
          <Typography variant="subtitle2" sx={{ fontWeight: 600, mb: 2 }}>Agent Limits</Typography>
          <Grid container spacing={2}>
            <Grid item xs={12} sm={4}>
              {numField('Max concurrent agents', 'max_concurrent_agents', { min: 1, max: 10, helperText: '1-10 simultaneous agents' })}
            </Grid>
            <Grid item xs={12} sm={4}>
              {numField('Max iterations per agent', 'max_iterations_per_agent', { min: 1, max: 500, helperText: 'Claude calls per investigation' })}
            </Grid>
            <Grid item xs={12} sm={4}>
              {numField('Max runtime (seconds)', 'max_runtime_per_investigation', { min: 60, max: 86400, suffix: 's', helperText: `${Math.round(config.max_runtime_per_investigation / 60)} minutes` })}
            </Grid>
          </Grid>
        </CardContent>
      </Card>

      {/* Cost Guardrails */}
      <Card variant="outlined" sx={{ mb: 3 }}>
        <CardContent>
          <Typography variant="subtitle2" sx={{ fontWeight: 600, mb: 2 }}>Cost Guardrails</Typography>
          <Grid container spacing={2}>
            <Grid item xs={12} sm={4}>
              {numField('Per investigation limit', 'max_cost_per_investigation', { min: 0.5, max: 100, prefix: '$', helperText: 'Max spend per investigation' })}
            </Grid>
            <Grid item xs={12} sm={4}>
              {numField('Hourly cost limit', 'max_total_hourly_cost', { min: 1, max: 500, prefix: '$', helperText: 'Pause intake if exceeded' })}
            </Grid>
            <Grid item xs={12} sm={4}>
              {numField('Daily cost limit', 'max_total_daily_cost', { min: 1, max: 1000, prefix: '$', helperText: 'Hard daily ceiling' })}
            </Grid>
          </Grid>
        </CardContent>
      </Card>

      {/* Advanced */}
      <Card variant="outlined" sx={{ mb: 3 }}>
        <CardContent>
          <Typography variant="subtitle2" sx={{ fontWeight: 600, mb: 2 }}>Timing &amp; Advanced</Typography>
          <Grid container spacing={2}>
            <Grid item xs={12} sm={4}>
              {numField('Loop interval', 'loop_interval', { min: 10, max: 600, suffix: 's', helperText: 'Orchestrator check interval' })}
            </Grid>
            <Grid item xs={12} sm={4}>
              {numField('Agent loop delay', 'agent_loop_delay', { min: 1, max: 30, suffix: 's', helperText: 'Pause between agent iterations' })}
            </Grid>
            <Grid item xs={12} sm={4}>
              {numField('Stale threshold', 'stale_threshold', { min: 60, max: 3600, suffix: 's', helperText: 'Kill idle agents after this' })}
            </Grid>
            <Grid item xs={12} sm={4}>
              {numField('Dedup window', 'dedup_window_minutes', { min: 5, max: 1440, suffix: 'min', helperText: 'Overlap detection window' })}
            </Grid>
            <Grid item xs={12} sm={4}>
              {numField('Context max chars', 'context_max_chars', { min: 1000, max: 100000, helperText: 'Max context.md in prompt' })}
            </Grid>
          </Grid>

          <Divider sx={{ my: 2 }} />

          <Grid container spacing={2}>
            <Grid item xs={12} sm={4}>
              <TextField
                fullWidth
                label="Plan model"
                size="small"
                value={config.plan_model}
                onChange={(e) => setConfig({ ...config, plan_model: e.target.value })}
                helperText="Claude model for agent work"
                sx={{ mb: 2 }}
              />
            </Grid>
            <Grid item xs={12} sm={4}>
              <TextField
                fullWidth
                label="Review model"
                size="small"
                value={config.review_model}
                onChange={(e) => setConfig({ ...config, review_model: e.target.value })}
                helperText="Claude model for master review"
                sx={{ mb: 2 }}
              />
            </Grid>
            <Grid item xs={12} sm={4}>
              <TextField
                fullWidth
                label="Working directory"
                size="small"
                value={config.workdir_base}
                onChange={(e) => setConfig({ ...config, workdir_base: e.target.value })}
                helperText="Base path for investigation files"
                sx={{ mb: 2 }}
              />
            </Grid>
          </Grid>
        </CardContent>
      </Card>

      {/* Actions */}
      <Box sx={{ display: 'flex', gap: 2 }}>
        <Button variant="contained" startIcon={<SaveIcon />} onClick={handleSave}>
          Save Settings
        </Button>
        <Button variant="outlined" startIcon={<ResetIcon />} onClick={handleReset}>
          Reset to Defaults
        </Button>
      </Box>
    </Box>
  )
}
