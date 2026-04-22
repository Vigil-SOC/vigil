/**
 * ModelAssignmentTab — per-component AI model picker (GH #89).
 *
 * Renders one row per component (chat_default, triage, investigation,
 * orchestrator_plan, orchestrator_review, summarization, reporting). Each
 * row has a provider + model selector populated from /api/ai/models.
 * "Inherit from Chat Default" clears the assignment so the component
 * falls back through the chain in services/model_registry.py.
 */

import { useEffect, useMemo, useState } from 'react'
import {
  Box,
  Typography,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Paper,
  Select,
  MenuItem,
  Chip,
  CircularProgress,
  Checkbox,
  FormControlLabel,
  Stack,
} from '@mui/material'
import PsychologyIcon from '@mui/icons-material/Psychology'
import BuildIcon from '@mui/icons-material/Build'
import ImageIcon from '@mui/icons-material/Image'
import {
  aiConfigApi,
  AIModelInfo,
  ComponentAssignment,
} from '../../services/api'

interface Props {
  setMessage: (m: { type: 'success' | 'error'; text: string } | null) => void
}

const COMPONENT_LABELS: Record<string, { label: string; description: string }> = {
  chat_default: {
    label: 'Chat (Default)',
    description: 'Fallback for interactive chat and every component below when unset.',
  },
  triage: {
    label: 'Triage Agent',
    description: 'Automated alert triage — cheaper/faster models work well here.',
  },
  investigation: {
    label: 'Investigation Agents',
    description: 'Investigator, Threat Hunter, Correlator, etc. — the heavy lifters.',
  },
  orchestrator_plan: {
    label: 'Orchestrator — Planning',
    description: 'Generates the investigation plan from the initial finding.',
  },
  orchestrator_review: {
    label: 'Orchestrator — Review',
    description: 'Reviews and approves sub-agent output at the end of an investigation.',
  },
  summarization: {
    label: 'Context Summarization',
    description: 'Compresses long conversations — a cheap model is usually fine.',
  },
  reporting: {
    label: 'Report Generation',
    description: 'Reporter agent output — clarity and structure matter more than depth.',
  },
}

const CHAT_DEFAULT_KEY = 'chat_default'

type RowState = {
  inherit: boolean
  providerId: string
  modelId: string
}

