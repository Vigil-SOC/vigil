/**
 * Consolidated Case Detail Dialog - Streamlined from 13 to 5 tabs.
 * 
 * New tab structure:
 * 1. Overview - Case info + Findings + Activities
 * 2. Investigation - Timeline + Entity Graph + Evidence
 * 3. Resolution - Tasks + Resolution Steps + SLA
 * 4. Collaboration - Comments + Watchers
 * 5. Details - IOCs + Relationships + Audit Log
 */

import { useState, useEffect } from 'react'
import {
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Button,
  TextField,
  Select,
  MenuItem,
  FormControl,
  InputLabel,
  Grid,
  Typography,
  Chip,
  Box,
  IconButton,
  List,
  ListItem,
  ListItemText,
  Snackbar,
  Alert,
  Tabs,
  Tab,
  Paper,
  Card,
  CardContent,
  Accordion,
  AccordionSummary,
  AccordionDetails,
} from '@mui/material'
import {
  Close as CloseIcon,
  Save as SaveIcon,
  Upload as ExportIcon,
  PictureAsPdf as PdfIcon,
  ExpandMore as ExpandMoreIcon,
  CheckCircle as CheckIcon,
  MergeType as MergeIcon,
} from '@mui/icons-material'
import { casesApi, findingsApi, timelineApi, graphApi } from '../../services/api'
import ExportToTimesketchDialog from '../timesketch/ExportToTimesketchDialog'
import JiraExportDialog from '../jira/JiraExportDialog'
import EventTimeline from '../timeline/EventTimeline'
import EntityVisualization from '../graph/EntityVisualization'
import CaseComments from './CaseComments'
import CaseEvidence from './CaseEvidence'
import CaseIOCs from './CaseIOCs'
import CaseTasks from './CaseTasks'
import CaseSLA from './CaseSLA'
import CaseRelationships from './CaseRelationships'
import CaseAuditLog from './CaseAuditLog'
import CaseWatchers from './CaseWatchers'

interface CaseDetailDialogProps {
  open: boolean
  onClose: () => void
  caseId: string | null
  onUpdate?: () => void
}

interface TabPanelProps {
  children?: React.ReactNode
  index: number
  value: number
}

function TabPanel(props: TabPanelProps) {
  const { children, value, index, ...other } = props

  return (
    <div
      role="tabpanel"
      hidden={value !== index}
      id={`case-tabpanel-${index}`}
      aria-labelledby={`case-tab-${index}`}
      {...other}
    >
      {value === index && <Box sx={{ pt: 2 }}>{children}</Box>}
    </div>
  )
}

