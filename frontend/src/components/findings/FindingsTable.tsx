import { useEffect, useState, useMemo, useRef } from 'react'
import {
  Box,
  Chip,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  CircularProgress,
  Typography,
  IconButton,
  Menu,
  MenuItem,
  ListItemIcon,
  ListItemText,
  TablePagination,
  TableSortLabel,
  Tooltip,
  alpha,
  useTheme,
} from '@mui/material'
import {
  Visibility as ViewIcon,
  Psychology as InvestigateIcon,
  PlayCircleOutline as PlaybookIcon,
} from '@mui/icons-material'
import { findingsApi, agentsApi, workflowApi } from '../../services/api'
import FindingDetailDialog from './FindingDetailDialog'
import { notificationService } from '../../services/notifications'
import { SeverityChip } from '../ui'

interface FindingsTableProps {
  filters?: any
  searchQuery?: string
  limit?: number
  refreshKey?: number
  onInvestigate?: (findingId: string, agentId: string, prompt: string, title: string) => void
}

interface Agent {
  id: string
  name: string
  icon: string
}

type ColumnKey = 'id' | 'severity' | 'tactic' | 'source' | 'time' | 'score' | 'actions'

export default function FindingsTable({ filters = {}, searchQuery = '', limit, refreshKey = 0, onInvestigate }: FindingsTableProps) {
  const [findings, setFindings] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [selectedFindingId, setSelectedFindingId] = useState<string | null>(null)
  const [dialogOpen, setDialogOpen] = useState(false)
  const [agents, setAgents] = useState<Agent[]>([])
  const [anchorEl, setAnchorEl] = useState<null | HTMLElement>(null)
  const [selectedFinding, setSelectedFinding] = useState<any>(null)
  // Workflow (playbook) run picker — separate anchor from the agent menu.
  const [workflows, setWorkflows] = useState<{ id: string; name: string; description?: string }[]>([])
  const [workflowAnchorEl, setWorkflowAnchorEl] = useState<null | HTMLElement>(null)
  const [workflowFinding, setWorkflowFinding] = useState<any>(null)
  const [runningWorkflowId, setRunningWorkflowId] = useState<string | null>(null)
  const prevFindingsRef = useRef<Set<string>>(new Set())
  const [page, setPage] = useState(0)
  const [rowsPerPage, setRowsPerPage] = useState(10)
  const [orderBy, setOrderBy] = useState<string>('timestamp')
  const [order, setOrder] = useState<'asc' | 'desc'>('desc')
  const [hoveredRow, setHoveredRow] = useState<string | null>(null)
  const theme = useTheme()

  // Column resizing state
  const [columnWidths, setColumnWidths] = useState<Record<ColumnKey, number>>({
    id: 140,
    severity: 120,
    tactic: 160,
    source: 100,
    time: 200,
    score: 80,
    actions: 110,
  })
  
  const resizeState = useRef<{
    column: ColumnKey | null
    startX: number
    startWidth: number
  }>({
    column: null,
    startX: 0,
    startWidth: 0,
  })

  const stableFilters = useMemo(() => filters, [JSON.stringify(filters)])

  useEffect(() => {
    loadFindings()
  }, [stableFilters, searchQuery, limit, refreshKey])

  useEffect(() => {
    agentsApi.listAgents()
      .then(res => setAgents(res.data.agents || []))
      .catch(() => {})
    // Workflows are playbooks — keep the list cached alongside agents so
    // the "Run Playbook" menu opens without a network roundtrip on click.
    workflowApi.listAll()
      .then(res => setWorkflows(res.data?.workflows || []))
      .catch(() => setWorkflows([]))
  }, [])

  const loadFindings = async () => {
    try {
      setLoading(true)
      const params: any = { ...stableFilters, limit }
      Object.keys(params).forEach(key => {
        if (!params[key]) delete params[key]
      })
      
      const response = await findingsApi.getAll(params)
      let newFindings = response.data.findings || []
      
      if (searchQuery.trim()) {
        const query = searchQuery.toLowerCase()
        newFindings = newFindings.filter((f: any) => 
          f.finding_id?.toLowerCase().includes(query) ||
          f.data_source?.toLowerCase().includes(query) ||
          f.description?.toLowerCase().includes(query) ||
          f.title?.toLowerCase().includes(query)
        )
      }
      
      if (prevFindingsRef.current.size > 0) {
        newFindings.forEach((f: any) => {
          const isNew = !prevFindingsRef.current.has(f.finding_id)
          const isHighSeverity = ['critical', 'high'].includes(f.severity?.toLowerCase())
          if (isNew && isHighSeverity) {
            notificationService.notifyNewFinding({
              finding_id: f.finding_id,
              title: f.title,
              severity: f.severity,
              description: f.description,
            })
          }
        })
      }
      
      prevFindingsRef.current = new Set(newFindings.map((f: any) => f.finding_id))
      setFindings(newFindings)
    } catch (error) {
      console.error('Failed to load findings:', error)
    } finally {
      setLoading(false)
    }
  }

  const handleViewFinding = (findingId: string) => {
    setSelectedFindingId(findingId)
    setDialogOpen(true)
  }

  const handleInvestigateClick = (event: React.MouseEvent<HTMLElement>, finding: any) => {
    event.stopPropagation()
    setAnchorEl(event.currentTarget)
    setSelectedFinding(finding)
  }

  const handleCloseMenu = () => {
    setAnchorEl(null)
    setSelectedFinding(null)
  }

  const handleAgentSelect = async (agentId: string) => {
    if (!selectedFinding || !onInvestigate) {
      handleCloseMenu()
      return
    }
    try {
      const response = await agentsApi.startInvestigation({
        finding_id: selectedFinding.finding_id,
        agent_id: agentId,
      })
      onInvestigate(
        selectedFinding.finding_id,
        agentId,
        response.data.prompt,
        `Investigation: ${selectedFinding.finding_id.substring(0, 8)}...`
      )
    } catch (error) {
      console.error('Failed to start investigation:', error)
    } finally {
      handleCloseMenu()
    }
  }

  // Playbook run — separate menu from the agent picker so the two
  // actions stay conceptually distinct. A workflow is run as a playbook
  // against the finding, not as a chat conversation.
  const handleRunPlaybookClick = (event: React.MouseEvent<HTMLElement>, finding: any) => {
    event.stopPropagation()
    setWorkflowAnchorEl(event.currentTarget)
    setWorkflowFinding(finding)
  }

  const handleCloseWorkflowMenu = () => {
    setWorkflowAnchorEl(null)
    setWorkflowFinding(null)
  }

  const handleWorkflowSelect = async (workflowId: string) => {
    if (!workflowFinding) {
      handleCloseWorkflowMenu()
      return
    }
    const findingId = workflowFinding.finding_id
    setRunningWorkflowId(workflowId)
    handleCloseWorkflowMenu()
    try {
      const res = await workflowApi.execute(workflowId, { finding_id: findingId })
      const body = res?.data || {}
      if (body.success === false) {
        notificationService.notifyGeneric(
          'Playbook finished with errors',
          `${body.workflow?.name || workflowId}: ${body.error || 'unknown error'}`,
          { severity: 'error' }
        )
      } else {
        notificationService.notifyGeneric(
          'Playbook executed',
          `${body.workflow?.name || workflowId} ran against ${findingId.substring(0, 12)}…`,
          { severity: 'success' }
        )
      }
    } catch (e: any) {
      notificationService.notifyGeneric(
        'Playbook execution failed',
        e?.response?.data?.detail || e?.message || 'unknown error',
        { severity: 'error' }
      )
    } finally {
      setRunningWorkflowId(null)
    }
  }

  const handleRequestSort = (property: string) => {
    const isAsc = orderBy === property && order === 'asc'
    setOrder(isAsc ? 'desc' : 'asc')
    setOrderBy(property)
  }

  const sortFindings = (items: any[]) => {
    return [...items].sort((a, b) => {
      let aVal = a[orderBy]
      let bVal = b[orderBy]

      if (orderBy === 'severity') {
        const severityOrder: Record<string, number> = { critical: 4, high: 3, medium: 2, low: 1 }
        aVal = severityOrder[a.severity?.toLowerCase()] || 0
        bVal = severityOrder[b.severity?.toLowerCase()] || 0
      } else if (orderBy === 'timestamp') {
        aVal = new Date(a.timestamp || 0).getTime()
        bVal = new Date(b.timestamp || 0).getTime()
      } else if (orderBy === 'anomaly_score') {
        aVal = a.anomaly_score || 0
        bVal = b.anomaly_score || 0
      }

      return order === 'asc' 
        ? (aVal < bVal ? -1 : aVal > bVal ? 1 : 0)
        : (aVal > bVal ? -1 : aVal < bVal ? 1 : 0)
    })
  }

  // Column resize handlers
  const handleResizeMouseDown = (e: React.MouseEvent, column: ColumnKey) => {
    e.preventDefault()
    e.stopPropagation()
    
    resizeState.current = {
      column,
      startX: e.clientX,
      startWidth: columnWidths[column],
    }

    // Add global mouse event listeners
    document.addEventListener('mousemove', handleResizeMouseMove)
    document.addEventListener('mouseup', handleResizeMouseUp)
    
    // Prevent text selection while dragging
    document.body.style.userSelect = 'none'
    document.body.style.cursor = 'col-resize'
  }

  const handleResizeMouseMove = (e: MouseEvent) => {
    if (!resizeState.current.column) return

    const diff = e.clientX - resizeState.current.startX
    const newWidth = Math.max(60, resizeState.current.startWidth + diff)
    
    setColumnWidths(prev => ({
      ...prev,
      [resizeState.current.column!]: newWidth,
    }))
  }

  const handleResizeMouseUp = () => {
    resizeState.current = {
      column: null,
      startX: 0,
      startWidth: 0,
    }

    document.removeEventListener('mousemove', handleResizeMouseMove)
    document.removeEventListener('mouseup', handleResizeMouseUp)
    
    // Restore normal cursor and text selection
    document.body.style.userSelect = ''
    document.body.style.cursor = ''
  }

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      document.removeEventListener('mousemove', handleResizeMouseMove)
      document.removeEventListener('mouseup', handleResizeMouseUp)
      document.body.style.userSelect = ''
      document.body.style.cursor = ''
    }
  }, [])

  if (loading) {
    return (
      <Box display="flex" justifyContent="center" p={4}>
        <CircularProgress size={32} />
      </Box>
    )
  }

  if (findings.length === 0) {
    return (
      <Box sx={{ p: 4, textAlign: 'center' }}>
        <Typography color="text.secondary">No findings found</Typography>
      </Box>
    )
  }

  const sortedFindings = sortFindings(findings)
  const paginatedFindings = sortedFindings.slice(page * rowsPerPage, page * rowsPerPage + rowsPerPage)

  // Resizable header cell component
  const ResizableHeaderCell = ({ 
    column, 
    sortKey, 
    label, 
    align = 'left' 
  }: { 
    column: ColumnKey
    sortKey?: string
    label: string
    align?: 'left' | 'right' 
  }) => (
    <TableCell 
      align={align}
      sx={{ 
        width: columnWidths[column],
        minWidth: columnWidths[column],
        maxWidth: columnWidths[column],
        position: 'relative',
        px: 2,
        borderRight: `1px solid ${alpha(theme.palette.divider, 0.3)}`,
        '&:last-child': {
          borderRight: 'none',
        },
      }}
    >
      {sortKey ? (
        <TableSortLabel
          active={orderBy === sortKey}
          direction={orderBy === sortKey ? order : 'asc'}
          onClick={() => handleRequestSort(sortKey)}
        >
          {label}
        </TableSortLabel>
      ) : (
        label
      )}
      <Box
        onMouseDown={(e) => handleResizeMouseDown(e, column)}
        sx={{
          position: 'absolute',
          right: -3,
          top: 0,
          bottom: 0,
          width: 6,
          cursor: 'col-resize',
          zIndex: 10,
          bgcolor: 'transparent',
          transition: 'background-color 0.2s',
          '&:hover': {
            bgcolor: alpha(theme.palette.primary.main, 0.5),
          },
          '&:active': {
            bgcolor: theme.palette.primary.main,
          },
        }}
      />
    </TableCell>
  )

  return (
    <>
      <TableContainer>
        <Table 
          size="small" 
          sx={{ 
            tableLayout: 'fixed',
            width: Object.values(columnWidths).reduce((a, b) => a + b, 0),
          }}
        >
          <TableHead>
            <TableRow>
              <ResizableHeaderCell column="id" sortKey="finding_id" label="ID" />
              <ResizableHeaderCell column="severity" sortKey="severity" label="Severity" />
              <ResizableHeaderCell column="tactic" label="MITRE Tactic" />
              <ResizableHeaderCell column="source" sortKey="data_source" label="Source" />
              <ResizableHeaderCell column="time" sortKey="timestamp" label="Time" />
              <ResizableHeaderCell column="score" sortKey="anomaly_score" label="Score" />
              <ResizableHeaderCell column="actions" label="Actions" align="right" />
            </TableRow>
          </TableHead>
          <TableBody>
            {paginatedFindings.map((finding) => (
              <TableRow
                key={finding.finding_id}
                onClick={() => handleViewFinding(finding.finding_id)}
                onMouseEnter={() => setHoveredRow(finding.finding_id)}
                onMouseLeave={() => setHoveredRow(null)}
                sx={{
                  cursor: 'pointer',
                  ...(finding.ai_enrichment && {
                    color: theme.palette.info.main,
                    '& .MuiTableCell-root': {
                      color: 'inherit',
                    },
                  }),
                }}
              >
                <TableCell 
                  sx={{ 
                    width: columnWidths.id,
                    minWidth: columnWidths.id,
                    maxWidth: columnWidths.id,
                    fontFamily: 'monospace', 
                    fontSize: '0.75rem',
                    wordBreak: 'break-all',
                    borderRight: `1px solid ${alpha(theme.palette.divider, 0.1)}`,
                  }}
                >
                  {finding.ai_enrichment ? (
                    <Tooltip title="AI Enriched" arrow>
                      <span>{finding.finding_id}</span>
                    </Tooltip>
                  ) : (
                    finding.finding_id
                  )}
                </TableCell>
                <TableCell 
                  sx={{ 
                    width: columnWidths.severity,
                    minWidth: columnWidths.severity,
                    maxWidth: columnWidths.severity,
                    borderRight: `1px solid ${alpha(theme.palette.divider, 0.1)}`,
                  }}
                >
                  <SeverityChip severity={finding.severity || 'unknown'} />
                </TableCell>
                <TableCell
                  sx={{
                    width: columnWidths.tactic,
                    minWidth: columnWidths.tactic,
                    maxWidth: columnWidths.tactic,
                    borderRight: `1px solid ${alpha(theme.palette.divider, 0.1)}`,
                  }}
                >
                  {finding.mitre_predictions && Object.keys(finding.mitre_predictions).length > 0 ? (() => {
                    const sorted = Object.entries(finding.mitre_predictions as Record<string, number>)
                      .sort(([,a], [,b]) => b - a)
                    const top = sorted[0]
                    const pct = top[1] <= 1 ? `${Math.round(top[1] * 100)}%` : ''
                    const label = pct ? `${top[0]} (${pct})` : top[0]
                    return (
                      <Tooltip title={sorted.length > 1 ? sorted.map(([t, p]) => `${t}: ${Math.round(p * 100)}%`).join(', ') : ''} arrow>
                        <Chip
                          label={label}
                          size="small"
                          variant="outlined"
                          color="primary"
                          sx={{ maxWidth: '100%', '& .MuiChip-label': { overflow: 'hidden', textOverflow: 'ellipsis' } }}
                        />
                      </Tooltip>
                    )
                  })() : (
                    <Typography variant="caption" color="text.secondary">-</Typography>
                  )}
                </TableCell>
                <TableCell 
                  sx={{ 
                    width: columnWidths.source,
                    minWidth: columnWidths.source,
                    maxWidth: columnWidths.source,
                    wordBreak: 'break-word',
                    borderRight: `1px solid ${alpha(theme.palette.divider, 0.1)}`,
                  }}
                >
                  {finding.data_source || '-'}
                </TableCell>
                <TableCell 
                  sx={{ 
                    width: columnWidths.time,
                    minWidth: columnWidths.time,
                    maxWidth: columnWidths.time,
                    fontSize: '0.75rem', 
                    color: 'text.secondary',
                    wordBreak: 'break-word',
                    borderRight: `1px solid ${alpha(theme.palette.divider, 0.1)}`,
                  }}
                >
                  {finding.timestamp ? new Date(finding.timestamp).toLocaleString() : '-'}
                </TableCell>
                <TableCell 
                  sx={{ 
                    width: columnWidths.score,
                    minWidth: columnWidths.score,
                    maxWidth: columnWidths.score,
                    borderRight: `1px solid ${alpha(theme.palette.divider, 0.1)}`,
                  }}
                >
                  {finding.anomaly_score ? finding.anomaly_score.toFixed(2) : '-'}
                </TableCell>
                <TableCell 
                  align="right" 
                  sx={{ 
                    width: columnWidths.actions,
                    minWidth: columnWidths.actions,
                    maxWidth: columnWidths.actions,
                  }}
                >
                  <Box
                    sx={{
                      display: 'flex',
                      gap: 0.5,
                      justifyContent: 'flex-end',
                      opacity: hoveredRow === finding.finding_id ? 1 : 0,
                      transition: 'opacity 0.15s',
                    }}
                  >
                    <Tooltip title="View Details">
                      <IconButton
                        size="small"
                        onClick={(e) => {
                          e.stopPropagation()
                          handleViewFinding(finding.finding_id)
                        }}
                      >
                        <ViewIcon sx={{ fontSize: 18 }} />
                      </IconButton>
                    </Tooltip>
                    {onInvestigate && (
                      <Tooltip title="Investigate">
                        <IconButton
                          size="small"
                          onClick={(e) => handleInvestigateClick(e, finding)}
                          sx={{
                            color: 'primary.main',
                            bgcolor: alpha(theme.palette.primary.main, 0.1),
                            '&:hover': {
                              bgcolor: alpha(theme.palette.primary.main, 0.2),
                            },
                          }}
                        >
                          <InvestigateIcon sx={{ fontSize: 18 }} />
                        </IconButton>
                      </Tooltip>
                    )}
                    {workflows.length > 0 && (
                      <Tooltip title="Run playbook against this finding">
                        <span>
                          <IconButton
                            size="small"
                            onClick={(e) => handleRunPlaybookClick(e, finding)}
                            disabled={runningWorkflowId !== null}
                            sx={{
                              color: 'secondary.main',
                              bgcolor: alpha(theme.palette.secondary.main, 0.1),
                              '&:hover': {
                                bgcolor: alpha(theme.palette.secondary.main, 0.2),
                              },
                            }}
                          >
                            {runningWorkflowId ? (
                              <CircularProgress size={16} />
                            ) : (
                              <PlaybookIcon sx={{ fontSize: 18 }} />
                            )}
                          </IconButton>
                        </span>
                      </Tooltip>
                    )}
                  </Box>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        <TablePagination
          rowsPerPageOptions={[10, 25, 50]}
          component="div"
          count={findings.length}
          rowsPerPage={rowsPerPage}
          page={page}
          onPageChange={(_, p) => setPage(p)}
          onRowsPerPageChange={(e) => {
            setRowsPerPage(parseInt(e.target.value, 10))
            setPage(0)
          }}
          sx={{
            '.MuiTablePagination-selectLabel, .MuiTablePagination-displayedRows': {
              fontSize: '0.75rem',
            },
          }}
        />
      </TableContainer>

      <FindingDetailDialog
        open={dialogOpen}
        onClose={() => { setDialogOpen(false); setSelectedFindingId(null) }}
        findingId={selectedFindingId}
        onUpdate={loadFindings}
      />

      <Menu anchorEl={anchorEl} open={Boolean(anchorEl)} onClose={handleCloseMenu}>
        <MenuItem disabled sx={{ opacity: 1 }}>
          <Typography variant="caption" color="text.secondary">Select agent:</Typography>
        </MenuItem>
        {agents.map((agent) => (
          <MenuItem key={agent.id} onClick={() => handleAgentSelect(agent.id)}>
            <ListItemIcon sx={{ minWidth: 32 }}>
              <span style={{ fontSize: '1.2rem' }}>{agent.icon}</span>
            </ListItemIcon>
            <ListItemText primary={agent.name} primaryTypographyProps={{ fontSize: '0.875rem' }} />
          </MenuItem>
        ))}
      </Menu>

      <Menu
        anchorEl={workflowAnchorEl}
        open={Boolean(workflowAnchorEl)}
        onClose={handleCloseWorkflowMenu}
      >
        <MenuItem disabled sx={{ opacity: 1 }}>
          <Typography variant="caption" color="text.secondary">
            Run playbook:
          </Typography>
        </MenuItem>
        {workflows.length === 0 ? (
          <MenuItem disabled>
            <Typography variant="caption" color="text.disabled">
              No workflows configured
            </Typography>
          </MenuItem>
        ) : (
          workflows.map((wf) => (
            <MenuItem key={wf.id} onClick={() => handleWorkflowSelect(wf.id)}>
              <ListItemIcon sx={{ minWidth: 32 }}>
                <PlaybookIcon sx={{ fontSize: 18, color: 'secondary.main' }} />
              </ListItemIcon>
              <ListItemText
                primary={wf.name}
                secondary={wf.description}
                primaryTypographyProps={{ fontSize: '0.875rem' }}
                secondaryTypographyProps={{ fontSize: '0.7rem', noWrap: true }}
              />
            </MenuItem>
          ))
        )}
      </Menu>
    </>
  )
}
