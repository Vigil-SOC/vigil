import { useState } from 'react'
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  FormControl,
  IconButton,
  InputLabel,
  MenuItem,
  Select,
  Step,
  StepLabel,
  Stepper,
  TextField,
  Typography,
} from '@mui/material'
import {
  Close as CloseIcon,
  Info as InfoIcon,
  Save as SaveIcon,
  AutoAwesome as SparkIcon,
} from '@mui/icons-material'

import {
  SKILL_CATEGORIES,
  SkillCategory,
  SkillDraft,
  skillsApi,
} from '../../services/skillsApi'

interface SkillBuilderProps {
  open: boolean
  onClose: () => void
  onSaved: (skillId: string) => void
}

const STEPS = ['Describe Capability', 'Review & Edit', 'Save']

const DEFAULT_DRAFT: SkillDraft = {
  name: '',
  description: '',
  category: 'custom',
  input_schema: {},
  output_schema: {},
  required_tools: [],
  prompt_template: '',
  execution_steps: [],
  is_active: true,
}

export default function SkillBuilder({ open, onClose, onSaved }: SkillBuilderProps) {
  const [activeStep, setActiveStep] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)

  // Step 1 — describe capability
  const [description, setDescription] = useState('')
  const [category, setCategory] = useState<SkillCategory>('detection')

  // Multi-turn clarification state
  const [needsClarification, setNeedsClarification] = useState(false)
  const [claudeQuestion, setClaudeQuestion] = useState('')
  const [userAnswer, setUserAnswer] = useState('')
  const [conversationHistory, setConversationHistory] = useState<
    { role: string; content: string }[]
  >([])

  // Step 2 — editable draft
  const [draft, setDraft] = useState<SkillDraft>(DEFAULT_DRAFT)
  const [requiredToolsText, setRequiredToolsText] = useState('')
  const [inputSchemaText, setInputSchemaText] = useState('{}')
  const [outputSchemaText, setOutputSchemaText] = useState('{}')
  const [executionStepsText, setExecutionStepsText] = useState('[]')

  const applyDraft = (d: SkillDraft) => {
    setDraft(d)
    setRequiredToolsText((d.required_tools || []).join(', '))
    setInputSchemaText(JSON.stringify(d.input_schema || {}, null, 2))
    setOutputSchemaText(JSON.stringify(d.output_schema || {}, null, 2))
    setExecutionStepsText(JSON.stringify(d.execution_steps || [], null, 2))
  }

  const reset = () => {
    setActiveStep(0)
    setLoading(false)
    setError(null)
    setSuccess(null)
    setDescription('')
    setCategory('detection')
    setNeedsClarification(false)
    setClaudeQuestion('')
    setUserAnswer('')
    setConversationHistory([])
    applyDraft(DEFAULT_DRAFT)
  }

  const handleClose = () => {
    reset()
    onClose()
  }

  const callGenerate = async (answer?: string) => {
    setLoading(true)
    setError(null)
    try {
      const result = await skillsApi.generate({
        description,
        category,
        conversation_history: conversationHistory.length ? conversationHistory : null,
        user_response: answer ?? null,
      })

      if (!result.success) {
        throw new Error(result.error || 'Failed to generate skill')
      }

      if (result.needs_clarification) {
        setNeedsClarification(true)
        setClaudeQuestion(result.message || '')
        setConversationHistory(result.conversation_history || [])
        setUserAnswer('')
      } else if (result.skill) {
        applyDraft({ ...DEFAULT_DRAFT, ...result.skill })
        setNeedsClarification(false)
        setSuccess('Skill draft generated — review and edit before saving.')
        setActiveStep(1)
      } else {
        throw new Error('Skill generator returned no skill draft')
      }
    } catch (err: any) {
      setError(err?.response?.data?.detail || err.message || 'Generation failed')
    } finally {
      setLoading(false)
    }
  }

  const handleGenerateClick = () => {
    if (!description.trim()) {
      setError('Please describe the capability you want to build.')
      return
    }
    callGenerate()
  }

  const handleAnswerQuestion = () => {
    if (!userAnswer.trim()) {
      setError('Please provide an answer to continue.')
      return
    }
    callGenerate(userAnswer)
  }

  const commitDraftFromForm = (): SkillDraft | null => {
    let input_schema: any = {}
    let output_schema: any = {}
    let execution_steps: any = []
    try {
      input_schema = inputSchemaText.trim() ? JSON.parse(inputSchemaText) : {}
    } catch (e) {
      setError('Input schema is not valid JSON')
      return null
    }
    try {
      output_schema = outputSchemaText.trim() ? JSON.parse(outputSchemaText) : {}
    } catch (e) {
      setError('Output schema is not valid JSON')
      return null
    }
    try {
      execution_steps = executionStepsText.trim() ? JSON.parse(executionStepsText) : []
    } catch (e) {
      setError('Execution steps is not valid JSON')
      return null
    }
    if (!Array.isArray(execution_steps)) {
      setError('Execution steps must be a JSON array')
      return null
    }
    const required_tools = requiredToolsText
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean)

    const next: SkillDraft = {
      ...draft,
      input_schema,
      output_schema,
      execution_steps,
      required_tools,
    }

    if (!next.name.trim()) {
      setError('Name is required')
      return null
    }
    if (!next.prompt_template.trim()) {
      setError('Prompt template is required')
      return null
    }
    return next
  }

  const handleSave = async () => {
    const finalDraft = commitDraftFromForm()
    if (!finalDraft) return
    setLoading(true)
    setError(null)
    try {
      const saved = await skillsApi.create(finalDraft)
      setSuccess(`Skill '${saved.name}' saved.`)
      setActiveStep(2)
      setTimeout(() => {
        onSaved(saved.skill_id)
        reset()
      }, 800)
    } catch (err: any) {
      setError(err?.response?.data?.detail || err.message || 'Save failed')
    } finally {
      setLoading(false)
    }
  }

  const renderStep0 = () => (
    <Box>
      {!needsClarification ? (
        <>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            Describe the capability you want to build in plain language. Our AI will
            draft a reusable skill — parameters, tools, prompt, and execution steps.
          </Typography>

          <FormControl fullWidth sx={{ mb: 2 }}>
            <InputLabel>Category</InputLabel>
            <Select
              value={category}
              label="Category"
              onChange={(e) => setCategory(e.target.value as SkillCategory)}
            >
              {SKILL_CATEGORIES.map((c) => (
                <MenuItem key={c} value={c}>
                  {c}
                </MenuItem>
              ))}
            </Select>
          </FormControl>

          <TextField
            fullWidth
            multiline
            rows={8}
            label="Capability description"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder={
              'e.g., "Detect lateral movement via RDP in the last 24 hours, ' +
              'enrich the involved hosts with CrowdStrike, and produce an analyst summary."'
            }
          />
        </>
      ) : (
        <>
          <Alert severity="info" icon={<InfoIcon />} sx={{ mb: 2 }}>
            Claude needs a bit more information to build this skill.
          </Alert>

          <Card sx={{ mb: 2, bgcolor: 'background.default' }}>
            <CardContent>
              <Typography variant="body1" sx={{ whiteSpace: 'pre-wrap' }}>
                {claudeQuestion}
              </Typography>
            </CardContent>
          </Card>

          <TextField
            fullWidth
            multiline
            rows={5}
            label="Your answer"
            value={userAnswer}
            onChange={(e) => setUserAnswer(e.target.value)}
            sx={{ mb: 2 }}
          />

          <Button
            variant="contained"
            onClick={handleAnswerQuestion}
            disabled={loading || !userAnswer.trim()}
            startIcon={loading ? <CircularProgress size={18} /> : null}
            fullWidth
          >
            {loading ? 'Processing…' : 'Send Answer'}
          </Button>
        </>
      )}
    </Box>
  )

  const renderStep1 = () => (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
      <Alert severity="info" icon={<InfoIcon />}>
        Review and edit the generated skill before saving.
      </Alert>

      <TextField
        fullWidth
        label="Name"
        value={draft.name}
        onChange={(e) => setDraft({ ...draft, name: e.target.value })}
      />

      <TextField
        fullWidth
        multiline
        rows={2}
        label="Description"
        value={draft.description || ''}
        onChange={(e) => setDraft({ ...draft, description: e.target.value })}
      />

      <FormControl fullWidth>
        <InputLabel>Category</InputLabel>
        <Select
          value={draft.category}
          label="Category"
          onChange={(e) =>
            setDraft({ ...draft, category: e.target.value as SkillCategory })
          }
        >
          {SKILL_CATEGORIES.map((c) => (
            <MenuItem key={c} value={c}>
              {c}
            </MenuItem>
          ))}
        </Select>
      </FormControl>

      <TextField
        fullWidth
        label="Required tools (comma-separated MCP tool names)"
        value={requiredToolsText}
        onChange={(e) => setRequiredToolsText(e.target.value)}
        placeholder="splunk.search, crowdstrike.host_lookup"
      />
      {draft.required_tools.length > 0 && (
        <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5 }}>
          {draft.required_tools.map((t) => (
            <Chip key={t} label={t} size="small" />
          ))}
        </Box>
      )}

      <TextField
        fullWidth
        multiline
        rows={6}
        label="Prompt template ({{param}} placeholders allowed)"
        value={draft.prompt_template}
        onChange={(e) => setDraft({ ...draft, prompt_template: e.target.value })}
      />

      <TextField
        fullWidth
        multiline
        rows={5}
        label="Input schema (JSON)"
        value={inputSchemaText}
        onChange={(e) => setInputSchemaText(e.target.value)}
        InputProps={{ sx: { fontFamily: 'monospace', fontSize: '0.85rem' } }}
      />

      <TextField
        fullWidth
        multiline
        rows={5}
        label="Output schema (JSON)"
        value={outputSchemaText}
        onChange={(e) => setOutputSchemaText(e.target.value)}
        InputProps={{ sx: { fontFamily: 'monospace', fontSize: '0.85rem' } }}
      />

      <TextField
        fullWidth
        multiline
        rows={6}
        label="Execution steps (JSON array)"
        value={executionStepsText}
        onChange={(e) => setExecutionStepsText(e.target.value)}
        helperText="Ordered list of tool calls/prompts. Interpreted by the future skill-execution worker."
        InputProps={{ sx: { fontFamily: 'monospace', fontSize: '0.85rem' } }}
      />
    </Box>
  )

  const renderStep2 = () => (
    <Box sx={{ textAlign: 'center', py: 4 }}>
      <Typography variant="h6" gutterBottom>
        Skill saved
      </Typography>
      <Typography variant="body2" color="text.secondary">
        It will appear in the Skills list. Execution wiring is coming in a follow-up.
      </Typography>
    </Box>
  )

  const renderCurrent = () => {
    if (activeStep === 0) return renderStep0()
    if (activeStep === 1) return renderStep1()
    return renderStep2()
  }

  return (
    <Dialog open={open} onClose={handleClose} maxWidth="md" fullWidth>
      <DialogTitle sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
        <SparkIcon color="primary" />
        Build a Skill
        <Box sx={{ flex: 1 }} />
        <IconButton onClick={handleClose} size="small">
          <CloseIcon />
        </IconButton>
      </DialogTitle>

      <DialogContent dividers>
        <Stepper activeStep={activeStep} sx={{ mb: 3 }}>
          {STEPS.map((label) => (
            <Step key={label}>
              <StepLabel>{label}</StepLabel>
            </Step>
          ))}
        </Stepper>

        {error && (
          <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
            {error}
          </Alert>
        )}
        {success && (
          <Alert severity="success" sx={{ mb: 2 }} onClose={() => setSuccess(null)}>
            {success}
          </Alert>
        )}

        {renderCurrent()}
      </DialogContent>

      <DialogActions sx={{ px: 3, py: 2 }}>
        <Button onClick={handleClose} color="inherit">
          Cancel
        </Button>
        <Box sx={{ flex: 1 }} />
        {activeStep === 1 && (
          <Button onClick={() => setActiveStep(0)} color="inherit">
            Back
          </Button>
        )}
        {activeStep === 0 && !needsClarification && (
          <Button
            variant="contained"
            onClick={handleGenerateClick}
            disabled={loading || !description.trim()}
            startIcon={loading ? <CircularProgress size={18} /> : <SparkIcon />}
          >
            {loading ? 'Generating…' : 'Generate'}
          </Button>
        )}
        {activeStep === 1 && (
          <Button
            variant="contained"
            onClick={handleSave}
            disabled={loading}
            startIcon={loading ? <CircularProgress size={18} /> : <SaveIcon />}
          >
            {loading ? 'Saving…' : 'Save Skill'}
          </Button>
        )}
      </DialogActions>

      <Divider />
    </Dialog>
  )
}
