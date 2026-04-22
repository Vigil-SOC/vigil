import { useEffect, useState } from 'react'
import {
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  TextField,
  Button,
  Box,
  Typography,
  Stack,
  Grid,
  FormControlLabel,
  Switch,
  Autocomplete,
  Chip,
  Accordion,
  AccordionSummary,
  AccordionDetails,
  Alert,
  CircularProgress,
} from '@mui/material'
import {
  ExpandMore as ExpandMoreIcon,
  AutoAwesome as AutoAwesomeIcon,
} from '@mui/icons-material'
import {
  agentsApi,
  type CustomAgent,
  type CustomAgentPayload,
  type GeneratedAgentDraft,
} from '../../services/api'

interface Props {
  open: boolean
  agentId: string | null
  onClose: (saved: boolean) => void
  onMessage: (msg: { type: 'success' | 'error'; text: string }) => void
}

const EMPTY_FORM: CustomAgentPayload = {
  name: '',
  role: '',
  description: '',
  icon: 'C',
  color: '#888888',
  specialization: '',
  extra_principles: '',
  methodology: '',
  system_prompt_override: '',
  recommended_tools: [],
  max_tokens: 4096,
  enable_thinking: false,
}

type AIHistoryEntry = { kind: 'describe' | 'refine'; text: string }

