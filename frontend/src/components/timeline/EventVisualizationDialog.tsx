/**
 * Consolidated Event Visualization Dialog - Streamlined from 6 to 4 tabs.
 * 
 * New tab structure:
 * 1. Summary - Overview + AI Analysis (side-by-side)
 * 2. Context - Entity Graph + Related Events
 * 3. Intelligence - MITRE ATT&CK + IOCs + Threat Intel
 * 4. Raw Data - JSON + Export
 */

import { useState, useEffect } from 'react'
import {
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Button,
  Box,
  Typography,
  Tabs,
  Tab,
  CircularProgress,
  Chip,
  Grid,
  Paper,
  List,
  ListItem,
  ListItemText,
  IconButton,
  Tooltip,
  Alert,
  Card,
  CardContent,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
} from '@mui/material'
import {
  Close as CloseIcon,
  ContentCopy as CopyIcon,
  FileDownload as DownloadIcon,
  Psychology as AiIcon,
  Security as SecurityIcon,
  AccountTree as GraphIcon,
  Code as CodeIcon,
} from '@mui/icons-material'
import { format } from 'date-fns'
import { timelineApi } from '../../services/api'
import EntityGraph from '../graph/EntityGraph'

interface EventVisualizationDialogProps {
  open: boolean
  onClose: () => void
  eventId: string | null
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
      id={`event-viz-tabpanel-${index}`}
      aria-labelledby={`event-viz-tab-${index}`}
      {...other}
    >
      {value === index && <Box sx={{ py: 2 }}>{children}</Box>}
    </div>
  )
}