export default function ModelAssignmentTab({ setMessage }: Props) {
  const [components, setComponents] = useState<string[]>([])
  const [initialAssignments, setInitialAssignments] = useState<Record<string, ComponentAssignment>>({})
  const [rows, setRows] = useState<Record<string, RowState>>({})
  const [models, setModels] = useState<AIModelInfo[]>([])
  const [loading, setLoading] = useState(false)

  const modelsByProvider = useMemo(() => {
    const grouped: Record<string, AIModelInfo[]> = {}
    for (const m of models) {
      if (!grouped[m.provider_id]) grouped[m.provider_id] = []
      grouped[m.provider_id].push(m)
    }
    return grouped
  }, [models])

  const providerIds = useMemo(() => Object.keys(modelsByProvider).sort(), [modelsByProvider])

  const load = async () => {
    setLoading(true)
    try {
      const [cfgResp, modelsResp] = await Promise.all([
        aiConfigApi.getConfig(),
        aiConfigApi.listModels(),
      ])
      setComponents(cfgResp.data.components)
      setInitialAssignments(cfgResp.data.assignments)
      setModels(modelsResp.data.models)

      const nextRows: Record<string, RowState> = {}
      for (const c of cfgResp.data.components) {
        const a = cfgResp.data.assignments[c]
        if (a) {
          nextRows[c] = {
            inherit: false,
            providerId: a.provider_id,
            modelId: a.model_id,
          }
        } else {
          nextRows[c] = {
            inherit: c !== CHAT_DEFAULT_KEY,
            providerId: '',
            modelId: '',
          }
        }
      }
      setRows(nextRows)
    } catch (e: any) {
      setMessage({
        type: 'error',
        text: e?.response?.data?.detail || 'Failed to load AI config',
      })
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const persistRow = async (component: string, next: RowState) => {
    const initial = initialAssignments[component]
    try {
      if (next.inherit) {
        if (initial !== undefined) {
          await aiConfigApi.clearComponent(component)
          setInitialAssignments((prev) => {
            const copy = { ...prev }
            delete copy[component]
            return copy
          })
          setMessage({ type: 'success', text: `${component} set to inherit` })
        }
        return
      }
      if (!next.providerId || !next.modelId) return
      if (
        initial &&
        initial.provider_id === next.providerId &&
        initial.model_id === next.modelId
      ) {
        return
      }
      await aiConfigApi.setComponent(component, {
        provider_id: next.providerId,
        model_id: next.modelId,
      })
      setInitialAssignments((prev) => ({
        ...prev,
        [component]: {
          component,
          provider_id: next.providerId,
          model_id: next.modelId,
          settings: {},
          updated_by: null,
          updated_at: null,
        },
      }))
      setMessage({ type: 'success', text: `${component} saved` })
    } catch (e: any) {
      setMessage({
        type: 'error',
        text: e?.response?.data?.detail || `Failed to save ${component}`,
      })
    }
  }

  const updateRow = (component: string, patch: Partial<RowState>) => {
    setRows((prev) => {
      const next = { ...prev[component], ...patch }
      persistRow(component, next)
      return { ...prev, [component]: next }
    })
  }

  const renderModelMenuItem = (m: AIModelInfo) => {
    const ctx = m.context_window ? `${Math.round(m.context_window / 1000)}K` : '?'
    const cost =
      m.input_cost_per_1k > 0 || m.output_cost_per_1k > 0
        ? `$${m.input_cost_per_1k.toFixed(4)} in / $${m.output_cost_per_1k.toFixed(4)} out (per 1K)`
        : 'self-hosted'
    return (
      <MenuItem value={m.model_id} key={`${m.provider_id}-${m.model_id}`}>
        <Stack>
          <Typography variant="body2">{m.display_name || m.model_id}</Typography>
          <Typography variant="caption" color="text.secondary">
            {ctx} ctx · {cost}
          </Typography>
        </Stack>
      </MenuItem>
    )
  }

  if (loading) {
    return (
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, p: 2 }}>
        <CircularProgress size={16} />
        <Typography variant="body2">Loading AI config…</Typography>
      </Box>
    )
  }

  return (
    <Box>
      <Typography variant="subtitle1" sx={{ fontWeight: 600, mb: 0.5 }}>
        Model Assignment
      </Typography>
      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 2 }}>
        Pick a provider + model for each system component. Unassigned rows fall back to
        the <code>chat_default</code> assignment. Model list is live-queried from each
        provider.
      </Typography>

      {providerIds.length === 0 ? (
        <Typography variant="body2" color="warning.main" sx={{ mb: 2 }}>
          No models discovered — add at least one active provider below before
          assigning models.
        </Typography>
      ) : null}

      <TableContainer component={Paper} variant="outlined">
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell sx={{ fontWeight: 600 }}>Component</TableCell>
              <TableCell sx={{ fontWeight: 600 }}>Provider</TableCell>
              <TableCell sx={{ fontWeight: 600 }}>Model</TableCell>
              <TableCell sx={{ fontWeight: 600 }}>Capabilities</TableCell>
              <TableCell sx={{ fontWeight: 600 }}>Inherit</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {components.map((c) => {
              const meta = COMPONENT_LABELS[c] || { label: c, description: '' }
              const row = rows[c] || { inherit: true, providerId: '', modelId: '' }
              const isChatDefault = c === CHAT_DEFAULT_KEY
              const providerModels = row.providerId
                ? modelsByProvider[row.providerId] || []
                : []
              const selectedModel =
                providerModels.find((m) => m.model_id === row.modelId) || null

              return (
                <TableRow key={c}>
                  <TableCell sx={{ verticalAlign: 'top' }}>
                    <Typography variant="body2" sx={{ fontWeight: 600 }}>
                      {meta.label}
                    </Typography>
                    <Typography variant="caption" color="text.secondary">
                      {meta.description}
                    </Typography>
                  </TableCell>
                  <TableCell>
                    <Select
                      size="small"
                      fullWidth
                      disabled={row.inherit}
                      value={row.providerId}
                      displayEmpty
                      onChange={(e) =>
                        updateRow(c, {
                          providerId: e.target.value as string,
                          modelId: '',
                        })
                      }
                    >
                      <MenuItem value="" disabled>
                        <em>Select provider</em>
                      </MenuItem>
                      {providerIds.map((pid) => (
                        <MenuItem value={pid} key={pid}>
                          {pid}
                        </MenuItem>
                      ))}
                    </Select>
                  </TableCell>
                  <TableCell>
                    <Select
                      size="small"
                      fullWidth
                      disabled={row.inherit || !row.providerId}
                      value={row.modelId}
                      displayEmpty
                      onChange={(e) => updateRow(c, { modelId: e.target.value as string })}
                    >
                      <MenuItem value="" disabled>
                        <em>Select model</em>
                      </MenuItem>
                      {providerModels.map(renderModelMenuItem)}
                    </Select>
                  </TableCell>
                  <TableCell>
                    <Stack direction="row" spacing={0.5}>
                      {selectedModel?.supports_tools ? (
                        <Chip size="small" label="Tools" icon={<BuildIcon sx={{ fontSize: 14 }} />} />
                      ) : null}
                      {selectedModel?.supports_thinking ? (
                        <Chip
                          size="small"
                          label="Thinking"
                          icon={<PsychologyIcon sx={{ fontSize: 14 }} />}
                        />
                      ) : null}
                      {selectedModel?.supports_vision ? (
                        <Chip size="small" label="Vision" icon={<ImageIcon sx={{ fontSize: 14 }} />} />
                      ) : null}
                    </Stack>
                  </TableCell>
                  <TableCell sx={{ verticalAlign: 'top' }}>
                    <FormControlLabel
                      disabled={isChatDefault}
                      control={
                        <Checkbox
                          size="small"
                          checked={row.inherit}
                          onChange={(e) =>
                            updateRow(c, {
                              inherit: e.target.checked,
                              ...(e.target.checked ? { providerId: '', modelId: '' } : {}),
                            })
                          }
                        />
                      }
                      label={<Typography variant="caption">From Chat Default</Typography>}
                    />
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </TableContainer>
    </Box>
  )
}
