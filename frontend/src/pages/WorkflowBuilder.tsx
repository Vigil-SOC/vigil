import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Autocomplete,
  Box,
  Button,
  Card,
  CardActions,
  CardContent,
  Checkbox,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControl,
  FormControlLabel,
  Grid,
  IconButton,
  InputLabel,
  MenuItem,
  Select,
  Snackbar,
  Stack,
  TextField,
  Tooltip,
  Typography,
  alpha,
  useTheme,
} from '@mui/material'
import {
  Add as AddIcon,
  ArrowDownward as DownIcon,
  ArrowUpward as UpIcon,
  AutoAwesome as AIIcon,
  Close as CloseIcon,
  Delete as DeleteIcon,
  Edit as EditIcon,
  PlayArrow as PlayIcon,
  Save as SaveIcon,
} from '@mui/icons-material'
import {
  addEdge,
  applyEdgeChanges,
  applyNodeChanges,
  Background,
  Controls,
  MarkerType,
  MiniMap,
  ReactFlow,
  type Connection,
  type Edge,
  type EdgeChange,
  type Node,
  type NodeChange,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { agentsApi, workflowApi, type WorkflowPhase } from '../services/api'

// -----------------------------------------------------------------------------
// Types
// -----------------------------------------------------------------------------

interface WorkflowListItem {
  id: string
  name: string
  description: string
  agents: string[]
  tools_used: string[]
  use_case: string
  trigger_examples: string[]
  source?: 'file' | 'custom'
  phases?: WorkflowPhase[]
}

interface CustomWorkflowRecord {
  workflow_id: string
  name: string
  description: string
  use_case?: string
  trigger_examples: string[]
  phases: WorkflowPhase[]
  graph_layout?: Record<string, any>
  is_active: boolean
  version: number
}

type View = 'list' | 'editor'

// Known built-in agent IDs mapped to readable labels (matches soc_agents.py)
const AGENT_OPTIONS: Array<{ id: string; label: string }> = [
  { id: 'triage', label: 'Triage Agent' },
  { id: 'investigator', label: 'Investigation Agent' },
  { id: 'threat_hunter', label: 'Threat Hunter' },
  { id: 'correlator', label: 'Correlator' },
  { id: 'responder', label: 'Responder' },
  { id: 'reporter', label: 'Reporter' },
  { id: 'mitre_analyst', label: 'MITRE Analyst' },
  { id: 'forensics', label: 'Forensics' },
  { id: 'threat_intel', label: 'Threat Intel' },
  { id: 'compliance', label: 'Compliance' },
  { id: 'malware_analyst', label: 'Malware Analyst' },
  { id: 'network_analyst', label: 'Network Analyst' },
]

const emptyPhase = (order: number): WorkflowPhase => ({
  phase_id: `phase-${order}`,
  order,
  agent_id: 'triage',
  name: `Phase ${order}`,
  purpose: '',
  tools: [],
  steps: [],
  expected_output: '',
  timeout_seconds: 300,
  approval_required: false,
})

interface EditorState {
  workflow_id: string | null // null when unsaved
  name: string
  description: string
  use_case: string
  trigger_examples: string[]
  phases: WorkflowPhase[]
}

const emptyEditor = (): EditorState => ({
  workflow_id: null,
  name: '',
  description: '',
  use_case: '',
  trigger_examples: [],
  phases: [emptyPhase(1)],
})

// -----------------------------------------------------------------------------
// Component
// -----------------------------------------------------------------------------

export default function WorkflowBuilder() {
  const theme = useTheme()
  const isDark = theme.palette.mode === 'dark'

  const [view, setView] = useState<View>('list')
  const [workflows, setWorkflows] = useState<WorkflowListItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const [editor, setEditor] = useState<EditorState>(emptyEditor())
  const [saving, setSaving] = useState(false)

  const [generateOpen, setGenerateOpen] = useState(false)
  const [generatePrompt, setGeneratePrompt] = useState('')
  const [generating, setGenerating] = useState(false)

  const [executeOpen, setExecuteOpen] = useState(false)
  const [executeTarget, setExecuteTarget] = useState<WorkflowListItem | null>(null)
  const [executeParams, setExecuteParams] = useState({
    finding_id: '',
    case_id: '',
    context: '',
    hypothesis: '',
  })
  const [executing, setExecuting] = useState(false)

  const [snackbar, setSnackbar] = useState<{
    open: boolean
    message: string
    severity: 'success' | 'error' | 'info'
  }>({ open: false, message: '', severity: 'info' })

  const notify = (message: string, severity: 'success' | 'error' | 'info' = 'info') =>
    setSnackbar({ open: true, message, severity })

  // ---------- Load list ------------------------------------------------------

  const loadWorkflows = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await workflowApi.listAll()
      setWorkflows(res.data.workflows || [])
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to load workflows')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    loadWorkflows()
  }, [loadWorkflows])

  // ---------- Editor actions -------------------------------------------------

  const openNewEditor = () => {
    setEditor(emptyEditor())
    setView('editor')
  }

  const openEditWorkflow = async (wf: WorkflowListItem) => {
    if (wf.source !== 'custom') {
      notify('File-based workflows are not editable here.', 'info')
      return
    }
    try {
      const res = await workflowApi.getCustom(wf.id)
      const full: CustomWorkflowRecord = res.data
      setEditor({
        workflow_id: full.workflow_id,
        name: full.name,
        description: full.description,
        use_case: full.use_case || '',
        trigger_examples: full.trigger_examples || [],
        phases: (full.phases || []).map((p, i) => ({
          ...emptyPhase(i + 1),
          ...p,
          order: i + 1,
        })),
      })
      setView('editor')
    } catch (err: any) {
      notify(err.response?.data?.detail || 'Failed to load workflow', 'error')
    }
  }

  const saveEditor = async () => {
    if (!editor.name.trim() || !editor.description.trim()) {
      notify('Name and description are required.', 'error')
      return
    }
    if (editor.phases.length === 0) {
      notify('Add at least one phase.', 'error')
      return
    }
    setSaving(true)
    const payload = {
      name: editor.name.trim(),
      description: editor.description.trim(),
      use_case: editor.use_case.trim(),
      trigger_examples: editor.trigger_examples.filter((t) => t.trim()),
      phases: editor.phases.map((p, i) => ({
        ...p,
        order: i + 1,
        phase_id: p.phase_id || `phase-${i + 1}`,
      })),
    }
    try {
      if (editor.workflow_id) {
        await workflowApi.updateCustom(editor.workflow_id, payload)
        notify('Workflow updated.', 'success')
      } else {
        const res = await workflowApi.createCustom(payload)
        setEditor((e) => ({ ...e, workflow_id: res.data.workflow_id }))
        notify('Workflow created.', 'success')
      }
      await loadWorkflows()
      setView('list')
    } catch (err: any) {
      notify(err.response?.data?.detail || 'Save failed', 'error')
    } finally {
      setSaving(false)
    }
  }

  const deleteWorkflow = async (wf: WorkflowListItem) => {
    if (wf.source !== 'custom') return
    if (!window.confirm(`Deactivate workflow "${wf.name}"?`)) return
    try {
      await workflowApi.deleteCustom(wf.id)
      notify('Workflow deactivated.', 'success')
      await loadWorkflows()
    } catch (err: any) {
      notify(err.response?.data?.detail || 'Delete failed', 'error')
    }
  }

  // ---------- Generation -----------------------------------------------------

  const runGenerate = async () => {
    if (!generatePrompt.trim()) {
      notify('Describe the scenario first.', 'error')
      return
    }
    setGenerating(true)
    try {
      const res = await workflowApi.generate(generatePrompt.trim())
      const draft = res.data.draft
      setEditor({
        workflow_id: null,
        name: draft.name || '',
        description: draft.description || '',
        use_case: draft.use_case || '',
        trigger_examples: draft.trigger_examples || [],
        phases: (draft.phases || []).map((p: WorkflowPhase, i: number) => ({
          ...emptyPhase(i + 1),
          ...p,
          order: i + 1,
        })),
      })
      setGenerateOpen(false)
      setGeneratePrompt('')
      setView('editor')
      notify('Draft generated. Review and save.', 'success')
    } catch (err: any) {
      notify(err.response?.data?.detail || 'Generation failed', 'error')
    } finally {
      setGenerating(false)
    }
  }

  // ---------- Execution ------------------------------------------------------

  const runExecute = async () => {
    if (!executeTarget) return
    const params: Record<string, string> = {}
    for (const [k, v] of Object.entries(executeParams)) {
      if (v.trim()) params[k] = v.trim()
    }
    if (Object.keys(params).length === 0) {
      notify('Provide at least one parameter.', 'error')
      return
    }
    setExecuting(true)
    try {
      await workflowApi.execute(executeTarget.id, params)
      notify(`Workflow "${executeTarget.name}" executed.`, 'success')
      setExecuteOpen(false)
      setExecuteTarget(null)
      setExecuteParams({ finding_id: '', case_id: '', context: '', hypothesis: '' })
    } catch (err: any) {
      notify(err.response?.data?.detail || 'Execution failed', 'error')
    } finally {
      setExecuting(false)
    }
  }

  // ---------- Render ---------------------------------------------------------

  if (view === 'editor') {
    return (
      <EditorView
        editor={editor}
        setEditor={setEditor}
        saving={saving}
        onCancel={() => setView('list')}
        onSave={saveEditor}
        snackbar={snackbar}
        setSnackbar={setSnackbar}
        isDark={isDark}
      />
    )
  }

  return (
    <Box sx={{ p: 3, height: '100%', overflow: 'auto' }}>
      <Stack
        direction="row"
        alignItems="center"
        justifyContent="space-between"
        sx={{ mb: 3 }}
      >
        <Box>
          <Typography variant="h4" sx={{ fontWeight: 700 }}>
            Workflow Builder
          </Typography>
          <Typography variant="body2" color="text.secondary">
            Create, customize, and execute multi-agent workflows.
          </Typography>
        </Box>
        <Stack direction="row" spacing={1}>
          <Button
            variant="outlined"
            startIcon={<AIIcon />}
            onClick={() => setGenerateOpen(true)}
          >
            Generate with AI
          </Button>
          <Button variant="contained" startIcon={<AddIcon />} onClick={openNewEditor}>
            New Workflow
          </Button>
        </Stack>
      </Stack>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }}>
          {error}
        </Alert>
      )}

      {loading ? (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 6 }}>
          <CircularProgress />
        </Box>
      ) : (
        <Grid container spacing={2}>
          {workflows.map((wf) => (
            <Grid item xs={12} md={6} lg={4} key={wf.id}>
              <Card
                sx={{
                  height: '100%',
                  display: 'flex',
                  flexDirection: 'column',
                  borderLeft: 4,
                  borderColor:
                    wf.source === 'custom' ? 'primary.main' : alpha(theme.palette.text.primary, 0.3),
                }}
              >
                <CardContent sx={{ flex: 1 }}>
                  <Stack
                    direction="row"
                    justifyContent="space-between"
                    alignItems="flex-start"
                    sx={{ mb: 1 }}
                  >
                    <Typography variant="h6" sx={{ fontWeight: 600 }}>
                      {wf.name}
                    </Typography>
                    <Chip
                      size="small"
                      label={wf.source === 'custom' ? 'Custom' : 'Built-in'}
                      color={wf.source === 'custom' ? 'primary' : 'default'}
                    />
                  </Stack>
                  <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
                    {wf.description}
                  </Typography>
                  <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap>
                    {(wf.agents || []).slice(0, 6).map((a) => (
                      <Chip key={a} size="small" label={a} variant="outlined" />
                    ))}
                  </Stack>
                </CardContent>
                <CardActions>
                  <Button
                    size="small"
                    startIcon={<PlayIcon />}
                    onClick={() => {
                      setExecuteTarget(wf)
                      setExecuteOpen(true)
                    }}
                  >
                    Execute
                  </Button>
                  <Button
                    size="small"
                    startIcon={<EditIcon />}
                    disabled={wf.source !== 'custom'}
                    onClick={() => openEditWorkflow(wf)}
                  >
                    Edit
                  </Button>
                  <Button
                    size="small"
                    color="error"
                    startIcon={<DeleteIcon />}
                    disabled={wf.source !== 'custom'}
                    onClick={() => deleteWorkflow(wf)}
                  >
                    Delete
                  </Button>
                </CardActions>
              </Card>
            </Grid>
          ))}
          {workflows.length === 0 && (
            <Grid item xs={12}>
              <Alert severity="info">
                No workflows yet. Click "New Workflow" or "Generate with AI" to create one.
              </Alert>
            </Grid>
          )}
        </Grid>
      )}

      {/* Generate dialog */}
      <Dialog
        open={generateOpen}
        onClose={() => !generating && setGenerateOpen(false)}
        maxWidth="sm"
        fullWidth
      >
        <DialogTitle>Generate Workflow with AI</DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            Describe the security scenario in plain English. The AI will draft a multi-phase
            workflow you can edit before saving.
          </Typography>
          <TextField
            autoFocus
            multiline
            minRows={4}
            fullWidth
            label="Scenario description"
            placeholder="e.g. Investigate suspicious login activity and contain the account if credentials look compromised."
            value={generatePrompt}
            onChange={(e) => setGeneratePrompt(e.target.value)}
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setGenerateOpen(false)} disabled={generating}>
            Cancel
          </Button>
          <Button
            variant="contained"
            startIcon={generating ? <CircularProgress size={16} /> : <AIIcon />}
            disabled={generating}
            onClick={runGenerate}
          >
            Generate Draft
          </Button>
        </DialogActions>
      </Dialog>

      {/* Execute dialog */}
      <Dialog
        open={executeOpen}
        onClose={() => !executing && setExecuteOpen(false)}
        maxWidth="sm"
        fullWidth
      >
        <DialogTitle>
          Execute: {executeTarget?.name}
          <IconButton
            onClick={() => setExecuteOpen(false)}
            size="small"
            sx={{ position: 'absolute', right: 8, top: 8 }}
          >
            <CloseIcon />
          </IconButton>
        </DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
            <TextField
              label="Finding ID"
              value={executeParams.finding_id}
              onChange={(e) =>
                setExecuteParams((p) => ({ ...p, finding_id: e.target.value }))
              }
              placeholder="f-YYYYMMDD-XXXXXXXX"
              fullWidth
            />
            <TextField
              label="Case ID"
              value={executeParams.case_id}
              onChange={(e) => setExecuteParams((p) => ({ ...p, case_id: e.target.value }))}
              fullWidth
            />
            <TextField
              label="Hypothesis"
              value={executeParams.hypothesis}
              onChange={(e) =>
                setExecuteParams((p) => ({ ...p, hypothesis: e.target.value }))
              }
              fullWidth
              multiline
              minRows={2}
            />
            <TextField
              label="Additional context"
              value={executeParams.context}
              onChange={(e) => setExecuteParams((p) => ({ ...p, context: e.target.value }))}
              fullWidth
              multiline
              minRows={2}
            />
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setExecuteOpen(false)} disabled={executing}>
            Cancel
          </Button>
          <Button
            variant="contained"
            startIcon={executing ? <CircularProgress size={16} /> : <PlayIcon />}
            disabled={executing}
            onClick={runExecute}
          >
            Execute
          </Button>
        </DialogActions>
      </Dialog>

      <Snackbar
        open={snackbar.open}
        autoHideDuration={4000}
        onClose={() => setSnackbar((s) => ({ ...s, open: false }))}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
      >
        <Alert severity={snackbar.severity} onClose={() => setSnackbar((s) => ({ ...s, open: false }))}>
          {snackbar.message}
        </Alert>
      </Snackbar>
    </Box>
  )
}

