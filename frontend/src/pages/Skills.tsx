import { useEffect, useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import {
  Box,
  Card,
  CardContent,
  CardActions,
  Typography,
  Button,
  Chip,
  CircularProgress,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  TextField,
  Grid,
  IconButton,
  Tooltip,
  Alert,
  Snackbar,
  alpha,
  useTheme,
  Divider,
  Stack,
} from '@mui/material'
import {
  PlayArrow as PlayIcon,
  Refresh as RefreshIcon,
  ArrowForward as ArrowIcon,
  BugReport as BugIcon,
  Search as SearchIcon,
  Shield as ShieldIcon,
  Biotech as ForensicsIcon,
  Policy as InvestigateIcon,
  Info as InfoIcon,
} from '@mui/icons-material'
import { skillsApi } from '../services/api'

interface LayoutContext {
  handleInvestigate: (findingId: string, agentId: string, prompt: string, title: string) => void
}

interface SkillData {
  id: string
  name: string
  description: string
  agents: string[]
  tools_used: string[]
  use_case: string
  trigger_examples: string[]
  body?: string
}

// Map agent IDs to display info
const AGENT_DISPLAY: Record<string, { name: string; icon: string; color: string }> = {
  triage: { name: 'Triage', icon: 'T', color: '#FF6B6B' },
  investigator: { name: 'Investigator', icon: 'I', color: '#4ECDC4' },
  threat_hunter: { name: 'Threat Hunter', icon: 'H', color: '#95E1D3' },
  correlator: { name: 'Correlator', icon: 'C', color: '#F38181' },
  responder: { name: 'Responder', icon: 'R', color: '#FF8B94' },
  reporter: { name: 'Reporter', icon: 'W', color: '#A8E6CF' },
  mitre_analyst: { name: 'MITRE Analyst', icon: 'M', color: '#FFD3B6' },
  forensics: { name: 'Forensics', icon: 'F', color: '#FFAAA5' },
  threat_intel: { name: 'Threat Intel', icon: 'TI', color: '#B4A7D6' },
  compliance: { name: 'Compliance', icon: 'CP', color: '#C7CEEA' },
  malware_analyst: { name: 'Malware Analyst', icon: 'MA', color: '#FF6B9D' },
  network_analyst: { name: 'Network Analyst', icon: 'NA', color: '#56CCF2' },
  auto_responder: { name: 'Auto-Responder', icon: 'AR', color: '#FF6B6B' },
}

// Map skill IDs to icons
const SKILL_ICONS: Record<string, React.ReactNode> = {
  'incident-response': <ShieldIcon sx={{ fontSize: 32 }} />,
  'full-investigation': <InvestigateIcon sx={{ fontSize: 32 }} />,
  'threat-hunt': <SearchIcon sx={{ fontSize: 32 }} />,
  'forensic-analysis': <ForensicsIcon sx={{ fontSize: 32 }} />,
}

// Skill accent colors
const SKILL_COLORS: Record<string, string> = {
  'incident-response': '#FF6B6B',
  'full-investigation': '#4ECDC4',
  'threat-hunt': '#95E1D3',
  'forensic-analysis': '#FFAAA5',
}

export default function Skills() {
  const { handleInvestigate } = useOutletContext<LayoutContext>()
  const theme = useTheme()
  const isDark = theme.palette.mode === 'dark'

  const [skills, setSkills] = useState<SkillData[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // Execute dialog state
  const [executeDialogOpen, setExecuteDialogOpen] = useState(false)
  const [selectedSkill, setSelectedSkill] = useState<SkillData | null>(null)
  const [executeParams, setExecuteParams] = useState({
    finding_id: '',
    case_id: '',
    context: '',
    hypothesis: '',
  })
  const [executing, setExecuting] = useState(false)

  // Detail dialog state
  const [detailDialogOpen, setDetailDialogOpen] = useState(false)
  const [detailSkill, setDetailSkill] = useState<SkillData | null>(null)
  const [loadingDetail, setLoadingDetail] = useState(false)

  // Snackbar
  const [snackbar, setSnackbar] = useState<{
    open: boolean
    message: string
    severity: 'success' | 'error' | 'info'
  }>({ open: false, message: '', severity: 'info' })

  useEffect(() => {
    loadSkills()
  }, [])

  const loadSkills = async () => {
    setLoading(true)
    setError(null)
    try {
      const response = await skillsApi.listSkills()
      setSkills(response.data.workflows || [])
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to load skills')
    } finally {
      setLoading(false)
    }
  }

  const handleReload = async () => {
    setLoading(true)
    setError(null)
    try {
      await skillsApi.reloadSkills()
      const response = await skillsApi.listSkills()
      setSkills(response.data.workflows || [])
      setSnackbar({ open: true, message: 'Skills reloaded from disk', severity: 'success' })
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to reload skills')
    } finally {
      setLoading(false)
    }
  }

  const handleOpenExecute = (skill: SkillData) => {
    setSelectedSkill(skill)
    setExecuteParams({ finding_id: '', case_id: '', context: '', hypothesis: '' })
    setExecuteDialogOpen(true)
  }

  const handleExecute = async () => {
    if (!selectedSkill) return

    // Build params, omitting empty strings
    const params: Record<string, string> = {}
    if (executeParams.finding_id) params.finding_id = executeParams.finding_id
    if (executeParams.case_id) params.case_id = executeParams.case_id
    if (executeParams.context) params.context = executeParams.context
    if (executeParams.hypothesis) params.hypothesis = executeParams.hypothesis

    if (Object.keys(params).length === 0) {
      setSnackbar({ open: true, message: 'Please provide at least one parameter', severity: 'error' })
      return
    }

    setExecuting(true)
    try {
      // Send to chat via handleInvestigate so the user sees the streaming response
      const prompt = buildSkillPrompt(selectedSkill, params)
      handleInvestigate(
        params.finding_id || '',
        selectedSkill.agents[0] || 'investigator',
        prompt,
        `Skill: ${selectedSkill.name}`,
      )
      setExecuteDialogOpen(false)
      setSnackbar({
        open: true,
        message: `Launched "${selectedSkill.name}" skill workflow in chat`,
        severity: 'success',
      })
    } catch (err: any) {
      setSnackbar({
        open: true,
        message: err.response?.data?.detail || 'Failed to execute skill',
        severity: 'error',
      })
    } finally {
      setExecuting(false)
    }
  }

  const buildSkillPrompt = (skill: SkillData, params: Record<string, string>): string => {
    let prompt = `Please execute the **${skill.name}** skill workflow.\n\n`
    prompt += `**Workflow:** ${skill.agents.map(a => AGENT_DISPLAY[a]?.name || a).join(' → ')}\n\n`

    if (params.finding_id) prompt += `**Target Finding:** ${params.finding_id}\n`
    if (params.case_id) prompt += `**Target Case:** ${params.case_id}\n`
    if (params.hypothesis) prompt += `**Hunt Hypothesis:** ${params.hypothesis}\n`
    if (params.context) prompt += `**Additional Context:** ${params.context}\n`

    prompt += `\nFollow each phase of the ${skill.name} workflow in sequence, using the appropriate agent role and tools for each phase. `
    prompt += `Pass context between phases and provide a final consolidated summary when complete.`

    return prompt
  }

  const handleViewDetail = async (skillId: string) => {
    setLoadingDetail(true)
    setDetailDialogOpen(true)
    try {
      const response = await skillsApi.getSkill(skillId)
      setDetailSkill(response.data)
    } catch (err: any) {
      setSnackbar({
        open: true,
        message: 'Failed to load skill details',
        severity: 'error',
      })
      setDetailDialogOpen(false)
    } finally {
      setLoadingDetail(false)
    }
  }

  if (loading) {
    return (
      <Box sx={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '60vh' }}>
        <CircularProgress />
      </Box>
    )
  }

  if (error) {
    return (
      <Box sx={{ p: 3 }}>
        <Alert severity="error" action={
          <Button color="inherit" size="small" onClick={loadSkills}>Retry</Button>
        }>
          {error}
        </Alert>
      </Box>
    )
  }

  return (
    <Box sx={{ p: 3 }}>
      {/* Header */}
      <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 3 }}>
        <Box>
          <Typography variant="h5" fontWeight={700}>
            Workflows & Skills
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
            Pre-built multi-agent workflows for common SOC operations. Each skill sequences specialized agents to handle complex tasks end-to-end.
          </Typography>
        </Box>
        <Tooltip title="Reload skills from disk">
          <IconButton onClick={handleReload} size="small">
            <RefreshIcon />
          </IconButton>
        </Tooltip>
      </Box>

      {/* Skills Grid */}
      <Grid container spacing={3}>
        {skills.map((skill) => {
          const accentColor = SKILL_COLORS[skill.id] || theme.palette.primary.main
          const icon = SKILL_ICONS[skill.id] || <BugIcon sx={{ fontSize: 32 }} />

          return (
            <Grid item xs={12} md={6} key={skill.id}>
              <Card
                elevation={0}
                sx={{
                  height: '100%',
                  display: 'flex',
                  flexDirection: 'column',
                  border: 1,
                  borderColor: 'divider',
                  borderRadius: 2,
                  transition: 'all 0.2s ease',
                  '&:hover': {
                    borderColor: accentColor,
                    boxShadow: `0 0 0 1px ${alpha(accentColor, 0.3)}`,
                  },
                }}
              >
                <CardContent sx={{ flex: 1, pb: 1 }}>
                  {/* Skill header */}
                  <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 2, mb: 2 }}>
                    <Box
                      sx={{
                        p: 1.5,
                        borderRadius: 2,
                        bgcolor: alpha(accentColor, isDark ? 0.15 : 0.1),
                        color: accentColor,
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                      }}
                    >
                      {icon}
                    </Box>
                    <Box sx={{ flex: 1 }}>
                      <Typography variant="h6" fontWeight={600} sx={{ lineHeight: 1.3 }}>
                        {skill.name.split('-').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ')}
                      </Typography>
                      <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
                        {skill.description}
                      </Typography>
                    </Box>
                  </Box>

                  {/* Agent chain */}
                  <Box sx={{ mb: 2 }}>
                    <Typography variant="caption" color="text.secondary" fontWeight={600} sx={{ mb: 1, display: 'block' }}>
                      AGENT SEQUENCE
                    </Typography>
                    <Stack direction="row" spacing={0.5} alignItems="center" flexWrap="wrap" useFlexGap>
                      {skill.agents.map((agentId, idx) => {
                        const agent = AGENT_DISPLAY[agentId] || {
                          name: agentId.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()),
                          icon: agentId.charAt(0).toUpperCase(),
                          color: theme.palette.grey[500],
                        }
                        return (
                          <Box key={agentId} sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
                            <Chip
                              label={agent.name}
                              size="small"
                              sx={{
                                bgcolor: alpha(agent.color, isDark ? 0.2 : 0.12),
                                color: isDark ? agent.color : undefined,
                                fontWeight: 500,
                                fontSize: '0.75rem',
                                height: 28,
                                '& .MuiChip-label': { px: 1 },
                              }}
                              avatar={
                                <Box
                                  component="span"
                                  sx={{
                                    width: 22,
                                    height: 22,
                                    borderRadius: '50%',
                                    bgcolor: agent.color,
                                    color: '#fff',
                                    display: 'flex',
                                    alignItems: 'center',
                                    justifyContent: 'center',
                                    fontSize: '0.65rem',
                                    fontWeight: 700,
                                    ml: '4px !important',
                                  }}
                                >
                                  {agent.icon}
                                </Box>
                              }
                            />
                            {idx < skill.agents.length - 1 && (
                              <ArrowIcon sx={{ fontSize: 14, color: 'text.disabled' }} />
                            )}
                          </Box>
                        )
                      })}
                    </Stack>
                  </Box>

                  {/* Use case */}
                  <Box sx={{ mb: 1 }}>
                    <Typography variant="caption" color="text.secondary" fontWeight={600} sx={{ mb: 0.5, display: 'block' }}>
                      USE CASE
                    </Typography>
                    <Typography variant="body2" color="text.secondary" sx={{ fontStyle: 'italic' }}>
                      {skill.use_case}
                    </Typography>
                  </Box>

                  {/* Example triggers */}
                  {skill.trigger_examples && skill.trigger_examples.length > 0 && (
                    <Box>
                      <Typography variant="caption" color="text.secondary" fontWeight={600} sx={{ mb: 0.5, display: 'block' }}>
                        EXAMPLE COMMANDS
                      </Typography>
                      <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5 }}>
                        {skill.trigger_examples.slice(0, 2).map((example, idx) => (
                          <Typography
                            key={idx}
                            variant="caption"
                            sx={{
                              px: 1,
                              py: 0.5,
                              borderRadius: 1,
                              bgcolor: isDark ? alpha(theme.palette.common.white, 0.05) : alpha(theme.palette.common.black, 0.04),
                              fontFamily: 'monospace',
                              fontSize: '0.7rem',
                            }}
                          >
                            "{example}"
                          </Typography>
                        ))}
                      </Box>
                    </Box>
                  )}
                </CardContent>

                <Divider />

                <CardActions sx={{ px: 2, py: 1.5, justifyContent: 'space-between' }}>
                  <Button
                    size="small"
                    startIcon={<InfoIcon />}
                    onClick={() => handleViewDetail(skill.id)}
                    sx={{ textTransform: 'none' }}
                  >
                    View Details
                  </Button>
                  <Button
                    variant="contained"
                    size="small"
                    startIcon={<PlayIcon />}
                    onClick={() => handleOpenExecute(skill)}
                    sx={{
                      textTransform: 'none',
                      bgcolor: accentColor,
                      '&:hover': { bgcolor: alpha(accentColor, 0.85) },
                    }}
                  >
                    Run Skill
                  </Button>
                </CardActions>
              </Card>
            </Grid>
          )
        })}
      </Grid>

      {skills.length === 0 && !loading && (
        <Box sx={{ textAlign: 'center', py: 6 }}>
          <Typography variant="h6" color="text.secondary">
            No skills found
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
            Add SKILL.md files to the <code>skills/</code> directory to get started.
          </Typography>
        </Box>
      )}

      {/* Execute Dialog */}
      <Dialog
        open={executeDialogOpen}
        onClose={() => !executing && setExecuteDialogOpen(false)}
        maxWidth="sm"
        fullWidth
      >
        <DialogTitle>
          Run: {selectedSkill?.name.split('-').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ')}
        </DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            Provide a target for this workflow. At least one field is required.
          </Typography>

          <TextField
            label="Finding ID"
            placeholder="e.g., f-20260215-abc123"
            fullWidth
            size="small"
            value={executeParams.finding_id}
            onChange={(e) => setExecuteParams(prev => ({ ...prev, finding_id: e.target.value }))}
            sx={{ mb: 2 }}
          />

          <TextField
            label="Case ID"
            placeholder="e.g., case-20260215-xyz789"
            fullWidth
            size="small"
            value={executeParams.case_id}
            onChange={(e) => setExecuteParams(prev => ({ ...prev, case_id: e.target.value }))}
            sx={{ mb: 2 }}
          />

          {selectedSkill?.id === 'threat-hunt' && (
            <TextField
              label="Hunt Hypothesis"
              placeholder="e.g., Suspected APT28 credential harvesting via T1078"
              fullWidth
              size="small"
              multiline
              rows={2}
              value={executeParams.hypothesis}
              onChange={(e) => setExecuteParams(prev => ({ ...prev, hypothesis: e.target.value }))}
              sx={{ mb: 2 }}
            />
          )}

          <TextField
            label="Additional Context"
            placeholder="Any additional context or instructions for the workflow..."
            fullWidth
            size="small"
            multiline
            rows={3}
            value={executeParams.context}
            onChange={(e) => setExecuteParams(prev => ({ ...prev, context: e.target.value }))}
          />
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <Button onClick={() => setExecuteDialogOpen(false)} disabled={executing}>
            Cancel
          </Button>
          <Button
            variant="contained"
            onClick={handleExecute}
            disabled={executing}
            startIcon={executing ? <CircularProgress size={16} /> : <PlayIcon />}
            sx={{
              bgcolor: selectedSkill ? SKILL_COLORS[selectedSkill.id] || theme.palette.primary.main : undefined,
            }}
          >
            {executing ? 'Launching...' : 'Execute Workflow'}
          </Button>
        </DialogActions>
      </Dialog>

      {/* Detail Dialog */}
      <Dialog
        open={detailDialogOpen}
        onClose={() => setDetailDialogOpen(false)}
        maxWidth="md"
        fullWidth
      >
        <DialogTitle>
          {detailSkill
            ? detailSkill.name.split('-').map((w: string) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ')
            : 'Loading...'}
        </DialogTitle>
        <DialogContent>
          {loadingDetail ? (
            <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
              <CircularProgress />
            </Box>
          ) : detailSkill ? (
            <Box>
              {/* Agent chain */}
              <Box sx={{ mb: 3 }}>
                <Typography variant="subtitle2" gutterBottom>Agent Sequence</Typography>
                <Stack direction="row" spacing={0.5} alignItems="center" flexWrap="wrap" useFlexGap>
                  {detailSkill.agents.map((agentId: string, idx: number) => {
                    const agent = AGENT_DISPLAY[agentId] || {
                      name: agentId.replace(/_/g, ' ').replace(/\b\w/g, (c: string) => c.toUpperCase()),
                      icon: agentId.charAt(0).toUpperCase(),
                      color: theme.palette.grey[500],
                    }
                    return (
                      <Box key={agentId} sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
                        <Chip
                          label={agent.name}
                          size="small"
                          sx={{
                            bgcolor: alpha(agent.color, isDark ? 0.2 : 0.12),
                            color: isDark ? agent.color : undefined,
                            fontWeight: 500,
                          }}
                        />
                        {idx < detailSkill.agents.length - 1 && (
                          <ArrowIcon sx={{ fontSize: 14, color: 'text.disabled' }} />
                        )}
                      </Box>
                    )
                  })}
                </Stack>
              </Box>

              {/* Tools */}
              <Box sx={{ mb: 3 }}>
                <Typography variant="subtitle2" gutterBottom>Tools Used</Typography>
                <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5 }}>
                  {detailSkill.tools_used.map((tool: string) => (
                    <Chip
                      key={tool}
                      label={tool}
                      size="small"
                      variant="outlined"
                      sx={{ fontFamily: 'monospace', fontSize: '0.75rem' }}
                    />
                  ))}
                </Box>
              </Box>

              <Divider sx={{ my: 2 }} />

              {/* Markdown body rendered as plain text (preserving structure) */}
              <Box
                sx={{
                  fontFamily: 'monospace',
                  fontSize: '0.8rem',
                  whiteSpace: 'pre-wrap',
                  bgcolor: isDark ? alpha(theme.palette.common.white, 0.03) : alpha(theme.palette.common.black, 0.02),
                  p: 2,
                  borderRadius: 1,
                  maxHeight: '60vh',
                  overflow: 'auto',
                }}
              >
                {detailSkill.body}
              </Box>
            </Box>
          ) : null}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDetailDialogOpen(false)}>Close</Button>
          {detailSkill && (
            <Button
              variant="contained"
              startIcon={<PlayIcon />}
              onClick={() => {
                setDetailDialogOpen(false)
                handleOpenExecute(detailSkill)
              }}
            >
              Run This Skill
            </Button>
          )}
        </DialogActions>
      </Dialog>

      {/* Snackbar */}
      <Snackbar
        open={snackbar.open}
        autoHideDuration={4000}
        onClose={() => setSnackbar(prev => ({ ...prev, open: false }))}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
      >
        <Alert severity={snackbar.severity} variant="filled" onClose={() => setSnackbar(prev => ({ ...prev, open: false }))}>
          {snackbar.message}
        </Alert>
      </Snackbar>
    </Box>
  )
}
