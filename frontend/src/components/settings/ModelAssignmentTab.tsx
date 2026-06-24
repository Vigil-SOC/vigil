/**
 * ModelAssignmentTab — per-component AI model picker (GH #89, #331).
 *
 * Each row has a single combined dropdown. The first option is
 * "Use Chat Default (model-name)" — selecting it clears the explicit
 * assignment and the component falls back through the chain in
 * services/model_registry.py. Selecting a specific model pins that
 * component to it. No separate checkbox needed.
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
  ListSubheader,
  Chip,
  CircularProgress,
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
const INHERIT_VALUE = ''

// Encode/decode a compound dropdown value so a single <Select> covers
// both provider and model without a separate provider dropdown.
const encode = (providerId: string, modelId: string) =>
  providerId && modelId ? `${providerId}::${modelId}` : INHERIT_VALUE

const decode = (value: string): { providerId: string; modelId: string } => {
  if (!value) return { providerId: '', modelId: '' }
  const sep = value.indexOf('::')
  if (sep === -1) return { providerId: '', modelId: '' }
  return { providerId: value.slice(0, sep), modelId: value.slice(sep + 2) }
}

type RowState = { providerId: string; modelId: string }

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

  // Resolved model shown in "Use Chat Default (...)" label.
  const chatDefaultModel = useMemo(() => {
    const a = initialAssignments[CHAT_DEFAULT_KEY]
    if (!a) return null
    return models.find((m) => m.provider_id === a.provider_id && m.model_id === a.model_id) ?? null
  }, [initialAssignments, models])

  const chatDefaultLabel = chatDefaultModel
    ? `Use Chat Default (${chatDefaultModel.display_name || chatDefaultModel.model_id})`
    : 'Use Chat Default'

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
        nextRows[c] = a
          ? { providerId: a.provider_id, modelId: a.model_id }
          : { providerId: '', modelId: '' }
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
    const isInherit = !next.providerId || !next.modelId
    try {
      if (isInherit) {
        if (initial !== undefined) {
          await aiConfigApi.clearComponent(component)
          setInitialAssignments((prev) => {
            const copy = { ...prev }
            delete copy[component]
            return copy
          })
          setMessage({ type: 'success', text: `${component} reset to Chat Default` })
        }
        return
      }
      if (
        initial &&
        initial.provider_id === next.providerId &&
        initial.model_id === next.modelId
      ) return

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

  const updateRow = (component: string, next: RowState) => {
    setRows((prev) => {
      persistRow(component, next)
      return { ...prev, [component]: next }
    })
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
        Pick a model for each system component. Unassigned rows fall back to the{' '}
        <code>Chat Default</code>, then to the active provider's default model if Chat Default is
        also unset. Model list is live-queried from each provider.
      </Typography>

      {providerIds.length === 0 ? (
        <Typography variant="body2" color="warning.main" sx={{ mb: 2 }}>
          No models discovered — add at least one active provider before assigning models.
        </Typography>
      ) : null}

      <TableContainer component={Paper} variant="outlined">
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell sx={{ fontWeight: 600, width: '30%' }}>Component</TableCell>
              <TableCell sx={{ fontWeight: 600, width: '45%' }}>Model</TableCell>
              <TableCell sx={{ fontWeight: 600 }}>Capabilities</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {components.map((c) => {
              const meta = COMPONENT_LABELS[c] || { label: c, description: '' }
              const row = rows[c] || { providerId: '', modelId: '' }
              const isChatDefault = c === CHAT_DEFAULT_KEY
              const dropdownValue = encode(row.providerId, row.modelId)

              // A pinned model can drop out of /ai/models (its provider was
              // disabled, or an Ollama model was removed locally). Without a
              // matching <MenuItem> the <Select> renders blank and MUI warns
              // about an out-of-range value, so the row looks unassigned even
              // though the DB still pins it. Detect that and render a disabled
              // fallback item below so the stale assignment stays visible.
              const isPinned = Boolean(row.providerId && row.modelId)
              const pinnedUnavailable =
                isPinned &&
                !(modelsByProvider[row.providerId] || []).some(
                  (m) => m.model_id === row.modelId,
                )

              // Resolve selected model for capabilities display.
              // When inheriting, show the chat_default model's capabilities.
              const resolvedModel: AIModelInfo | null = (() => {
                if (row.providerId && row.modelId) {
                  return (
                    (modelsByProvider[row.providerId] || []).find(
                      (m) => m.model_id === row.modelId,
                    ) ?? null
                  )
                }
                return chatDefaultModel
              })()

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
                      value={dropdownValue}
                      displayEmpty
                      onChange={(e) => {
                        const next = decode(e.target.value as string)
                        updateRow(c, next)
                      }}
                    >
                      {/* "Use Chat Default" option — hidden for chat_default itself */}
                      {!isChatDefault && (
                        <MenuItem value={INHERIT_VALUE}>
                          <Typography variant="body2" color="text.secondary">
                            {chatDefaultLabel}
                          </Typography>
                        </MenuItem>
                      )}
                      {/* Hidden sentinel so MUI doesn't warn when chat_default has no assignment */}
                      {isChatDefault && (
                        <MenuItem value={INHERIT_VALUE} sx={{ display: 'none' }} />
                      )}

                      {/* Stale pin: model is no longer offered by its provider.
                          Disabled so it can't be re-picked, but keeps the value
                          matched so the assignment stays visible. */}
                      {pinnedUnavailable && (
                        <MenuItem value={dropdownValue} disabled>
                          <Typography variant="body2" color="warning.main">
                            Pinned: {row.modelId} (unavailable)
                          </Typography>
                        </MenuItem>
                      )}

                      {/* Models grouped by provider */}
                      {providerIds.flatMap((pid) => {
                        const providerModels = modelsByProvider[pid] || []
                        return [
                          <ListSubheader key={`header-${pid}`} sx={{ lineHeight: '28px', fontSize: 11 }}>
                            {pid}
                          </ListSubheader>,
                          ...providerModels.map((m) => {
                            const ctx = m.context_window
                              ? `${Math.round(m.context_window / 1000)}K`
                              : '?'
                            const cost =
                              m.input_cost_per_1k > 0 || m.output_cost_per_1k > 0
                                ? `$${m.input_cost_per_1k.toFixed(4)}/$${m.output_cost_per_1k.toFixed(4)} per 1K`
                                : 'self-hosted'
                            return (
                              <MenuItem
                                value={encode(pid, m.model_id)}
                                key={`${pid}::${m.model_id}`}
                              >
                                <Stack>
                                  <Typography variant="body2">
                                    {m.display_name || m.model_id}
                                  </Typography>
                                  <Typography variant="caption" color="text.secondary">
                                    {ctx} ctx · {cost}
                                  </Typography>
                                </Stack>
                              </MenuItem>
                            )
                          }),
                        ]
                      })}
                    </Select>
                  </TableCell>

                  <TableCell sx={{ verticalAlign: 'middle' }}>
                    <Stack direction="row" spacing={0.5} flexWrap="wrap">
                      {resolvedModel?.supports_tools ? (
                        <Chip size="small" label="Tools" icon={<BuildIcon sx={{ fontSize: 14 }} />} />
                      ) : null}
                      {resolvedModel?.supports_thinking ? (
                        <Chip
                          size="small"
                          label="Thinking"
                          icon={<PsychologyIcon sx={{ fontSize: 14 }} />}
                        />
                      ) : null}
                      {resolvedModel?.supports_vision ? (
                        <Chip size="small" label="Vision" icon={<ImageIcon sx={{ fontSize: 14 }} />} />
                      ) : null}
                    </Stack>
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