// -----------------------------------------------------------------------------
// Editor view — form (left) + xyflow canvas preview (right)
// -----------------------------------------------------------------------------

interface EditorViewProps {
  editor: EditorState
  setEditor: React.Dispatch<React.SetStateAction<EditorState>>
  saving: boolean
  onCancel: () => void
  onSave: () => void
  snackbar: { open: boolean; message: string; severity: 'success' | 'error' | 'info' }
  setSnackbar: React.Dispatch<
    React.SetStateAction<{ open: boolean; message: string; severity: 'success' | 'error' | 'info' }>
  >
  isDark: boolean
}

function EditorView({
  editor,
  setEditor,
  saving,
  onCancel,
  onSave,
  snackbar,
  setSnackbar,
  isDark,
}: EditorViewProps) {
  const initialGraph = useMemo(() => buildGraph(editor.phases), [])
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const [nodes, setNodes] = useState<Node[]>(initialGraph.nodes)
  const [edges, setEdges] = useState<Edge[]>(initialGraph.edges)
  const [edgeLabelEdit, setEdgeLabelEdit] = useState<{ edgeId: string; label: string } | null>(null)

  // Keep nodes in sync with editor.phases when phases are added/removed/renamed.
  // We preserve user-dragged positions for phases that still exist.
  useEffect(() => {
    setNodes((prev) => {
      const prevById = new Map(prev.map((n) => [n.id, n]))
      const fresh = buildGraph(editor.phases).nodes
      return fresh.map((n) => {
        const existing = prevById.get(n.id)
        return existing
          ? { ...n, position: existing.position, selected: existing.selected }
          : n
      })
    })
    setEdges((prev) => {
      // Drop edges whose endpoints no longer exist; keep user-created / custom-labeled ones.
      const validIds = new Set(
        editor.phases.map((p, i) => p.phase_id || `phase-${i + 1}`),
      )
      const keep = prev.filter(
        (e) => validIds.has(e.source) && validIds.has(e.target),
      )
      // If there are no edges at all (first render or all invalid) seed from defaults.
      if (keep.length === 0) return buildGraph(editor.phases).edges
      return keep
    })
  }, [editor.phases])

  const onNodesChange = useCallback(
    (changes: NodeChange[]) => setNodes((nds) => applyNodeChanges(changes, nds)),
    [],
  )
  const onEdgesChange = useCallback(
    (changes: EdgeChange[]) => setEdges((eds) => applyEdgeChanges(changes, eds)),
    [],
  )
  const onConnect = useCallback((connection: Connection) => {
    setEdges((eds) =>
      addEdge(
        {
          ...connection,
          markerEnd: { type: MarkerType.ArrowClosed },
        },
        eds,
      ),
    )
  }, [])
  const onEdgeDoubleClick = useCallback((_: any, edge: Edge) => {
    setEdgeLabelEdit({ edgeId: edge.id, label: String(edge.label || '') })
  }, [])
  const commitEdgeLabel = () => {
    if (!edgeLabelEdit) return
    const { edgeId, label } = edgeLabelEdit
    setEdges((eds) =>
      eds.map((e) =>
        e.id === edgeId
          ? {
              ...e,
              label: label || undefined,
              data: { ...(e.data || {}), condition: label || undefined },
            }
          : e,
      ),
    )
    setEdgeLabelEdit(null)
  }

  const updatePhase = (idx: number, patch: Partial<WorkflowPhase>) => {
    setEditor((e) => {
      const next = [...e.phases]
      next[idx] = { ...next[idx], ...patch }
      return { ...e, phases: next }
    })
  }

  const movePhase = (idx: number, delta: number) => {
    const target = idx + delta
    setEditor((e) => {
      if (target < 0 || target >= e.phases.length) return e
      const next = [...e.phases]
      const [item] = next.splice(idx, 1)
      next.splice(target, 0, item)
      return {
        ...e,
        phases: next.map((p, i) => ({ ...p, order: i + 1 })),
      }
    })
  }

  const removePhase = (idx: number) => {
    setEditor((e) => ({
      ...e,
      phases: e.phases
        .filter((_, i) => i !== idx)
        .map((p, i) => ({ ...p, order: i + 1, phase_id: p.phase_id || `phase-${i + 1}` })),
    }))
  }

  const [editPhaseIdx, setEditPhaseIdx] = useState<number | null>(null)
  const [phaseSnapshot, setPhaseSnapshot] = useState<WorkflowPhase | null>(null)
  const [phaseIsNew, setPhaseIsNew] = useState(false)
  const [availableTools, setAvailableTools] = useState<string[]>([])

  useEffect(() => {
    agentsApi
      .getAvailableTools()
      .then((res) => setAvailableTools(res.data.tools || []))
      .catch(() => { /* non-fatal; falls back to free text */ })
  }, [])

  const openPhase = (idx: number, isNew: boolean) => {
    setPhaseSnapshot(JSON.parse(JSON.stringify(editor.phases[idx] ?? null)))
    setPhaseIsNew(isNew)
    setEditPhaseIdx(idx)
  }

  const handleNodeClick = useCallback((_: any, node: Node) => {
    const id = node.id
    const idx = editor.phases.findIndex(
      (p, i) => (p.phase_id || `phase-${i + 1}`) === id
    )
    if (idx >= 0) openPhase(idx, false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editor.phases])

  const addAndEditPhase = () => {
    setEditor((e) => {
      const order = e.phases.length + 1
      const newPhase = emptyPhase(order)
      const next = [...e.phases, newPhase]
      setTimeout(() => {
        setPhaseSnapshot(JSON.parse(JSON.stringify(newPhase)))
        setPhaseIsNew(true)
        setEditPhaseIdx(next.length - 1)
      }, 0)
      return { ...e, phases: next }
    })
  }

  const closePhaseEditor = (commit: boolean) => {
    if (!commit && editPhaseIdx !== null) {
      if (phaseIsNew) {
        // Newly-added phase + Cancel → drop it entirely
        removePhase(editPhaseIdx)
      } else if (phaseSnapshot) {
        // Existing phase + Cancel → restore the pre-edit snapshot
        const restore = phaseSnapshot
        const idx = editPhaseIdx
        setEditor((e) => {
          const next = [...e.phases]
          next[idx] = restore
          return { ...e, phases: next }
        })
      }
    }
    setEditPhaseIdx(null)
    setPhaseSnapshot(null)
    setPhaseIsNew(false)
  }

  return (
    <Box
      sx={{
        display: 'flex',
        flexDirection: 'column',
        height: '100%',
        overflow: 'hidden',
      }}
    >
      {/* Top toolbar — title + Save/Cancel */}
      <Stack
        direction="row"
        alignItems="center"
        justifyContent="space-between"
        sx={{
          px: 3,
          py: 1.5,
          borderBottom: 1,
          borderColor: 'divider',
          bgcolor: 'background.paper',
          flexShrink: 0,
        }}
      >
        <Box sx={{ minWidth: 0 }}>
          <Typography variant="h6" sx={{ fontWeight: 700, lineHeight: 1.2 }} noWrap>
            {editor.workflow_id ? 'Edit Workflow' : 'New Workflow'}
          </Typography>
          <Typography variant="caption" color="text.secondary" noWrap>
            {editor.workflow_id ? editor.workflow_id : 'Draft — not yet saved'}
          </Typography>
        </Box>
        <Stack direction="row" spacing={1} sx={{ flexShrink: 0 }}>
          <Button onClick={onCancel} disabled={saving} size="small">
            Cancel
          </Button>
          <Button
            variant="contained"
            size="small"
            startIcon={saving ? <CircularProgress size={14} color="inherit" /> : <SaveIcon />}
            disabled={saving}
            onClick={onSave}
          >
            Save
          </Button>
        </Stack>
      </Stack>

      {/* Metadata strip — compact, above the canvas */}
      <Box
        sx={{
          px: 3,
          py: 1.5,
          borderBottom: 1,
          borderColor: 'divider',
          bgcolor: 'background.paper',
          flexShrink: 0,
        }}
      >
        <Grid container spacing={1.5} alignItems="flex-start">
          <Grid item xs={12} sm={6} md={3}>
            <TextField
              label="Name"
              required
              size="small"
              value={editor.name}
              onChange={(e) => setEditor((x) => ({ ...x, name: e.target.value }))}
              fullWidth
            />
          </Grid>
          <Grid item xs={12} sm={6} md={3}>
            <TextField
              label="Description"
              required
              size="small"
              value={editor.description}
              onChange={(e) => setEditor((x) => ({ ...x, description: e.target.value }))}
              fullWidth
            />
          </Grid>
          <Grid item xs={12} sm={6} md={2}>
            <TextField
              label="Use case"
              size="small"
              value={editor.use_case}
              onChange={(e) => setEditor((x) => ({ ...x, use_case: e.target.value }))}
              fullWidth
            />
          </Grid>
          <Grid item xs={12} sm={6} md={3}>
            <TextField
              label="Trigger examples (one per line)"
              size="small"
              value={editor.trigger_examples.join('\n')}
              onChange={(e) =>
                setEditor((x) => ({
                  ...x,
                  trigger_examples: e.target.value.split('\n'),
                }))
              }
              fullWidth
              multiline
              minRows={1}
              maxRows={3}
            />
          </Grid>
          <Grid item xs={12} md="auto" sx={{ display: 'flex', justifyContent: { xs: 'flex-start', md: 'flex-end' }, alignItems: 'center' }}>
            <Button
              size="small"
              variant="contained"
              startIcon={<AddIcon />}
              onClick={addAndEditPhase}
              sx={{ whiteSpace: 'nowrap', flexShrink: 0, px: 1.75 }}
            >
              Add Phase
            </Button>
          </Grid>
        </Grid>
      </Box>

      {/* Canvas — full width */}
      <Box sx={{ flex: 1, position: 'relative', minWidth: 0, minHeight: 0, bgcolor: 'background.default' }}>
        <Box sx={{ position: 'absolute', inset: 0 }}>
          <ReactFlow
            nodes={nodes}
            edges={edges}
            fitView
            nodesDraggable={true}
            nodesConnectable={true}
            elementsSelectable={true}
            edgesFocusable={true}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onNodeClick={handleNodeClick}
            onEdgeDoubleClick={onEdgeDoubleClick}
            proOptions={{ hideAttribution: true }}
            colorMode={isDark ? 'dark' : 'light'}
          >
            <Background />
            <MiniMap pannable zoomable />
            <Controls showInteractive={false} />
          </ReactFlow>
        </Box>
        {editor.phases.length === 0 && (
          <Box
            sx={{
              position: 'absolute',
              inset: 0,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              pointerEvents: 'none',
            }}
          >
            <Typography variant="body2" color="text.secondary">
              Click <strong>Add Phase</strong> to start building your workflow.
            </Typography>
          </Box>
        )}
      </Box>

      {/* Phase editor dialog — opens when a phase node is clicked */}
      <Dialog
        open={editPhaseIdx !== null}
        onClose={() => closePhaseEditor(false)}
        maxWidth="sm"
        fullWidth
      >
        {editPhaseIdx !== null && editor.phases[editPhaseIdx] && (
          <>
            <DialogTitle sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <span>Phase {editPhaseIdx + 1}</span>
              <Stack direction="row" spacing={0.5}>
                <Tooltip title="Move up">
                  <span>
                    <IconButton
                      size="small"
                      disabled={editPhaseIdx === 0}
                      onClick={() => {
                        movePhase(editPhaseIdx, -1)
                        setEditPhaseIdx(editPhaseIdx - 1)
                      }}
                    >
                      <UpIcon fontSize="small" />
                    </IconButton>
                  </span>
                </Tooltip>
                <Tooltip title="Move down">
                  <span>
                    <IconButton
                      size="small"
                      disabled={editPhaseIdx >= editor.phases.length - 1}
                      onClick={() => {
                        movePhase(editPhaseIdx, 1)
                        setEditPhaseIdx(editPhaseIdx + 1)
                      }}
                    >
                      <DownIcon fontSize="small" />
                    </IconButton>
                  </span>
                </Tooltip>
                <Tooltip title="Delete phase">
                  <IconButton
                    size="small"
                    color="error"
                    onClick={() => {
                      removePhase(editPhaseIdx)
                      setEditPhaseIdx(null)
                      setPhaseSnapshot(null)
                      setPhaseIsNew(false)
                    }}
                  >
                    <DeleteIcon fontSize="small" />
                  </IconButton>
                </Tooltip>
              </Stack>
            </DialogTitle>
            <DialogContent dividers>
              <PhaseFields
                phase={editor.phases[editPhaseIdx]}
                onChange={(patch) => updatePhase(editPhaseIdx, patch)}
                availableTools={availableTools}
              />
            </DialogContent>
            <DialogActions>
              <Button onClick={() => closePhaseEditor(false)}>Cancel</Button>
              <Button variant="contained" onClick={() => closePhaseEditor(true)}>Done</Button>
            </DialogActions>
          </>
        )}
      </Dialog>

      {/* Edge condition editor — open via double-click on an edge. */}
      <Dialog open={edgeLabelEdit !== null} onClose={() => setEdgeLabelEdit(null)} maxWidth="xs" fullWidth>
        <DialogTitle>Edge condition</DialogTitle>
        <DialogContent dividers>
          <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1.5 }}>
            Optional condition — when set, this edge is taken only if the predicate
            evaluates true against the previous phase's output. Leave blank for an
            unconditional transition. Enables branches and loops.
          </Typography>
          <TextField
            fullWidth
            autoFocus
            size="small"
            label="Condition"
            placeholder='e.g. output.severity == "high"'
            value={edgeLabelEdit?.label ?? ''}
            onChange={(e) =>
              setEdgeLabelEdit((prev) => (prev ? { ...prev, label: e.target.value } : prev))
            }
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setEdgeLabelEdit(null)}>Cancel</Button>
          <Button variant="contained" onClick={commitEdgeLabel}>Save</Button>
        </DialogActions>
      </Dialog>

      <Snackbar
        open={snackbar.open}
        autoHideDuration={4000}
        onClose={() => setSnackbar((s) => ({ ...s, open: false }))}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
      >
        <Alert
          severity={snackbar.severity}
          onClose={() => setSnackbar((s) => ({ ...s, open: false }))}
        >
          {snackbar.message}
        </Alert>
      </Snackbar>
    </Box>
  )
}

