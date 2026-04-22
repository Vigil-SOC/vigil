import { useEffect, useRef, useState } from 'react'
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Divider,
  FormControl,
  FormControlLabel,
  Grid,
  InputAdornment,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  Switch,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import RestoreIcon from '@mui/icons-material/Restore'
import { aiConfigApi, AIModelInfo, configApi, orchestratorApi } from '../../services/api'

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
  enabled: true,
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
  // Kept for backwards compatibility with Settings.tsx wiring; no longer used
  // now that the tab auto-saves. Safe to remove if Settings.tsx is updated.
  showConfirm?: (title: string, msg: string, onConfirm: () => void) => void
}

export default function AutoInvestigateTab({ onMessage }: Props) {
  const [config, setConfig] = useState<OrchestratorConfig>(DEFAULTS)
  const [loading, setLoading] = useState(true)
  const [status, setStatus] = useState<any>(null)
  const [models, setModels] = useState<AIModelInfo[]>([])
  const lastSaved = useRef<OrchestratorConfig>(DEFAULTS)

  const loadConfig = async () => {
    try {
      const [cfgRes, statusRes, modelsRes] = await Promise.all([
        configApi.getOrchestrator().catch(() => ({ data: DEFAULTS })),
        orchestratorApi.getStatus().catch(() => ({ data: null })),
        aiConfigApi.listModels().catch(() => ({ data: { models: [] } })),
      ])
      const merged = { ...DEFAULTS, ...cfgRes.data }
      setConfig(merged)
      lastSaved.current = merged
      setStatus(statusRes.data)
      setModels(modelsRes.data.models || [])
    } catch {
      /* use defaults */
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadConfig()
  }, [])

  const persist = async (next: OrchestratorConfig) => {
    try {
      await configApi.setOrchestrator(next)
      lastSaved.current = next
      onMessage({ type: 'success', text: 'Auto Investigate settings saved' })
      setTimeout(() => onMessage({ type: 'success', text: '' }), 2500)
    } catch {
      onMessage({ type: 'error', text: 'Failed to save Auto Investigate settings' })
    }
  }

  // Apply + persist a partial change. Used for toggles and chip clicks that
  // should save immediately (no blur event to hook into).
  const applyAndSave = (patch: Partial<OrchestratorConfig>) => {
    const next = { ...config, ...patch }
    setConfig(next)
    persist(next)
  }

  const toggleSeverity = (sev: string) => {
    const current = config.auto_assign_severities
    const next = current.includes(sev)
      ? current.filter((s) => s !== sev)
      : [...current, sev]
    applyAndSave({ auto_assign_severities: next })
  }

  const handleReset = () => {
    setConfig(DEFAULTS)
    persist(DEFAULTS)
  }

  // 0 is the sentinel for "unlimited" across all bounded numeric fields.
  // The daemon treats 0 as no-limit for cost/time/concurrency caps.
  const isUnlimited = (field: keyof OrchestratorConfig) =>
    (config[field] as number) === 0

  const numField = (
    label: string,
    field: keyof OrchestratorConfig,
    opts?: {
      min?: number
      max?: number
      prefix?: string
      suffix?: string
      helperText?: string
      allowUnlimited?: boolean
    },
  ) => {
    const unlimited = Boolean(opts?.allowUnlimited) && isUnlimited(field)
    return (
      <Box>
        <Tooltip title={opts?.helperText || ''} placement="top" arrow>
          <TextField
            fullWidth
            label={label}
            type="number"
            size="small"
            value={unlimited ? '' : (config[field] as number)}
            disabled={unlimited}
            placeholder={unlimited ? 'Unlimited' : ''}
            onChange={(e) => {
              let val = Number(e.target.value)
              if (opts?.min !== undefined && val < opts.min) val = opts.min
              if (opts?.max !== undefined && val > opts.max) val = opts.max
              setConfig((prev) => ({ ...prev, [field]: val }))
            }}
            onBlur={() => {
              if (config[field] !== lastSaved.current[field]) persist(config)
            }}
            helperText={opts?.helperText}
            InputProps={{
              ...(opts?.prefix
                ? {
                    startAdornment: (
                      <InputAdornment position="start">{opts.prefix}</InputAdornment>
                    ),
                  }
                : {}),
              ...(opts?.suffix
                ? {
                    endAdornment: (
                      <InputAdornment position="end">{opts.suffix}</InputAdornment>
                    ),
                  }
                : {}),
            }}
          />
        </Tooltip>
        {opts?.allowUnlimited && (
          <FormControlLabel
            sx={{ ml: 0, mt: 0.25 }}
            control={
              <Switch
                size="small"
                checked={unlimited}
                onChange={(e) => {
                  if (e.target.checked) {
                    // save sentinel immediately
                    applyAndSave({ [field]: 0 } as Partial<OrchestratorConfig>)
                  } else {
                    // restore to default value for this field
                    const def = DEFAULTS[field] as number
                    applyAndSave({ [field]: def } as Partial<OrchestratorConfig>)
                  }
                }}
              />
            }
            label={
              <Typography variant="caption" color="text.secondary">
                Unlimited
              </Typography>
            }
          />
        )}
      </Box>
    )
  }

  const modelSelect = (
    label: string,
    field: 'plan_model' | 'review_model',
    helperText: string,
  ) => {
    const current = config[field] as string
    const options = models.map((m) => m.model_id)
    const hasCurrent = !current || options.includes(current)
    // If the saved value isn't in the live-fetched list (e.g., backend has
    // a stale default), still show it so the user isn't surprised.
    const shownOptions = hasCurrent ? options : [...options, current]
    return (
      <FormControl fullWidth size="small">
        <InputLabel>{label}</InputLabel>
        <Select
          label={label}
          value={current}
          onChange={(e) => applyAndSave({ [field]: e.target.value as string } as Partial<OrchestratorConfig>)}
          displayEmpty
        >
          {shownOptions.length === 0 && (
            <MenuItem value="" disabled>
              <em>No models available — add a provider in AI Config</em>
            </MenuItem>
          )}
          {shownOptions.map((id) => {
            const info = models.find((m) => m.model_id === id)
            return (
              <MenuItem key={id} value={id}>
                <Stack>
                  <Typography variant="body2">
                    {info?.display_name || id}
                  </Typography>
                  {info && (
                    <Typography variant="caption" color="text.secondary">
                      {info.provider_id} ·{' '}
                      {info.context_window
                        ? `${Math.round(info.context_window / 1000)}K ctx`
                        : ''}
                    </Typography>
                  )}
                </Stack>
              </MenuItem>
            )
          })}
        </Select>
        <Typography variant="caption" color="text.secondary" sx={{ mt: 0.5 }}>
          {helperText}
        </Typography>
      </FormControl>
    )
  }

  const textField = (
    label: string,
    field: keyof OrchestratorConfig,
    helperText: string,
  ) => (
    <TextField
      fullWidth
      label={label}
      size="small"
      value={config[field] as string}
      onChange={(e) =>
        setConfig((prev) => ({ ...prev, [field]: e.target.value }))
      }
      onBlur={() => {
        if (config[field] !== lastSaved.current[field]) persist(config)
      }}
      helperText={helperText}
    />
  )

  if (loading) {
    return (
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, p: 2 }}>
        <CircularProgress size={16} />
        <Typography variant="body2">Loading Auto Investigate config…</Typography>
      </Box>
    )
  }

  return (
    <Box>
      <Typography variant="subtitle1" sx={{ fontWeight: 600, mb: 0.5 }}>
        Auto Investigate
      </Typography>
      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 2 }}>
        Runtime toggles for the autonomous investigation orchestrator. Changes
        save automatically and take effect across backend / daemon / llm-worker
        within ~60 seconds (the runtime-config cache TTL).
      </Typography>

      {status && (
        <Alert severity={status.enabled ? 'success' : 'info'} sx={{ mb: 3 }}>
          Orchestrator is <strong>{status.enabled ? 'ENABLED' : 'DISABLED'}</strong>
          {status.active_agents !== undefined && ` · ${status.active_agents} active agent(s)`}
          {status.cost?.total_cost_usd !== undefined &&
            ` · Total cost: $${status.cost.total_cost_usd.toFixed(2)}`}
        </Alert>
      )}

      {/* Master Controls */}
      <Typography variant="subtitle2" sx={{ fontWeight: 600, mb: 1.5 }}>
        Master Controls
      </Typography>
      <Stack spacing={0.5} sx={{ mb: 2 }}>
        <FormControlLabel
          control={
            <Switch
              checked={config.enabled}
              onChange={(e) => applyAndSave({ enabled: e.target.checked })}
            />
          }
          label="Enable autonomous investigations"
        />
        <FormControlLabel
          control={
            <Switch
              checked={config.dry_run}
              onChange={(e) => applyAndSave({ dry_run: e.target.checked })}
            />
          }
          label="Dry run mode (agents gather data but skip write actions)"
        />
        <FormControlLabel
          control={
            <Switch
              checked={config.auto_assign_findings}
              onChange={(e) =>
                applyAndSave({ auto_assign_findings: e.target.checked })
              }
            />
          }
          label="Auto-assign new findings for investigation"
        />
      </Stack>
      <Typography variant="body2" sx={{ mb: 1, fontWeight: 500 }}>
        Auto-investigate severities
      </Typography>
      <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap', mb: 3 }}>
        {ALL_SEVERITIES.map((sev) => (
          <Chip
            key={sev}
            label={sev.charAt(0).toUpperCase() + sev.slice(1)}
            variant={config.auto_assign_severities.includes(sev) ? 'filled' : 'outlined'}
            color={
              sev === 'critical'
                ? 'error'
                : sev === 'high'
                  ? 'warning'
                  : sev === 'medium'
                    ? 'info'
                    : 'default'
            }
            onClick={() => toggleSeverity(sev)}
            sx={{ cursor: 'pointer' }}
          />
        ))}
      </Box>

      <Divider sx={{ my: 3 }} />

      {/* Agent Limits */}
      <Typography variant="subtitle2" sx={{ fontWeight: 600, mb: 1.5 }}>
        Agent Limits
      </Typography>
      <Grid container spacing={2} sx={{ mb: 3 }}>
        <Grid item xs={12} sm={4}>
          {numField('Max concurrent agents', 'max_concurrent_agents', {
            min: 1,
            max: 10,
            helperText: '1-10 simultaneous agents',
            allowUnlimited: true,
          })}
        </Grid>
        <Grid item xs={12} sm={4}>
          {numField('Max iterations per agent', 'max_iterations_per_agent', {
            min: 1,
            max: 500,
            helperText: 'Claude calls per investigation',
            allowUnlimited: true,
          })}
        </Grid>
        <Grid item xs={12} sm={4}>
          {numField('Max runtime (seconds)', 'max_runtime_per_investigation', {
            min: 60,
            max: 86400,
            suffix: 's',
            helperText: `${Math.round(config.max_runtime_per_investigation / 60)} minutes`,
            allowUnlimited: true,
          })}
        </Grid>
      </Grid>

      <Divider sx={{ my: 3 }} />

      {/* Cost Guardrails */}
      <Typography variant="subtitle2" sx={{ fontWeight: 600, mb: 1.5 }}>
        Cost Guardrails
      </Typography>
      <Grid container spacing={2} sx={{ mb: 3 }}>
        <Grid item xs={12} sm={4}>
          {numField('Per investigation limit', 'max_cost_per_investigation', {
            min: 0.5,
            max: 100,
            prefix: '$',
            helperText: 'Max spend per investigation',
            allowUnlimited: true,
          })}
        </Grid>
        <Grid item xs={12} sm={4}>
          {numField('Hourly cost limit', 'max_total_hourly_cost', {
            min: 1,
            max: 500,
            prefix: '$',
            helperText: 'Pause intake if exceeded',
            allowUnlimited: true,
          })}
        </Grid>
        <Grid item xs={12} sm={4}>
          {numField('Daily cost limit', 'max_total_daily_cost', {
            min: 1,
            max: 1000,
            prefix: '$',
            helperText: 'Hard daily ceiling',
            allowUnlimited: true,
          })}
        </Grid>
      </Grid>

      <Divider sx={{ my: 3 }} />

      {/* Timing & Advanced */}
      <Typography variant="subtitle2" sx={{ fontWeight: 600, mb: 1.5 }}>
        Timing &amp; Advanced
      </Typography>
      <Grid container spacing={2} sx={{ mb: 2 }}>
        <Grid item xs={12} sm={4}>
          {numField('Loop interval', 'loop_interval', {
            min: 10,
            max: 600,
            suffix: 's',
            helperText: 'Orchestrator check interval',
          })}
        </Grid>
        <Grid item xs={12} sm={4}>
          {numField('Agent loop delay', 'agent_loop_delay', {
            min: 1,
            max: 30,
            suffix: 's',
            helperText: 'Pause between agent iterations',
          })}
        </Grid>
        <Grid item xs={12} sm={4}>
          {numField('Stale threshold', 'stale_threshold', {
            min: 60,
            max: 3600,
            suffix: 's',
            helperText: 'Kill idle agents after this',
          })}
        </Grid>
        <Grid item xs={12} sm={4}>
          {numField('Dedup window', 'dedup_window_minutes', {
            min: 5,
            max: 1440,
            suffix: 'min',
            helperText: 'Overlap detection window',
          })}
        </Grid>
        <Grid item xs={12} sm={4}>
          {numField('Context max chars', 'context_max_chars', {
            min: 1000,
            max: 100000,
            helperText: 'Max context.md in prompt',
          })}
        </Grid>
      </Grid>

      <Grid container spacing={2} sx={{ mb: 3 }}>
        <Grid item xs={12} sm={4}>
          {modelSelect('Plan model', 'plan_model', 'Model for agent planning')}
        </Grid>
        <Grid item xs={12} sm={4}>
          {modelSelect('Review model', 'review_model', 'Model for master review')}
        </Grid>
        <Grid item xs={12} sm={4}>
          {textField('Working directory', 'workdir_base', 'Base path for investigation files')}
        </Grid>
      </Grid>

      <Divider sx={{ my: 3 }} />

      <Stack direction="row" spacing={2}>
        <Button variant="outlined" startIcon={<RestoreIcon />} onClick={handleReset}>
          Reset to defaults
        </Button>
      </Stack>
    </Box>
  )
}