export default function EventVisualizationDialog({
  open,
  onClose,
  eventId,
}: EventVisualizationDialogProps) {
  const [loading, setLoading] = useState(false)
  const [tabValue, setTabValue] = useState(0)
  const [vizData, setVizData] = useState<any>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (open && eventId) {
      loadVisualizationData()
    }
  }, [open, eventId])

  const loadVisualizationData = async () => {
    if (!eventId) return

    setLoading(true)
    setError(null)
    try {
      const response = await timelineApi.getEventVisualization(eventId, {
        include_ai_analysis: true,
        time_window_minutes: 60,
      })
      setVizData(response.data)
    } catch (err) {
      setError('Failed to load event visualization')
      console.error(err)
    } finally {
      setLoading(false)
    }
  }

  const handleTabChange = (_: any, newValue: number) => {
    setTabValue(newValue)
  }

  const handleCopyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text)
  }

  const handleExportReport = () => {
    if (!vizData) return
    const dataStr = JSON.stringify(vizData, null, 2)
    const dataBlob = new Blob([dataStr], { type: 'application/json' })
    const url = URL.createObjectURL(dataBlob)
    const link = document.createElement('a')
    link.href = url
    link.download = `event-${eventId}-report.json`
    link.click()
    URL.revokeObjectURL(url)
  }

  const getSeverityColor = (severity: string) => {
    switch (severity?.toLowerCase()) {
      case 'critical':
      case 'high':
        return 'error'
      case 'medium':
        return 'warning'
      case 'low':
        return 'success'
      default:
        return 'default'
    }
  }

  if (!eventId) return null

  return (
    <Dialog open={open} onClose={onClose} maxWidth="xl" fullWidth>
      <DialogTitle>
        <Box display="flex" justifyContent="space-between" alignItems="center">
          <Box display="flex" alignItems="center" gap={1}>
            <SecurityIcon color="primary" />
            <Typography variant="h6">Event Analysis</Typography>
            {vizData?.event?.severity && (
              <Chip
                label={vizData.event.severity.toUpperCase()}
                color={getSeverityColor(vizData.event.severity)}
                size="small"
              />
            )}
          </Box>
          <Box display="flex" gap={1}>
            <Tooltip title="Export Report">
              <IconButton size="small" onClick={handleExportReport} disabled={!vizData} aria-label="Export report">
                <DownloadIcon />
              </IconButton>
            </Tooltip>
            <IconButton onClick={onClose} size="small" aria-label="Close dialog">
              <CloseIcon />
            </IconButton>
          </Box>
        </Box>
      </DialogTitle>

      <DialogContent dividers>
        {loading && !vizData ? (
          <Box display="flex" justifyContent="center" alignItems="center" py={8}>
            <CircularProgress aria-label="Loading event analysis" />
            <Typography sx={{ ml: 2 }}>Loading event analysis...</Typography>
          </Box>
        ) : error ? (
          <Alert 
            severity="error"
            action={
              <Button color="inherit" size="small" onClick={loadVisualizationData}>
                Retry
              </Button>
            }
          >
            {error}
          </Alert>
        ) : vizData ? (
          <>
            <Tabs
              value={tabValue}
              onChange={handleTabChange}
              variant="scrollable"
              scrollButtons="auto"
              aria-label="Event analysis tabs"
            >
              <Tab label="Summary" icon={<SecurityIcon />} iconPosition="start" />
              <Tab label="Context" icon={<GraphIcon />} iconPosition="start" />
              <Tab label="Intelligence" icon={<AiIcon />} iconPosition="start" />
              <Tab label="Raw Data" icon={<CodeIcon />} iconPosition="start" />
            </Tabs>

            {/* Tab 1: Summary - Overview + AI Analysis side-by-side */}
            <TabPanel value={tabValue} index={0}>
              <Grid container spacing={3}>
                {/* Left: Event Overview */}
                <Grid item xs={12} md={6}>
                  <Card>
                    <CardContent>
                      <Typography variant="h6" gutterBottom>
                        Event Overview
                      </Typography>
                      <Grid container spacing={2}>
                        <Grid item xs={12}>
                          <Typography variant="body2" color="textSecondary">
                            Event ID
                          </Typography>
                          <Box display="flex" alignItems="center" gap={1}>
                            <Typography>{vizData.event.id}</Typography>
                            <Tooltip title="Copy">
                              <IconButton
                                size="small"
                                onClick={() => handleCopyToClipboard(vizData.event.id)}
                              >
                                <CopyIcon fontSize="small" />
                              </IconButton>
                            </Tooltip>
                          </Box>
                        </Grid>
                        <Grid item xs={12} sm={6}>
                          <Typography variant="body2" color="textSecondary">
                            Timestamp
                          </Typography>
                          <Typography>
                            {format(new Date(vizData.event.start), 'yyyy-MM-dd HH:mm:ss')}
                          </Typography>
                        </Grid>
                        <Grid item xs={12} sm={6}>
                          <Typography variant="body2" color="textSecondary">
                            Source
                          </Typography>
                          <Typography>{vizData.event.metadata?.data_source || vizData.event.type || 'N/A'}</Typography>
                        </Grid>
                        <Grid item xs={12}>
                          <Typography variant="body2" color="textSecondary">
                            Summary
                          </Typography>
                          <Typography>{vizData.event.content}</Typography>
                        </Grid>
                        <Grid item xs={12}>
                          <Typography variant="body2" color="textSecondary">
                            Description
                          </Typography>
                          <Typography>{vizData.event.metadata?.description || vizData.finding?.description || 'No description'}</Typography>
                        </Grid>
                      </Grid>

                      {/* Key Entities */}
                      {vizData.entities && Object.keys(vizData.entities).length > 0 && (
                        <Box mt={2}>
                          <Typography variant="subtitle2" gutterBottom>
                            Key Entities
                          </Typography>
                          <Box display="flex" flexWrap="wrap" gap={1}>
                            {Object.entries(vizData.entities).map(([type, values]: [string, any]) => (
                              <Chip
                                key={type}
                                label={`${type}: ${Array.isArray(values) ? values.length : 1}`}
                                size="small"
                                variant="outlined"
                              />
                            ))}
                          </Box>
                        </Box>
                      )}
                    </CardContent>
                  </Card>
                </Grid>

                {/* Right: AI Analysis */}
                <Grid item xs={12} md={6}>
                  <Card sx={{ bgcolor: 'primary.dark', height: '100%' }}>
                    <CardContent>
                      <Box display="flex" alignItems="center" gap={1} mb={2}>
                        <AiIcon sx={{ color: 'white' }} />
                        <Typography variant="h6" color="white">
                          AI Analysis
                        </Typography>
                      </Box>
                      {vizData.ai_analysis ? (
                        <>
                          <Typography variant="body2" color="white" paragraph>
                            <strong>Risk Assessment:</strong> {vizData.ai_analysis.risk_level || 'Analyzing...'}
                          </Typography>
                          <Typography variant="body2" color="white" paragraph>
                            <strong>Analysis:</strong>
                          </Typography>
                          <Typography variant="body2" color="white" sx={{ whiteSpace: 'pre-wrap' }}>
                            {vizData.ai_analysis.summary || 'No AI analysis available'}
                          </Typography>
                          {vizData.ai_analysis.recommendations && (
                            <Box mt={2}>
                              <Typography variant="body2" color="white" gutterBottom>
                                <strong>Recommendations:</strong>
                              </Typography>
                              <List dense>
                                {vizData.ai_analysis.recommendations.map((rec: string, index: number) => (
                                  <ListItem key={index} sx={{ color: 'white' }}>
                                    <ListItemText primary={`• ${rec}`} />
                                  </ListItem>
                                ))}
                              </List>
                            </Box>
                          )}
                        </>
                      ) : (
                        <Typography variant="body2" color="white">
                          AI analysis not available for this event
                        </Typography>
                      )}
                    </CardContent>
                  </Card>
                </Grid>
              </Grid>
            </TabPanel>

            {/* Tab 2: Context - Entity Graph + Related Events */}
            <TabPanel value={tabValue} index={1}>
              <Grid container spacing={3}>
                <Grid item xs={12} md={6}>
                  <Paper sx={{ p: 2, overflow: 'hidden' }}>
                    <Typography variant="h6" gutterBottom>
                      Entity Relationships
                    </Typography>
                    {vizData.entity_graph && vizData.entity_graph.nodes?.length > 0 ? (
                      <Box sx={{ height: 400, overflow: 'hidden', position: 'relative' }}>
                        <EntityGraph 
                          nodes={vizData.entity_graph.nodes || []} 
                          links={vizData.entity_graph.links || []} 
                          height={400}
                          showControls={false}
                        />
                      </Box>
                    ) : (
                      <Typography color="textSecondary">No entity graph available</Typography>
                    )}
                  </Paper>
                </Grid>
                <Grid item xs={12} md={6}>
                  <Paper sx={{ p: 2 }}>
                    <Typography variant="h6" gutterBottom>
                      Related Events ({vizData.related_events?.length || 0})
                    </Typography>
                    {vizData.related_events && vizData.related_events.length > 0 ? (
                      <Box sx={{ height: 400, overflow: 'auto' }}>
                        <List dense>
                          {vizData.related_events.map((event: any) => (
                            <ListItem key={event.id || event.content} divider>
                              <ListItemText
                                primary={event.content}
                                secondary={
                                  <>
                                    {format(new Date(event.start), 'HH:mm:ss')}
                                    {event.severity && (
                                      <Chip
                                        label={event.severity}
                                        size="small"
                                        color={getSeverityColor(event.severity)}
                                        sx={{ ml: 1 }}
                                      />
                                    )}
                                  </>
                                }
                              />
                            </ListItem>
                          ))}
                        </List>
                      </Box>
                    ) : (
                      <Typography color="textSecondary">No related events found</Typography>
                    )}
                  </Paper>
                </Grid>
              </Grid>
            </TabPanel>

            {/* Tab 3: Intelligence - MITRE + IOCs + Threat Intel */}
            <TabPanel value={tabValue} index={2}>
              <Grid container spacing={3}>
                {/* MITRE ATT&CK */}
                <Grid item xs={12} md={6}>
                  <Paper sx={{ p: 2 }}>
                    <Typography variant="h6" gutterBottom>
                      MITRE ATT&CK Mapping
                    </Typography>
                    {vizData.mitre_techniques && vizData.mitre_techniques.length > 0 ? (
                      <Box>
                        <Typography variant="subtitle2" gutterBottom>
                          Techniques
                        </Typography>
                        <List dense>
                          {vizData.mitre_techniques.map((technique: any) => (
                            <ListItem key={technique.technique_id || technique.name}>
                              <ListItemText
                                primary={technique.name || technique.technique_id}
                                secondary={
                                  <>
                                    {technique.technique_id}
                                    {technique.confidence > 0 && ` - ${Math.round(technique.confidence * 100)}% confidence`}
                                  </>
                                }
                              />
                            </ListItem>
                          ))}
                        </List>
                      </Box>
                    ) : (
                      <Typography color="textSecondary">No MITRE ATT&CK mapping available</Typography>
                    )}
                  </Paper>
                </Grid>

                {/* IOCs and Threat Intel */}
                <Grid item xs={12} md={6}>
                  <Paper sx={{ p: 2 }}>
                    <Typography variant="h6" gutterBottom>
                      Indicators of Compromise
                    </Typography>
                    {vizData.iocs && vizData.iocs.length > 0 ? (
                      <TableContainer>
                        <Table size="small">
                          <TableHead>
                            <TableRow>
                              <TableCell>Type</TableCell>
                              <TableCell>Value</TableCell>
                            </TableRow>
                          </TableHead>
                          <TableBody>
                            {vizData.iocs.map((ioc: any, index: number) => (
                              <TableRow key={index}>
                                <TableCell>{ioc.type}</TableCell>
                                <TableCell>
                                  <Box display="flex" alignItems="center" gap={1}>
                                    <Typography variant="body2" noWrap>
                                      {ioc.value}
                                    </Typography>
                                    <Tooltip title="Copy">
                                      <IconButton
                                        size="small"
                                        onClick={() => handleCopyToClipboard(ioc.value)}
                                      >
                                        <CopyIcon fontSize="small" />
                                      </IconButton>
                                    </Tooltip>
                                  </Box>
                                </TableCell>
                              </TableRow>
                            ))}
                          </TableBody>
                        </Table>
                      </TableContainer>
                    ) : (
                      <Typography color="textSecondary">No IOCs identified</Typography>
                    )}
                  </Paper>
                </Grid>
              </Grid>
            </TabPanel>

            {/* Tab 4: Raw Data */}
            <TabPanel value={tabValue} index={3}>
              <Paper sx={{ p: 2, bgcolor: '#1e1e1e' }}>
                <Box display="flex" justifyContent="space-between" alignItems="center" mb={2}>
                  <Typography variant="h6" color="white">
                    Raw Event Data
                  </Typography>
                  <Button
                    startIcon={<DownloadIcon />}
                    onClick={handleExportReport}
                    variant="outlined"
                    size="small"
                  >
                    Export JSON
                  </Button>
                </Box>
                <Box
                  component="pre"
                  sx={{
                    color: '#d4d4d4',
                    maxHeight: '500px',
                    overflow: 'auto',
                    fontSize: '0.85rem',
                    fontFamily: 'monospace',
                  }}
                >
                  {JSON.stringify(vizData, null, 2)}
                </Box>
              </Paper>
            </TabPanel>
          </>
        ) : null}
      </DialogContent>

      <DialogActions>
        <Button onClick={onClose}>Close</Button>
      </DialogActions>
    </Dialog>
  )
}