export default function AgentBuilderDialog({ open, agentId, onClose, onMessage }: Props) {
  const [form, setForm] = useState<CustomAgentPayload>(EMPTY_FORM)
  const [availableTools, setAvailableTools] = useState<string[]>([])
  const [effectivePrompt, setEffectivePrompt] = useState<string>('')
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [useOverride, setUseOverride] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // AI Assist state (issue #80 Phase 2) — iterative NL → draft refinement
  const [aiDescription, setAiDescription] = useState('')
  const [aiFeedback, setAiFeedback] = useState('')
  const [aiDraft, setAiDraft] = useState<GeneratedAgentDraft | null>(null)
  const [aiHistory, setAiHistory] = useState<AIHistoryEntry[]>([])
  const [aiBusy, setAiBusy] = useState(false)
  const [aiError, setAiError] = useState<string | null>(null)

  const isEdit = Boolean(agentId)

  useEffect(() => {
    if (!open) return
    setError(null)
    // Load tool list once per open
    agentsApi
      .getAvailableTools()
      .then((res) => setAvailableTools(res.data.tools || []))
      .catch(() => {
        /* non-fatal; multi-select still accepts free text */
      })
    if (agentId) {
      setLoading(true)
      agentsApi
        .getCustom(agentId)
        .then((res) => {
          const a: CustomAgent = res.data
          setForm({
            name: a.name,
            role: a.role,
            description: a.description || '',
            icon: a.icon || 'C',
            color: a.color || '#888888',
            specialization: a.specialization || '',
            extra_principles: a.extra_principles || '',
            methodology: a.methodology || '',
            system_prompt_override: a.system_prompt_override || '',
            recommended_tools: a.recommended_tools || [],
            max_tokens: a.max_tokens ?? 4096,
            enable_thinking: a.enable_thinking ?? false,
          })
          setUseOverride(Boolean(a.system_prompt_override))
          setEffectivePrompt(a.effective_prompt || '')
        })
        .catch((err) => {
          setError(err?.response?.data?.detail || err?.message || 'Failed to load agent')
        })
        .finally(() => setLoading(false))
    } else {
      setForm(EMPTY_FORM)
      setUseOverride(false)
      setEffectivePrompt('')
    }
    // Reset AI Assist state whenever the dialog opens or the target changes.
    setAiDescription('')
    setAiFeedback('')
    setAiDraft(null)
    setAiHistory([])
    setAiBusy(false)
    setAiError(null)
  }, [open, agentId])

  const handleChange = <K extends keyof CustomAgentPayload>(key: K, value: CustomAgentPayload[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }))
  }

  // Merge an AI-generated draft into the form. Preserves a manually-typed name
  // (and always preserves it in edit mode since the ID is locked there);
  // everything else is replaced because that's what the AI is for.
  const mergeDraftIntoForm = (d: GeneratedAgentDraft) => {
    setForm((prev) => ({
      ...prev,
      name: isEdit || prev.name?.trim() ? prev.name : d.name,
      description: d.description || prev.description,
      specialization: d.specialization || prev.specialization,
      icon: d.icon || prev.icon,
      color: d.color || prev.color,
      role: d.role || prev.role,
      extra_principles: d.extra_principles || prev.extra_principles,
      methodology: d.methodology || prev.methodology,
      recommended_tools: d.recommended_tools?.length
        ? d.recommended_tools
        : prev.recommended_tools,
      max_tokens: d.max_tokens || prev.max_tokens,
      enable_thinking: d.enable_thinking ?? prev.enable_thinking,
    }))
  }

  const handleAIGenerate = async () => {
    if (!aiDescription.trim()) {
      setAiError('Please describe your agent first.')
      return
    }
    setAiBusy(true)
    setAiError(null)
    try {
      const res = await agentsApi.generateCustom({ description: aiDescription })
      const draft = res.data.draft
      setAiDraft(draft)
      setAiHistory((h) => [...h, { kind: 'describe', text: aiDescription }])
      mergeDraftIntoForm(draft)
    } catch (err: any) {
      setAiError(err?.response?.data?.detail || err?.message || 'Generation failed')
    } finally {
      setAiBusy(false)
    }
  }

  const handleAIRefine = async () => {
    if (!aiFeedback.trim()) {
      setAiError('Describe what to change in the feedback box.')
      return
    }
    setAiBusy(true)
    setAiError(null)
    try {
      const res = await agentsApi.generateCustom({
        description: aiDescription || form.role || 'custom SOC agent',
        current_draft: aiDraft,
        feedback: aiFeedback,
      })
      const draft = res.data.draft
      setAiDraft(draft)
      setAiHistory((h) => [...h, { kind: 'refine', text: aiFeedback }])
      setAiFeedback('')
      mergeDraftIntoForm(draft)
    } catch (err: any) {
      setAiError(err?.response?.data?.detail || err?.message || 'Refinement failed')
    } finally {
      setAiBusy(false)
    }
  }

  const validate = (): string | null => {
    if (!form.name?.trim()) return 'Name is required'
    if (!form.role?.trim()) return 'Role is required'
    if (useOverride && !form.system_prompt_override?.trim()) {
      return 'Advanced override is enabled but the override text is empty'
    }
    return null
  }

  const handleSave = async () => {
    const v = validate()
    if (v) {
      setError(v)
      return
    }
    setError(null)
    setSaving(true)
    try {
      const payload: CustomAgentPayload = {
        ...form,
        // When override toggle is off, send explicit null so server clears any prior override
        system_prompt_override: useOverride ? form.system_prompt_override || '' : null,
        // Coerce numeric field
        max_tokens: Number(form.max_tokens) || 4096,
      }
      if (isEdit && agentId) {
        await agentsApi.updateCustom(agentId, payload)
        onMessage({ type: 'success', text: `Updated ${form.name}` })
      } else {
        await agentsApi.createCustom(payload)
        onMessage({ type: 'success', text: `Created ${form.name}` })
      }
      setTimeout(() => onMessage({ type: 'success', text: '' }), 3000)
      onClose(true)
    } catch (err: any) {
      const detail = err?.response?.data?.detail || err?.message || 'Save failed'
      setError(detail)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog open={open} onClose={() => onClose(false)} maxWidth="md" fullWidth>
      <DialogTitle>{isEdit ? `Edit Agent: ${form.name || agentId}` : 'New Custom Agent'}</DialogTitle>
      <DialogContent dividers>
        {loading ? (
          <Box sx={{ display: 'flex', justifyContent: 'center', p: 4 }}>
            <CircularProgress size={28} />
          </Box>
        ) : (
          <Stack spacing={2}>
            {error && <Alert severity="error">{error}</Alert>}

            {/* AI Assist — NL → draft with iterative refinement (issue #80 Phase 2) */}
            <Accordion variant="outlined" disableGutters defaultExpanded={!isEdit}>
              <AccordionSummary expandIcon={<ExpandMoreIcon />}>
                <Stack direction="row" spacing={1} alignItems="center">
                  <AutoAwesomeIcon fontSize="small" color="primary" />
                  <Typography variant="body2" sx={{ fontWeight: 600 }}>
                    AI Assist
                  </Typography>
                  <Typography variant="caption" color="text.secondary">
                    Describe your agent and iterate with feedback
                  </Typography>
                </Stack>
              </AccordionSummary>
              <AccordionDetails>
                <Stack spacing={1.5}>
                  {aiError && (
                    <Alert severity="error" onClose={() => setAiError(null)}>
                      {aiError}
                    </Alert>
                  )}
                  <TextField
                    label="Describe your agent"
                    multiline
                    rows={2}
                    fullWidth
                    value={aiDescription}
                    onChange={(e) => setAiDescription(e.target.value)}
                    placeholder="e.g. detects impossible-travel logins by correlating auth events across geographies"
                    disabled={aiBusy}
                    helperText="The draft fills the fields below. Your typed values are preserved where possible."
                  />
                  <Stack direction="row" spacing={1} alignItems="center">
                    <Button
                      variant="contained"
                      size="small"
                      startIcon={
                        aiBusy && !aiDraft ? (
                          <CircularProgress size={14} />
                        ) : (
                          <AutoAwesomeIcon />
                        )
                      }
                      onClick={handleAIGenerate}
                      disabled={aiBusy || !aiDescription.trim()}
                    >
                      {aiDraft ? 'Regenerate' : 'Generate'}
                    </Button>
                    {aiDraft && (
                      <Typography variant="caption" color="text.secondary">
                        Draft applied. Refine below or edit fields directly.
                      </Typography>
                    )}
                  </Stack>

                  {aiDraft && (
                    <>
                      <TextField
                        label="Refinement feedback"
                        multiline
                        rows={2}
                        fullWidth
                        value={aiFeedback}
                        onChange={(e) => setAiFeedback(e.target.value)}
                        placeholder="e.g. add VirusTotal lookups; tighten methodology to 3 steps; enable thinking"
                        disabled={aiBusy}
                      />
                      <Stack direction="row" spacing={1} alignItems="center">
                        <Button
                          variant="outlined"
                          size="small"
                          startIcon={
                            aiBusy && aiDraft ? (
                              <CircularProgress size={14} />
                            ) : (
                              <AutoAwesomeIcon />
                            )
                          }
                          onClick={handleAIRefine}
                          disabled={aiBusy || !aiFeedback.trim()}
                        >
                          Refine
                        </Button>
                        <Typography variant="caption" color="text.secondary">
                          Keeps what's good; changes what you ask for.
                        </Typography>
                      </Stack>
                    </>
                  )}

                  {aiHistory.length > 0 && (
                    <Box
                      sx={{
                        mt: 0.5,
                        p: 1,
                        borderRadius: 1,
                        bgcolor: (t) =>
                          t.palette.mode === 'dark'
                            ? 'rgba(255,255,255,0.03)'
                            : 'rgba(0,0,0,0.03)',
                      }}
                    >
                      <Typography
                        variant="caption"
                        color="text.secondary"
                        sx={{ display: 'block', mb: 0.5 }}
                      >
                        Conversation
                      </Typography>
                      <Stack spacing={0.5}>
                        {aiHistory.map((h, i) => (
                          <Stack key={i} direction="row" spacing={1}>
                            <Chip
                              size="small"
                              label={h.kind === 'describe' ? 'describe' : 'refine'}
                              color={h.kind === 'describe' ? 'primary' : 'default'}
                              variant="outlined"
                            />
                            <Typography
                              variant="caption"
                              sx={{ whiteSpace: 'pre-wrap' }}
                            >
                              {h.text}
                            </Typography>
                          </Stack>
                        ))}
                      </Stack>
                    </Box>
                  )}
                </Stack>
              </AccordionDetails>
            </Accordion>

            <Typography variant="subtitle2" sx={{ mt: 1 }}>
              Identity
            </Typography>
            <Grid container spacing={2}>
              <Grid item xs={12} sm={6}>
                <TextField
                  label="Name"
                  fullWidth
                  required
                  value={form.name}
                  onChange={(e) => handleChange('name', e.target.value)}
                  disabled={isEdit}
                  helperText={isEdit ? 'Agent ID is derived from the name and cannot be changed' : 'The agent ID will be "custom-<slug>"'}
                />
              </Grid>
              <Grid item xs={12} sm={6}>
                <TextField
                  label="Specialization"
                  fullWidth
                  value={form.specialization || ''}
                  onChange={(e) => handleChange('specialization', e.target.value)}
                  placeholder="e.g. Phishing Analysis"
                />
              </Grid>
              <Grid item xs={12}>
                <TextField
                  label="Description"
                  fullWidth
                  multiline
                  rows={2}
                  value={form.description || ''}
                  onChange={(e) => handleChange('description', e.target.value)}
                />
              </Grid>
              <Grid item xs={6} sm={3}>
                <TextField
                  label="Icon (1 char)"
                  fullWidth
                  inputProps={{ maxLength: 2 }}
                  value={form.icon || ''}
                  onChange={(e) => handleChange('icon', e.target.value)}
                />
              </Grid>
              <Grid item xs={6} sm={3}>
                <TextField
                  label="Color"
                  type="color"
                  fullWidth
                  value={form.color || '#888888'}
                  onChange={(e) => handleChange('color', e.target.value)}
                />
              </Grid>
            </Grid>

            <Typography variant="subtitle2" sx={{ mt: 1 }}>
              Prompt fragments
              <Typography variant="caption" component="span" color="text.secondary" sx={{ ml: 1 }}>
                Rendered into the Vigil base prompt (preserves mempalace + entity-recognition directives)
              </Typography>
            </Typography>
            <TextField
              label="Role"
              required
              fullWidth
              value={form.role}
              onChange={(e) => handleChange('role', e.target.value)}
              placeholder="e.g. phishing specialist"
              helperText='Renders as: "You are a SOC {role} in the Vigil SOC platform."'
              disabled={useOverride}
            />
            <TextField
              label="Extra principles"
              multiline
              rows={3}
              fullWidth
              value={form.extra_principles || ''}
              onChange={(e) => handleChange('extra_principles', e.target.value)}
              placeholder="- Verify sender reputation before classifying..."
              disabled={useOverride}
            />
            <TextField
              label="Methodology"
              multiline
              rows={4}
              fullWidth
              value={form.methodology || ''}
              onChange={(e) => handleChange('methodology', e.target.value)}
              placeholder="1. Enrich sender IP with threat intel...\n2. Check for lookalike domains...\n3. ..."
              disabled={useOverride}
            />

            <FormControlLabel
              control={
                <Switch
                  checked={useOverride}
                  onChange={(e) => setUseOverride(e.target.checked)}
                />
              }
              label="Advanced: bypass base template (write the full system prompt yourself)"
            />
            {useOverride && (
              <TextField
                label="System prompt (verbatim — replaces the base template)"
                multiline
                rows={10}
                fullWidth
                value={form.system_prompt_override || ''}
                onChange={(e) => handleChange('system_prompt_override', e.target.value)}
                InputProps={{ sx: { fontFamily: 'monospace', fontSize: '0.85rem' } }}
              />
            )}

            <Typography variant="subtitle2" sx={{ mt: 1 }}>
              Tools & behavior
            </Typography>
            <Autocomplete
              multiple
              freeSolo
              options={availableTools}
              groupBy={(opt) => (opt.includes('_') ? opt.split('_', 1)[0] : 'other')}
              value={form.recommended_tools || []}
              onChange={(_, newValue) => handleChange('recommended_tools', newValue as string[])}
              renderTags={(value, getTagProps) =>
                value.map((tool, index) => (
                  <Chip
                    size="small"
                    variant="outlined"
                    label={tool}
                    {...getTagProps({ index })}
                    key={tool}
                  />
                ))
              }
              renderInput={(params) => (
                <TextField
                  {...params}
                  label="Recommended MCP tools"
                  placeholder="Start typing to filter..."
                  helperText={`${availableTools.length} tools available — free text accepted if a tool isn't in the registry yet`}
                />
              )}
            />
            <Grid container spacing={2}>
              <Grid item xs={6} sm={4}>
                <TextField
                  label="Max tokens"
                  type="number"
                  fullWidth
                  value={form.max_tokens ?? 4096}
                  onChange={(e) => handleChange('max_tokens', Number(e.target.value))}
                  inputProps={{ min: 256, max: 32768, step: 256 }}
                />
              </Grid>
              <Grid item xs={6} sm={4}>
                <FormControlLabel
                  control={
                    <Switch
                      checked={form.enable_thinking ?? false}
                      onChange={(e) => handleChange('enable_thinking', e.target.checked)}
                    />
                  }
                  label="Enable thinking"
                />
              </Grid>
            </Grid>

            {isEdit && effectivePrompt && (
              <Accordion variant="outlined" disableGutters>
                <AccordionSummary expandIcon={<ExpandMoreIcon />}>
                  <Typography variant="body2">Preview effective prompt</Typography>
                </AccordionSummary>
                <AccordionDetails>
                  <Box
                    sx={{
                      fontFamily: 'monospace',
                      fontSize: '0.8rem',
                      whiteSpace: 'pre-wrap',
                      bgcolor: (t) => (t.palette.mode === 'dark' ? 'rgba(255,255,255,0.03)' : 'rgba(0,0,0,0.03)'),
                      p: 2,
                      borderRadius: 1,
                      maxHeight: '40vh',
                      overflow: 'auto',
                    }}
                  >
                    {effectivePrompt}
                  </Box>
                  <Typography variant="caption" color="text.secondary">
                    This is the exact system prompt Claude receives. Re-save to refresh.
                  </Typography>
                </AccordionDetails>
              </Accordion>
            )}
          </Stack>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={() => onClose(false)} disabled={saving}>
          Cancel
        </Button>
        <Button variant="contained" onClick={handleSave} disabled={saving || loading}>
          {saving ? 'Saving…' : isEdit ? 'Save Changes' : 'Create Agent'}
        </Button>
      </DialogActions>
    </Dialog>
  )
}
