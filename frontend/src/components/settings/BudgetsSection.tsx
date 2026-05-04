import { useEffect, useState } from 'react'
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  FormControl,
  InputAdornment,
  InputLabel,
  LinearProgress,
  MenuItem,
  Paper,
  Select,
  Stack,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import RefreshIcon from '@mui/icons-material/Refresh'
import SaveIcon from '@mui/icons-material/Save'
import { budgetsApi, BudgetSettings, BudgetQuotaResponse } from '../../services/api'

interface Props {
  setMessage: (m: { type: 'success' | 'error'; text: string } | null) => void
}

// Mask all but the last 4 chars of the VK so the UI doesn't leak the full
// secret in screenshots / shared screens. The ID prefix `sk-bf-...` is
// already a public marker so showing it is fine.
function maskVk(vk: string): string {
  if (!vk) return ''
  if (vk.length <= 8) return vk
  return `${vk.slice(0, 6)}…${vk.slice(-4)}`
}

export default function BudgetsSection({ setMessage }: Props) {
  const [settings, setSettings] = useState<BudgetSettings>({
    default_vk: '',
    budget_limit_usd: 0,
    enforcement_mode: 'warning',
  })
  const [draft, setDraft] = useState<BudgetSettings>({
    default_vk: '',
    budget_limit_usd: 0,
    enforcement_mode: 'warning',
  })
  const [quota, setQuota] = useState<BudgetQuotaResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [showVk, setShowVk] = useState(false)

  const load = async () => {
    try {
      const [settingsRes, quotaRes] = await Promise.all([
        budgetsApi.get(),
        budgetsApi.getQuota().catch(() => ({ data: null })),
      ])
      setSettings(settingsRes.data)
      setDraft(settingsRes.data)
      setQuota(quotaRes.data)
    } catch {
      setMessage({ type: 'error', text: 'Failed to load budget settings' })
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  const dirty =
    draft.default_vk !== settings.default_vk ||
    draft.budget_limit_usd !== settings.budget_limit_usd ||
    draft.enforcement_mode !== settings.enforcement_mode

  const handleSave = async () => {
    setSaving(true)
    try {
      const res = await budgetsApi.set({
        default_vk: draft.default_vk.trim(),
        budget_limit_usd: Number(draft.budget_limit_usd) || 0,
        enforcement_mode: draft.enforcement_mode,
      })
      setSettings(res.data)
      setDraft(res.data)
      setMessage({ type: 'success', text: 'Budget settings saved' })
      // Refresh quota since the VK might have changed.
      const q = await budgetsApi.getQuota().catch(() => ({ data: null }))
      setQuota(q.data)
    } catch (e: any) {
      setMessage({ type: 'error', text: e?.response?.data?.detail || 'Save failed' })
    } finally {
      setSaving(false)
    }
  }

  if (loading) {
    return (
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, py: 2 }}>
        <CircularProgress size={16} />
        <Typography variant="body2">Loading budget settings…</Typography>
      </Box>
    )
  }

  // Pull the first budget tier out of the quota response (Bifrost can return
  // multiple — for MVP we only render the first; per-tier breakdown is a
  // post-MVP enhancement when we have multi-tenant scoping).
  const firstBudget = quota?.quota?.budgets?.[0]
  const spendPct =
    firstBudget && firstBudget.max_limit > 0
      ? Math.min(
          100,
          Math.round((firstBudget.current_usage / firstBudget.max_limit) * 100),
        )
      : 0

  return (
    <Paper variant="outlined" sx={{ p: 2.5, mt: 4 }}>
      <Box sx={{ display: 'flex', alignItems: 'center', mb: 1.5 }}>
        <Typography variant="subtitle1" sx={{ fontWeight: 600, flexGrow: 1 }}>
          Budgets (Bifrost virtual key)
        </Typography>
        <Tooltip title="Refresh quota from Bifrost">
          <Button size="small" startIcon={<RefreshIcon />} onClick={load}>
            Refresh
          </Button>
        </Tooltip>
      </Box>

      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 2 }}>
        Bifrost enforces a USD budget per virtual key, upstream of every LLM
        call. Set the global VK and ceiling here. <code>DEV_MODE=true</code> or{' '}
        <code>LLM_BUDGET_UNLIMITED=true</code> in the environment bypasses
        enforcement; useful for local development without reconfiguring
        Bifrost.
      </Typography>

      {/* Live quota — only renders when Bifrost is reachable AND a VK is set */}
      {quota?.configured && quota.available && firstBudget && (
        <Box sx={{ mb: 3 }}>
          <Box sx={{ display: 'flex', justifyContent: 'space-between', mb: 0.5 }}>
            <Typography variant="body2">
              <strong>${firstBudget.current_usage.toFixed(2)}</strong> spent of{' '}
              <strong>${firstBudget.max_limit.toFixed(2)}</strong>
            </Typography>
            <Typography variant="caption" color="text.secondary">
              {firstBudget.reset_duration} cycle · resets {firstBudget.last_reset || '—'}
            </Typography>
          </Box>
          <LinearProgress
            variant="determinate"
            value={spendPct}
            color={spendPct >= 90 ? 'error' : spendPct >= 75 ? 'warning' : 'primary'}
          />
        </Box>
      )}

      {quota?.configured && !quota.available && (
        <Alert severity="warning" sx={{ mb: 2 }}>
          {quota.message ||
            "Bifrost is unreachable or the configured VK doesn't exist."}
        </Alert>
      )}

      {!quota?.configured && (
        <Alert severity="info" sx={{ mb: 2 }}>
          {quota?.message ||
            'No virtual key configured. Provision one in the Bifrost UI, then paste its ID below.'}
        </Alert>
      )}

      <Stack spacing={2} sx={{ maxWidth: 640 }}>
        <TextField
          label="Default virtual key (sk-bf-…)"
          value={showVk ? draft.default_vk : maskVk(draft.default_vk)}
          onChange={(e) => setDraft({ ...draft, default_vk: e.target.value })}
          onFocus={() => setShowVk(true)}
          onBlur={() => setShowVk(false)}
          size="small"
          fullWidth
          helperText="The VK Bifrost reads from `x-bf-vk` on every upstream LLM call. Empty = bootstrap mode (no enforcement)."
        />
        <TextField
          label="Monthly budget ceiling"
          type="number"
          size="small"
          value={draft.budget_limit_usd}
          onChange={(e) =>
            setDraft({ ...draft, budget_limit_usd: Number(e.target.value) })
          }
          InputProps={{
            startAdornment: <InputAdornment position="start">$</InputAdornment>,
          }}
          helperText="Reference value used by the dashboard. Bifrost enforces its own ceiling on the VK; keep them in sync to avoid surprises."
          sx={{ maxWidth: 280 }}
        />
        <FormControl size="small" sx={{ maxWidth: 280 }}>
          <InputLabel>Enforcement mode</InputLabel>
          <Select
            label="Enforcement mode"
            value={draft.enforcement_mode}
            onChange={(e) =>
              setDraft({
                ...draft,
                enforcement_mode: e.target.value as 'warning' | 'hard_stop',
              })
            }
          >
            <MenuItem value="warning">Warning only — log but allow</MenuItem>
            <MenuItem value="hard_stop">Hard stop — block on exceed</MenuItem>
          </Select>
        </FormControl>

        <Box>
          <Button
            variant="contained"
            startIcon={<SaveIcon />}
            disabled={!dirty || saving}
            onClick={handleSave}
          >
            {saving ? 'Saving…' : 'Save'}
          </Button>
        </Box>
      </Stack>
    </Paper>
  )
}