export default function CaseDetailDialog({
  open,
  onClose,
  caseId,
  onUpdate,
}: CaseDetailDialogProps) {
  const [loading, setLoading] = useState(false)
  const [tabValue, setTabValue] = useState(0)
  const [caseData, setCaseData] = useState<any>(null)
  const [findings, setFindings] = useState<any[]>([])
  const [activities, setActivities] = useState<any[]>([])
  const [resolutionSteps, setResolutionSteps] = useState<any[]>([])
  const [timelineEvents, setTimelineEvents] = useState<any[]>([])
  const [graphData, setGraphData] = useState<{ nodes: any[]; links: any[] }>({ nodes: [], links: [] })
  const [exportDialogOpen, setExportDialogOpen] = useState(false)
  const [jiraExportOpen, setJiraExportOpen] = useState(false)
  const [editMode, setEditMode] = useState(false)
  const [editedCase, setEditedCase] = useState<any>(null)
  const [mergeDialogOpen, setMergeDialogOpen] = useState(false)
  const [allCases, setAllCases] = useState<any[]>([])
  const [selectedMergeCase, setSelectedMergeCase] = useState<string>('')
  const [merging, setMerging] = useState(false)
  const [snackbar, setSnackbar] = useState<{
    open: boolean
    message: string
    severity: 'success' | 'error'
  }>({
    open: false,
    message: '',
    severity: 'success',
  })

  useEffect(() => {
    if (open && caseId) {
      loadCase()
    }
  }, [open, caseId])

  const loadCase = async () => {
    if (!caseId) return

    setLoading(true)
    try {
      const response = await casesApi.getById(caseId)
      setCaseData(response.data)
      setEditedCase(response.data)
      
      // Load associated findings
      const findingIds = response.data.finding_ids || []
      const findingsData = await Promise.all(
        findingIds.map(async (id: string) => {
          try {
            const resp = await findingsApi.getById(id)
            return resp.data
          } catch (error) {
            return null
          }
        })
      )
      setFindings(findingsData.filter((f) => f !== null))

      // activities and resolution_steps are top-level fields on the case
      // payload (see Case.to_dict in database/models.py) — there is no
      // `metadata` wrapper, so reading metadata?.* always yielded [].
      setActivities(response.data.activities || [])
      setResolutionSteps(response.data.resolution_steps || [])
    } catch (error) {
      console.error('Failed to load case:', error)
      setSnackbar({
        open: true,
        message: 'Failed to load case',
        severity: 'error',
      })
    } finally {
      setLoading(false)
    }
  }

  const loadTimeline = async () => {
    if (!caseId) return
    try {
      const response = await timelineApi.getCaseTimeline(caseId)
      setTimelineEvents(response.data.events || [])
    } catch (error) {
      console.error('Failed to load timeline:', error)
    }
  }

  const loadGraph = async () => {
    if (!caseId) return
    try {
      const response = await graphApi.getEntityGraph({ case_id: caseId })
      setGraphData(response.data)
    } catch (error) {
      console.error('Failed to load graph:', error)
    }
  }

  const handleSave = async () => {
    if (!caseId || !editedCase) return

    try {
      await casesApi.update(caseId, {
        title: editedCase.title,
        description: editedCase.description,
        status: editedCase.status,
        priority: editedCase.priority,
        assignee: editedCase.assignee,
      })
      
      setSnackbar({
        open: true,
        message: 'Case updated successfully',
        severity: 'success',
      })
      setEditMode(false)
      onUpdate?.()
      await loadCase()
    } catch (error) {
      setSnackbar({
        open: true,
        message: 'Failed to update case',
        severity: 'error',
      })
    }
  }

  const handleTabChange = (_: any, newValue: number) => {
    setTabValue(newValue)
    // Load data when switching to Investigation tab
    if (newValue === 1 && timelineEvents.length === 0) {
      loadTimeline()
      loadGraph()
    }
  }

  const handleOpenMerge = async () => {
    try {
      const resp = await casesApi.getAll()
      const cases = (resp.data.cases || []).filter((c: any) => c.case_id !== caseId && c.status !== 'closed')
      setAllCases(cases)
      setSelectedMergeCase('')
      setMergeDialogOpen(true)
    } catch {
      setSnackbar({ open: true, message: 'Failed to load cases', severity: 'error' })
    }
  }

  const handleMerge = async () => {
    if (!caseId || !selectedMergeCase) return
    setMerging(true)
    try {
      await casesApi.merge(caseId, selectedMergeCase)
      setSnackbar({ open: true, message: 'Cases merged successfully', severity: 'success' })
      setMergeDialogOpen(false)
      onUpdate?.()
      await loadCase()
    } catch (error: any) {
      setSnackbar({
        open: true,
        message: error?.response?.data?.detail || 'Failed to merge cases',
        severity: 'error',
      })
    } finally {
      setMerging(false)
    }
  }

  if (!caseData && !loading) return null

  const displayCase = editMode ? editedCase : caseData

  // Calculate key metrics for Overview
  const criticalFindings = findings.filter(f => f.severity === 'critical').length
  const highFindings = findings.filter(f => f.severity === 'high').length
  const completedTasks = activities.filter((a: any) => a.type === 'task_completed').length
  const totalTasks = activities.filter((a: any) => a.type?.includes('task')).length

  return (
    <Dialog
      open={open}
      onClose={onClose}
      maxWidth="xl"
      fullWidth
      PaperProps={{
        sx: { height: '90vh' }
      }}
    >
      <DialogTitle>
        <Box display="flex" justifyContent="space-between" alignItems="center">
          <Box>
            <Typography variant="h6">
              {displayCase?.title || 'Case Details'}
            </Typography>
            <Box display="flex" gap={1} mt={1}>
              <Chip 
                label={displayCase?.status || 'unknown'} 
                size="small" 
                color={displayCase?.status === 'closed' ? 'success' : 'primary'}
              />
              <Chip 
                label={displayCase?.priority || 'medium'} 
                size="small" 
                color={
                  displayCase?.priority === 'critical' ? 'error' :
                  displayCase?.priority === 'high' ? 'warning' : 'default'
                }
              />
            </Box>
          </Box>
          <Box display="flex" gap={1}>
            {editMode ? (
              <>
                <Button onClick={() => setEditMode(false)} size="small">Cancel</Button>
                <Button onClick={handleSave} variant="contained" size="small" startIcon={<SaveIcon />}>
                  Save
                </Button>
              </>
            ) : (
              <>
                <Button onClick={() => setEditMode(true)} size="small">Edit</Button>
                <Button onClick={handleOpenMerge} startIcon={<MergeIcon />} size="small">
                  Merge
                </Button>
                <Button onClick={() => setExportDialogOpen(true)} startIcon={<PdfIcon />} size="small">
                  Timesketch
                </Button>
                <IconButton onClick={() => setJiraExportOpen(true)} size="small" color="primary">
                  <ExportIcon />
                </IconButton>
                <IconButton onClick={onClose} size="small">
                  <CloseIcon />
                </IconButton>
              </>
            )}
          </Box>
        </Box>
      </DialogTitle>

      <Box sx={{ borderBottom: 1, borderColor: 'divider', px: 3 }}>
        <Tabs value={tabValue} onChange={handleTabChange} variant="scrollable" scrollButtons="auto">
          <Tab label="Overview" />
          <Tab label="Investigation" />
          <Tab label="Resolution" />
          <Tab label="Collaboration" />
          <Tab label="Details" />
        </Tabs>
      </Box>

      <DialogContent dividers sx={{ p: 3 }}>
        {/* Tab 1: Overview - Case info + Findings + Activities */}
        <TabPanel value={tabValue} index={0}>
          <Grid container spacing={3}>
            {/* Key Metrics */}
            <Grid item xs={12}>
              <Grid container spacing={2}>
                <Grid item xs={12} sm={3}>
                  <Card>
                    <CardContent>
                      <Typography color="text.secondary" gutterBottom variant="body2">
                        Total Findings
                      </Typography>
                      <Typography variant="h4">{findings.length}</Typography>
                    </CardContent>
                  </Card>
                </Grid>
                <Grid item xs={12} sm={3}>
                  <Card sx={{ bgcolor: 'error.dark' }}>
                    <CardContent>
                      <Typography color="white" gutterBottom variant="body2">
                        Critical
                      </Typography>
                      <Typography variant="h4" color="white">{criticalFindings}</Typography>
                    </CardContent>
                  </Card>
                </Grid>
                <Grid item xs={12} sm={3}>
                  <Card sx={{ bgcolor: 'warning.dark' }}>
                    <CardContent>
                      <Typography color="white" gutterBottom variant="body2">
                        High Priority
                      </Typography>
                      <Typography variant="h4" color="white">{highFindings}</Typography>
                    </CardContent>
                  </Card>
                </Grid>
                <Grid item xs={12} sm={3}>
                  <Card>
                    <CardContent>
                      <Typography color="text.secondary" gutterBottom variant="body2">
                        Tasks Progress
                      </Typography>
                      <Typography variant="h4">{completedTasks}/{totalTasks}</Typography>
                    </CardContent>
                  </Card>
                </Grid>
              </Grid>
            </Grid>

            {/* Case Information */}
            <Grid item xs={12} md={6}>
              <Paper sx={{ p: 2 }}>
                <Typography variant="h6" gutterBottom>Case Information</Typography>
                <Grid container spacing={2}>
                  <Grid item xs={12}>
                    {editMode ? (
                      <TextField
                        fullWidth
                        label="Title"
                        value={editedCase?.title || ''}
                        onChange={(e) => setEditedCase({ ...editedCase, title: e.target.value })}
                      />
                    ) : (
                      <>
                        <Typography variant="subtitle2" color="text.secondary">Title</Typography>
                        <Typography>{displayCase?.title}</Typography>
                      </>
                    )}
                  </Grid>
                  <Grid item xs={6}>
                    {editMode ? (
                      <FormControl fullWidth>
                        <InputLabel>Status</InputLabel>
                        <Select
                          value={editedCase?.status || ''}
                          onChange={(e) => setEditedCase({ ...editedCase, status: e.target.value })}
                        >
                          <MenuItem value="open">Open</MenuItem>
                          <MenuItem value="investigating">Investigating</MenuItem>
                          <MenuItem value="resolved">Resolved</MenuItem>
                          <MenuItem value="closed">Closed</MenuItem>
                        </Select>
                      </FormControl>
                    ) : (
                      <>
                        <Typography variant="subtitle2" color="text.secondary">Status</Typography>
                        <Typography>{displayCase?.status}</Typography>
                      </>
                    )}
                  </Grid>
                  <Grid item xs={6}>
                    {editMode ? (
                      <FormControl fullWidth>
                        <InputLabel>Priority</InputLabel>
                        <Select
                          value={editedCase?.priority || ''}
                          onChange={(e) => setEditedCase({ ...editedCase, priority: e.target.value })}
                        >
                          <MenuItem value="low">Low</MenuItem>
                          <MenuItem value="medium">Medium</MenuItem>
                          <MenuItem value="high">High</MenuItem>
                          <MenuItem value="critical">Critical</MenuItem>
                        </Select>
                      </FormControl>
                    ) : (
                      <>
                        <Typography variant="subtitle2" color="text.secondary">Priority</Typography>
                        <Typography>{displayCase?.priority}</Typography>
                      </>
                    )}
                  </Grid>
                  <Grid item xs={12}>
                    {editMode ? (
                      <TextField
                        fullWidth
                        label="Assignee"
                        value={editedCase?.assignee || ''}
                        onChange={(e) => setEditedCase({ ...editedCase, assignee: e.target.value })}
                      />
                    ) : (
                      <>
                        <Typography variant="subtitle2" color="text.secondary">Assignee</Typography>
                        <Typography>{displayCase?.assignee || 'Unassigned'}</Typography>
                      </>
                    )}
                  </Grid>
                  <Grid item xs={12}>
                    {editMode ? (
                      <TextField
                        fullWidth
                        multiline
                        rows={3}
                        label="Description"
                        value={editedCase?.description || ''}
                        onChange={(e) => setEditedCase({ ...editedCase, description: e.target.value })}
                      />
                    ) : (
                      <>
                        <Typography variant="subtitle2" color="text.secondary">Description</Typography>
                        <Typography>{displayCase?.description || 'No description'}</Typography>
                      </>
                    )}
                  </Grid>
                </Grid>
              </Paper>
            </Grid>

            {/* Findings Summary */}
            <Grid item xs={12} md={6}>
              <Paper sx={{ p: 2 }}>
                <Typography variant="h6" gutterBottom>
                  Findings ({findings.length})
                </Typography>
                <List dense sx={{ maxHeight: 300, overflow: 'auto' }}>
                  {findings.slice(0, 10).map((finding) => (
                    <ListItem key={finding.finding_id}>
                      <Chip
                        label={finding.severity.toUpperCase()}
                        size="small"
                        color={
                          finding.severity === 'critical' ? 'error' :
                          finding.severity === 'high' ? 'warning' : 'default'
                        }
                        sx={{ mr: 1 }}
                      />
                      <ListItemText
                        primary={finding.title}
                        secondary={new Date(finding.timestamp).toLocaleString()}
                      />
                    </ListItem>
                  ))}
                  {findings.length > 10 && (
                    <ListItem>
                      <ListItemText secondary={`... and ${findings.length - 10} more`} />
                    </ListItem>
                  )}
                </List>
              </Paper>
            </Grid>

            {/* Recent Activities */}
            <Grid item xs={12}>
              <Paper sx={{ p: 2 }}>
                <Typography variant="h6" gutterBottom>
                  Recent Activities ({activities.length})
                </Typography>
                <List dense sx={{ maxHeight: 200, overflow: 'auto' }}>
                  {activities.slice(0, 10).map((activity: any, index: number) => (
                    <ListItem key={index}>
                      <ListItemText
                        primary={activity.description}
                        secondary={`${activity.activity_type} - ${new Date(activity.timestamp).toLocaleString()}`}
                      />
                    </ListItem>
                  ))}
                </List>
              </Paper>
            </Grid>
          </Grid>
        </TabPanel>

        {/* Tab 2: Investigation - Timeline + Graph + Evidence */}
        <TabPanel value={tabValue} index={1}>
          <Grid container spacing={3}>
            <Grid item xs={12} md={6}>
              <Paper sx={{ p: 2 }}>
                <Typography variant="h6" gutterBottom>Timeline</Typography>
                {timelineEvents.length > 0 ? (
                  <Box sx={{ height: 400 }}>
                    <EventTimeline events={timelineEvents} />
                  </Box>
                ) : (
                  <Typography color="text.secondary">No timeline events</Typography>
                )}
              </Paper>
            </Grid>
            <Grid item xs={12} md={6}>
              <Paper sx={{ p: 2, overflow: 'hidden' }}>
                <Typography variant="h6" gutterBottom>Entity Graph</Typography>
                <Box sx={{ height: 400, width: '100%', overflow: 'hidden', position: 'relative' }}>
                  <EntityVisualization
                    nodes={graphData.nodes}
                    links={graphData.links}
                    height={400}
                    showControls={false}
                    vstrikeFindings={findings}
                    emptyState={
                      <Typography color="text.secondary">
                        No entities to display
                      </Typography>
                    }
                  />
                </Box>
              </Paper>
            </Grid>
            <Grid item xs={12}>
              {caseId && <CaseEvidence caseId={caseId} />}
            </Grid>
          </Grid>
        </TabPanel>

        {/* Tab 3: Resolution - Tasks + Steps + SLA */}
        <TabPanel value={tabValue} index={2}>
          <Grid container spacing={3}>
            <Grid item xs={12} md={8}>
              <Paper sx={{ p: 2, mb: 2 }}>
                <Typography variant="h6" gutterBottom>Resolution Steps</Typography>
                <List>
                  {resolutionSteps.map((step: any, index: number) => (
                    <ListItem key={index}>
                      <CheckIcon color="success" sx={{ mr: 2 }} />
                      <ListItemText
                        primary={step.description}
                        secondary={`Action: ${step.action_taken} | Result: ${step.result || 'Pending'}`}
                      />
                    </ListItem>
                  ))}
                  {resolutionSteps.length === 0 && (
                    <ListItem>
                      <ListItemText secondary="No resolution steps yet" />
                    </ListItem>
                  )}
                </List>
              </Paper>
              {caseId && <CaseTasks caseId={caseId} />}
            </Grid>
            <Grid item xs={12} md={4}>
              {caseId && <CaseSLA caseId={caseId} />}
            </Grid>
          </Grid>
        </TabPanel>

        {/* Tab 4: Collaboration - Comments + Watchers */}
        <TabPanel value={tabValue} index={3}>
          <Grid container spacing={3}>
            <Grid item xs={12} md={8}>
              {caseId && <CaseComments caseId={caseId} />}
            </Grid>
            <Grid item xs={12} md={4}>
              {caseId && <CaseWatchers caseId={caseId} />}
            </Grid>
          </Grid>
        </TabPanel>

        {/* Tab 5: Details - IOCs + Relationships + Audit */}
        <TabPanel value={tabValue} index={4}>
          <Grid container spacing={2}>
            <Grid item xs={12}>
              <Accordion>
                <AccordionSummary expandIcon={<ExpandMoreIcon />}>
                  <Typography variant="h6">Indicators of Compromise (IOCs)</Typography>
                </AccordionSummary>
                <AccordionDetails>
                  {caseId && <CaseIOCs caseId={caseId} />}
                </AccordionDetails>
              </Accordion>
            </Grid>
            <Grid item xs={12}>
              <Accordion>
                <AccordionSummary expandIcon={<ExpandMoreIcon />}>
                  <Typography variant="h6">Related Cases</Typography>
                </AccordionSummary>
                <AccordionDetails>
                  {caseId && <CaseRelationships caseId={caseId} />}
                </AccordionDetails>
              </Accordion>
            </Grid>
            <Grid item xs={12}>
              <Accordion>
                <AccordionSummary expandIcon={<ExpandMoreIcon />}>
                  <Typography variant="h6">Audit Log</Typography>
                </AccordionSummary>
                <AccordionDetails>
                  {caseId && <CaseAuditLog caseId={caseId} />}
                </AccordionDetails>
              </Accordion>
            </Grid>
          </Grid>
        </TabPanel>
      </DialogContent>

      <DialogActions>
        <Button onClick={onClose}>Close</Button>
      </DialogActions>

      {/* Export Dialogs */}
      {caseId && (
        <>
          <ExportToTimesketchDialog
            open={exportDialogOpen}
            onClose={() => setExportDialogOpen(false)}
            caseId={caseId}
          />
          {caseData && (
            <JiraExportDialog
              open={jiraExportOpen}
              onClose={() => setJiraExportOpen(false)}
              caseId={caseId}
              caseTitle={caseData.title}
            />
          )}
        </>
      )}

      {/* Merge Dialog */}
      <Dialog open={mergeDialogOpen} onClose={() => setMergeDialogOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>Merge Another Case Into This One</DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            All findings, timeline entries, IOCs, evidence, tasks, and comments from the
            selected case will be moved into <strong>{caseData?.title}</strong>. The source
            case will be closed.
          </Typography>
          <FormControl fullWidth sx={{ mt: 1 }}>
            <InputLabel>Source Case</InputLabel>
            <Select
              value={selectedMergeCase}
              onChange={(e) => setSelectedMergeCase(e.target.value as string)}
              label="Source Case"
            >
              {allCases.map((c) => (
                <MenuItem key={c.case_id} value={c.case_id}>
                  {c.case_id} &mdash; {c.title} ({c.status})
                </MenuItem>
              ))}
            </Select>
          </FormControl>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setMergeDialogOpen(false)}>Cancel</Button>
          <Button
            onClick={handleMerge}
            variant="contained"
            color="warning"
            disabled={!selectedMergeCase || merging}
          >
            {merging ? 'Merging...' : 'Merge'}
          </Button>
        </DialogActions>
      </Dialog>

      {/* Snackbar */}
      <Snackbar
        open={snackbar.open}
        autoHideDuration={6000}
        onClose={() => setSnackbar({ ...snackbar, open: false })}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
      >
        <Alert
          onClose={() => setSnackbar({ ...snackbar, open: false })}
          severity={snackbar.severity}
        >
          {snackbar.message}
        </Alert>
      </Snackbar>
    </Dialog>
  )
}