// -----------------------------------------------------------------------------
// Single-phase editor
// -----------------------------------------------------------------------------

interface PhaseFieldsProps {
  phase: WorkflowPhase
  onChange: (patch: Partial<WorkflowPhase>) => void
  availableTools: string[]
}

function PhaseFields({ phase, onChange, availableTools }: PhaseFieldsProps) {
  const allAccess = (phase.tools || []).length === 1 && phase.tools[0] === '*'
  const selectedTools = allAccess ? [] : phase.tools || []
  return (
    <Box>
      <Grid container spacing={1.5}>
          <Grid item xs={12} sm={7}>
            <TextField
              label="Phase name"
              value={phase.name}
              onChange={(e) => onChange({ name: e.target.value })}
              fullWidth
              size="small"
            />
          </Grid>
          <Grid item xs={12} sm={5}>
            <FormControl fullWidth size="small">
              <InputLabel>Agent</InputLabel>
              <Select
                label="Agent"
                value={phase.agent_id}
                onChange={(e) => onChange({ agent_id: e.target.value })}
              >
                {AGENT_OPTIONS.map((a) => (
                  <MenuItem key={a.id} value={a.id}>
                    {a.label} ({a.id})
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
          </Grid>
          <Grid item xs={12}>
            <TextField
              label="Purpose"
              value={phase.purpose || ''}
              onChange={(e) => onChange({ purpose: e.target.value })}
              fullWidth
              size="small"
            />
          </Grid>
          <Grid item xs={12}>
            <Autocomplete
              multiple
              freeSolo
              size="small"
              disabled={allAccess}
              options={availableTools}
              groupBy={(opt) => (opt.includes('_') ? opt.split('_', 1)[0] : 'other')}
              value={selectedTools}
              onChange={(_, next) => onChange({ tools: next as string[] })}
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
                  label="Tools"
                  placeholder={
                    allAccess ? 'All tools — individual picks disabled' : 'Start typing to filter…'
                  }
                  helperText={
                    allAccess
                      ? 'This phase has unrestricted tool access. Uncheck "All tool access" to pick specific tools.'
                      : availableTools.length > 0
                        ? `${availableTools.length} tools available — free text accepted if a tool isn't in the registry yet`
                        : 'Tool registry unavailable — free text accepted'
                  }
                />
              )}
            />
            <FormControlLabel
              sx={{ ml: 0.5, mt: 0.5 }}
              control={
                <Checkbox
                  size="small"
                  checked={allAccess}
                  onChange={(e) =>
                    onChange({ tools: e.target.checked ? ['*'] : [] })
                  }
                />
              }
              label={
                <Typography variant="caption" color="text.secondary">
                  All tool access (grants the agent every registered tool)
                </Typography>
              }
            />
          </Grid>
          <Grid item xs={12}>
            <TextField
              label="Steps (one per line)"
              value={(phase.steps || []).join('\n')}
              onChange={(e) =>
                onChange({
                  steps: e.target.value.split('\n').filter((s) => s !== '' || true),
                })
              }
              fullWidth
              multiline
              minRows={2}
              size="small"
            />
          </Grid>
          <Grid item xs={12} sm={6}>
            <TextField
              label="Expected output"
              value={phase.expected_output || ''}
              onChange={(e) => onChange({ expected_output: e.target.value })}
              fullWidth
              size="small"
            />
          </Grid>
          <Grid item xs={6} sm={3}>
            <TextField
              label="Timeout (s)"
              type="number"
              value={phase.timeout_seconds ?? 300}
              onChange={(e) =>
                onChange({ timeout_seconds: parseInt(e.target.value, 10) || 0 })
              }
              fullWidth
              size="small"
            />
          </Grid>
          <Grid item xs={6} sm={3}>
            <FormControlLabel
              control={
                <Checkbox
                  checked={!!phase.approval_required}
                  onChange={(e) => onChange({ approval_required: e.target.checked })}
                />
              }
              label="Approval"
            />
          </Grid>
        </Grid>
      </Box>
  )
}

// -----------------------------------------------------------------------------
// Graph builder — linear left-to-right layout
// -----------------------------------------------------------------------------

function buildGraph(phases: WorkflowPhase[]): { nodes: Node[]; edges: Edge[] } {
  const NODE_WIDTH = 280
  const HORIZONTAL_GAP = 60
  const agentLabel = (id: string) =>
    AGENT_OPTIONS.find((a) => a.id === id)?.label || id
  const timeoutLabel = (s?: number) => {
    if (!s || s <= 0) return null
    if (s < 60) return `${s}s`
    if (s < 3600) return `${Math.round(s / 60)}m`
    return `${(s / 3600).toFixed(1)}h`
  }

  const nodes: Node[] = phases.map((phase, i) => {
    const toolsArr = phase.tools || []
    const stepsArr = phase.steps || []
    const tLabel = timeoutLabel(phase.timeout_seconds ?? 300)
    return {
      id: phase.phase_id || `phase-${i + 1}`,
      position: { x: i * (NODE_WIDTH + HORIZONTAL_GAP), y: 0 },
      data: {
        label: (
          <Box sx={{ textAlign: 'left', p: 0.5 }}>
            <Box
              sx={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: 1,
                mb: 0.5,
              }}
            >
              <Typography variant="caption" sx={{ fontWeight: 700, opacity: 0.7 }}>
                PHASE {i + 1}
              </Typography>
              {phase.approval_required && (
                <Box
                  component="span"
                  sx={{
                    fontSize: '0.6rem',
                    fontWeight: 700,
                    px: 0.75,
                    py: 0.1,
                    borderRadius: 1,
                    bgcolor: 'rgba(255,152,0,0.18)',
                    color: '#ff9800',
                    border: '1px solid rgba(255,152,0,0.5)',
                    letterSpacing: 0.4,
                  }}
                >
                  APPROVAL
                </Box>
              )}
            </Box>
            <Typography
              variant="body2"
              sx={{ fontWeight: 700, mb: 0.25, lineHeight: 1.25, whiteSpace: 'normal' }}
            >
              {phase.name || '(unnamed)'}
            </Typography>
            <Typography
              variant="caption"
              color="text.secondary"
              sx={{ display: 'block', mb: phase.purpose ? 0.5 : 0 }}
            >
              {agentLabel(phase.agent_id)}
            </Typography>
            {phase.purpose && (
              <Typography
                variant="caption"
                sx={{
                  display: '-webkit-box',
                  WebkitLineClamp: 3,
                  WebkitBoxOrient: 'vertical',
                  overflow: 'hidden',
                  lineHeight: 1.35,
                  whiteSpace: 'normal',
                  mb: 0.75,
                }}
              >
                {phase.purpose}
              </Typography>
            )}
            {toolsArr.length > 0 && (
              <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.4, mb: 0.5 }}>
                {toolsArr.slice(0, 4).map((t) => (
                  <Box
                    key={t}
                    component="span"
                    sx={{
                      fontSize: '0.6rem',
                      px: 0.6,
                      py: 0.1,
                      borderRadius: 0.75,
                      bgcolor: 'rgba(255,255,255,0.08)',
                      border: '1px solid rgba(255,255,255,0.15)',
                    }}
                  >
                    {t}
                  </Box>
                ))}
                {toolsArr.length > 4 && (
                  <Typography variant="caption" sx={{ fontSize: '0.6rem', opacity: 0.6 }}>
                    +{toolsArr.length - 4}
                  </Typography>
                )}
              </Box>
            )}
            <Box sx={{ display: 'flex', gap: 1.25, flexWrap: 'wrap', mt: 0.25 }}>
              {stepsArr.filter((s) => s.trim()).length > 0 && (
                <Typography variant="caption" sx={{ fontSize: '0.65rem', opacity: 0.7 }}>
                  {stepsArr.filter((s) => s.trim()).length} step
                  {stepsArr.filter((s) => s.trim()).length === 1 ? '' : 's'}
                </Typography>
              )}
              {tLabel && (
                <Typography variant="caption" sx={{ fontSize: '0.65rem', opacity: 0.7 }}>
                  ⏱ {tLabel}
                </Typography>
              )}
              {phase.expected_output && (
                <Typography
                  variant="caption"
                  sx={{
                    fontSize: '0.65rem',
                    opacity: 0.7,
                    maxWidth: '100%',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}
                >
                  → {phase.expected_output}
                </Typography>
              )}
            </Box>
          </Box>
        ),
      },
      style: {
        width: NODE_WIDTH,
        padding: 10,
        borderRadius: 10,
        border: phase.approval_required ? '2px solid #ff9800' : '1px solid #888',
        textAlign: 'left' as const,
      },
    }
  })

  const edges: Edge[] = []
  for (let i = 0; i < phases.length - 1; i++) {
    const from = phases[i].phase_id || `phase-${i + 1}`
    const to = phases[i + 1].phase_id || `phase-${i + 2}`
    edges.push({
      id: `${from}->${to}`,
      source: from,
      target: to,
      markerEnd: { type: MarkerType.ArrowClosed },
    })
  }
  return { nodes, edges }
}
