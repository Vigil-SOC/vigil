/**
 * AIOperationsTab — runtime AI cost/perf toggles (GH #84 PR-F).
 *
 * Surfaces four knobs that previously lived only as env vars:
 *   - prompt_cache_enabled     → Anthropic native prompt caching (PR-C)
 *   - history_window           → sliding-window conversation history (PR-D)
 *   - tool_response_budget_default → tiered tool-result truncation (PR-D)
 *   - thinking_budget          → daemon-default extended thinking budget (PR-D)
 *
 * Values persist in ``system_config['ai_operations.settings']`` via
 * ``POST /config/ai-operations``. Readers (``services.runtime_config``)
 * pick them up on their next 60s cache-TTL miss across backend /
 * soc-daemon / llm-worker processes.
 */

import { useEffect, useState } from 'react'
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  FormControlLabel,
  Grid,
  Paper,
  Stack,
  Switch,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import SaveIcon from '@mui/icons-material/Save'
import RestoreIcon from '@mui/icons-material/Restore'
import { configApi } from '../../services/api'

interface Props {
  setMessage: (m: { type: 'success' | 'error'; text: string } | null) => void
}

interface AIOperationsSettings {
  prompt_cache_enabled: boolean
  history_window: number
  tool_response_budget_default: number
  thinking_budget: number
}

const DEFAULTS: AIOperationsSettings = {
  prompt_cache_enabled: true,
  history_window: 20,
  tool_response_budget_default: 8000,
  thinking_budget: 10000,
}

export default function AIOperationsTab({ setMessage }: Props) {
  const [settings, setSettings] = useState<AIOperationsSettings>(DEFAULTS)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)

  const load = async () => {
    setLoading(true)
    try {
      const { data } = await configApi.getAIOperations()
      setSettings({ ...DEFAULTS, ...data })
    } catch (err: any) {
      setMessage({
        type: 'error',
        text: err?.response?.data?.detail || 'Failed to load AI operations config',
      })
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const handleSave = async () => {
    setSaving(true)
    try {
      await configApi.setAIOperations(settings)
      setMessage({ type: 'success', text: 'AI operations settings saved' })
    } catch (err: any) {
      setMessage({
        type: 'error',
        text: err?.response?.data?.detail || 'Failed to save AI operations config',
      })
    } finally {
      setSaving(false)
    }
  }

  const handleReset = () => setSettings(DEFAULTS)

  const numberField = (
    key: keyof AIOperationsSettings,
    label: string,
    helper: string,
    min: number,
    max: number,
  ) => (
    <Tooltip title={helper} placement="top" arrow>
      <TextField
        label={label}
        type="number"
        value={settings[key] as number}
        onChange={(e) =>
          setSettings({
            ...settings,
            [key]: Math.max(min, Math.min(max, Number(e.target.value) || 0)),
          })
        }
        inputProps={{ min, max }}
        fullWidth
        helperText={helper}
      />
    </Tooltip>
  )

  if (loading) {
    return (
      <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
        <CircularProgress size={24} />
      </Box>
    )
  }

  return (
    <Box>
      <Typography variant="h6" sx={{ mb: 1 }}>
        AI Operations (Cost &amp; Performance)
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        Runtime toggles for Anthropic prompt caching, conversation history
        windowing, tool-response truncation, and the daemon's default
        extended-thinking budget. Changes persist in the database and take
        effect across backend / daemon / llm-worker within ~60 seconds
        (the runtime-config cache TTL).
      </Typography>

      <Alert severity="info" sx={{ mb: 3 }}>
        Env vars (<code>ANTHROPIC_PROMPT_CACHE_ENABLED</code>,{' '}
        <code>CLAUDE_HISTORY_WINDOW</code>, <code>TOOL_RESPONSE_BUDGET_DEFAULT</code>,{' '}
        <code>CLAUDE_THINKING_BUDGET</code>) still work as a fallback when this
        record is absent — useful for hardened production deployments that
        pin values at deploy time.
      </Alert>

      <Paper sx={{ p: 3 }}>
        <Stack spacing={3}>
          <FormControlLabel
            control={
              <Switch
                checked={settings.prompt_cache_enabled}
                onChange={(e) =>
                  setSettings({ ...settings, prompt_cache_enabled: e.target.checked })
                }
              />
            }
            label={
              <Box>
                <Typography>Anthropic prompt caching</Typography>
                <Typography variant="caption" color="text.secondary">
                  Tag system + tool blocks with cache_control. ~90% cheaper on
                  cached input tokens. Leave on unless debugging cache-related
                  behavior.
                </Typography>
              </Box>
            }
          />

          <Grid container spacing={2}>
            <Grid item xs={12} md={4}>
              {numberField(
                'history_window',
                'History window (turns)',
                'Cap per-session history. 20 turns = up to 40 messages. Set to 0 to disable.',
                0,
                200,
              )}
            </Grid>
            <Grid item xs={12} md={4}>
              {numberField(
                'tool_response_budget_default',
                'Tool-result budget (tokens)',
                'Default truncation budget for tool results. Per-tool overrides in code for raw-log fetches.',
                500,
                60000,
              )}
            </Grid>
            <Grid item xs={12} md={4}>
              {numberField(
                'thinking_budget',
                'Daemon thinking budget (tokens)',
                'Default extended-thinking budget for the autonomous daemon. Per-agent profiles override when caller has agent context.',
                500,
                32000,
              )}
            </Grid>
          </Grid>

          <Stack direction="row" spacing={2} sx={{ pt: 1 }}>
            <Button
              variant="contained"
              startIcon={<SaveIcon />}
              onClick={handleSave}
              disabled={saving}
            >
              {saving ? 'Saving…' : 'Save'}
            </Button>
            <Button
              variant="outlined"
              startIcon={<RestoreIcon />}
              onClick={handleReset}
              disabled={saving}
            >
              Reset to defaults
            </Button>
          </Stack>
        </Stack>
      </Paper>
    </Box>
  )
}
