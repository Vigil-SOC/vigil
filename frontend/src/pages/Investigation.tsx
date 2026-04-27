import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  Box,
  Grid,
  Paper,
  Typography,
  CircularProgress,
  IconButton,
  Tooltip,
  ToggleButtonGroup,
  ToggleButton,
  Chip,
  Stack,
  Divider,
  Alert,
  Button,
} from '@mui/material'
import {
  Refresh as RefreshIcon,
  Fullscreen as FullscreenIcon,
  FullscreenExit as FullscreenExitIcon,
  SwapVert as SwapVertIcon,
} from '@mui/icons-material'
import { timelineApi, graphApi } from '../services/api'
import EventTimeline, { TimelineEvent } from '../components/timeline/EventTimeline'
import { GraphNode, GraphLink } from '../components/graph/EntityGraph'
import EntityVisualization from '../components/graph/EntityVisualization'
import {
  VSTRIKE_GRAPH_HIGHLIGHT_EVENT,
  VStrikeGraphHighlightDetail,
} from '../types/vstrike'

export default function Investigation() {
  const [searchParams] = useSearchParams()
  const caseId = searchParams.get('case_id')
  const findingIds = searchParams.get('finding_ids')?.split(',')
  const clusterId = searchParams.get('cluster_id')

  const [loading, setLoading] = useState(true)
  const [timelineEvents, setTimelineEvents] = useState<TimelineEvent[]>([])
  const [graphData, setGraphData] = useState<{ nodes: GraphNode[]; links: GraphLink[] }>({
    nodes: [],
    links: [],
  })
  const [highlightedNodes, setHighlightedNodes] = useState<string[]>([])
  const [highlightedEvents, setHighlightedEvents] = useState<string[]>([])
  const [layout, setLayout] = useState<'horizontal' | 'vertical'>('horizontal')
  const [timelineFullscreen, setTimelineFullscreen] = useState(false)
  const [graphFullscreen, setGraphFullscreen] = useState(false)
  const [viewMode, setViewMode] = useState<'both' | 'timeline' | 'graph'>('both')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    loadData()
  }, [caseId, findingIds, clusterId])

  // Listen for pivot events dispatched by the VStrike NetworkContextPanel.
  useEffect(() => {
    const handleVStrikeHighlight = (event: Event) => {
      const detail = (event as CustomEvent<VStrikeGraphHighlightDetail>).detail
      if (!detail?.nodeId) return
      setHighlightedNodes((prev) =>
        prev.includes(detail.nodeId) ? prev : [detail.nodeId]
      )
    }
    window.addEventListener(
      VSTRIKE_GRAPH_HIGHLIGHT_EVENT,
      handleVStrikeHighlight as EventListener
    )
    return () => {
      window.removeEventListener(
        VSTRIKE_GRAPH_HIGHLIGHT_EVENT,
        handleVStrikeHighlight as EventListener
      )
    }
  }, [])

  const loadData = async () => {
    setLoading(true)
    setError(null)
    try {
      await Promise.all([loadTimelineData(), loadGraphData()])
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to load investigation data'
      setError(message)
      console.error('Failed to load investigation data:', err)
    } finally {
      setLoading(false)
    }
  }

  const loadTimelineData = async () => {
    try {
      let response
      if (caseId) {
        response = await timelineApi.getCaseTimeline(caseId)
      } else if (clusterId) {
        response = await timelineApi.getClusterTimeline(clusterId)
      } else if (findingIds && findingIds.length > 0) {
        // Load timeline context for each finding and merge events
        const contextResponses = await Promise.all(
          findingIds.slice(0, 10).map((fid) =>
            timelineApi.getFindingContext(fid, 60).catch(() => null)
          )
        )
        const mergedEvents = new Map<string, any>()
        for (const res of contextResponses) {
          if (res?.data?.events) {
            for (const event of res.data.events) {
              mergedEvents.set(event.id, event)
            }
          }
        }
        setTimelineEvents(Array.from(mergedEvents.values()))
        return
      } else {
        response = await timelineApi.getTimelineRange({ limit: 500 })
      }
      setTimelineEvents(response.data.events || [])
    } catch (error) {
      console.error('Failed to load timeline:', error)
    }
  }

  const loadGraphData = async () => {
    try {
      let response
      if (caseId) {
        response = await graphApi.getAttackPath(caseId)
      } else if (clusterId) {
        response = await graphApi.getClusterGraph(clusterId)
      } else if (findingIds && findingIds.length > 0) {
        response = await graphApi.getEntityGraph({
          finding_ids: findingIds.join(','),
        })
      } else {
        response = await graphApi.getEntityGraph({ limit: 100 })
      }
      setGraphData({
        nodes: response.data.nodes || [],
        links: response.data.links || [],
      })
    } catch (error) {
      console.error('Failed to load graph:', error)
    }
  }

  const handleTimelineEventClick = (event: TimelineEvent) => {
    // Highlight related entities in graph
    if (event.metadata?.entity_context) {
      const entityContext = event.metadata.entity_context
      const relatedNodeIds: string[] = []

      // Helper to add IDs from singular or plural field names
      const addEntityIds = (prefix: string, singular: string, plural: string) => {
        const singularVal = entityContext[singular]
        const pluralVal = entityContext[plural]
        if (singularVal) {
          if (Array.isArray(singularVal)) {
            singularVal.forEach((v: string) => relatedNodeIds.push(`${prefix}-${v}`))
          } else {
            relatedNodeIds.push(`${prefix}-${singularVal}`)
          }
        }
        if (pluralVal && Array.isArray(pluralVal)) {
          pluralVal.forEach((v: string) => relatedNodeIds.push(`${prefix}-${v}`))
        }
      }

      addEntityIds('ip', 'src_ip', 'src_ips')
      addEntityIds('ip', 'dst_ip', 'dest_ips')
      addEntityIds('host', 'hostname', 'hostnames')
      addEntityIds('user', 'user', 'usernames')

      setHighlightedNodes(relatedNodeIds)
    }
  }

  const handleGraphNodeClick = (node: GraphNode) => {
    console.log('Graph node clicked:', node)

    // Highlight related timeline events
    if (node.metadata?.findings) {
      const relatedEventIds = timelineEvents
        .filter((e) => node.metadata.findings.includes(e.metadata?.finding_id))
        .map((e) => e.id)

      setHighlightedEvents(relatedEventIds)
    }
  }

  const handleRefresh = () => {
    loadData()
  }

  const toggleLayout = () => {
    setLayout(layout === 'horizontal' ? 'vertical' : 'horizontal')
  }

  const getTitle = () => {
    if (caseId) return `Investigation: Case ${caseId}`
    if (clusterId) return `Investigation: Cluster ${clusterId}`
    if (findingIds && findingIds.length > 0)
      return `Investigation: ${findingIds.length} Finding(s)`
    return 'Investigation Workspace'
  }

  // Derive a VStrike network id from the first node that carries one in its
  // VStrike enrichment topology metadata. Used to auto-load the iframe to
  // the relevant network when VStrike is configured. Falls back to undefined
  // (user picks from the dropdown) when no enrichment carries an id.
  // Must live above any early-return so React hook order stays stable.
  const vstrikeNetworkId = useMemo<string | undefined>(() => {
    for (const node of graphData.nodes) {
      const vstrike = (node.metadata as any)?.vstrike
      const fromTopology = vstrike?.topology_metadata?.network_id
      if (typeof fromTopology === 'string' && fromTopology) return fromTopology
      const direct = vstrike?.network_id
      if (typeof direct === 'string' && direct) return direct
    }
    return undefined
  }, [graphData.nodes])

  if (loading) {
    return (
      <Box display="flex" justifyContent="center" alignItems="center" height="80vh">
        <CircularProgress />
      </Box>
    )
  }

  const timelineHeight = timelineFullscreen ? 'calc(100vh - 200px)' : layout === 'horizontal' ? 400 : 300
  const graphHeight = graphFullscreen ? 'calc(100vh - 200px)' : layout === 'horizontal' ? 400 : 300

  return (
    <Box>
      {/* Header */}
      <Box display="flex" justifyContent="space-between" alignItems="center" mb={3}>
        <Box>
          <Typography variant="h5" sx={{ fontWeight: 700, mb: 0.5 }}>
            {getTitle()}
          </Typography>
          <Stack direction="row" spacing={1}>
            <Chip
              label={`${timelineEvents.length} events`}
              size="small"
              color="primary"
              variant="outlined"
            />
            <Chip
              label={`${graphData.nodes.length} entities`}
              size="small"
              color="secondary"
              variant="outlined"
            />
            <Chip
              label={`${graphData.links.length} connections`}
              size="small"
              color="info"
              variant="outlined"
            />
          </Stack>
        </Box>

        <Box display="flex" gap={1} alignItems="center">
          <ToggleButtonGroup
            size="small"
            value={viewMode}
            exclusive
            onChange={(_, value) => value && setViewMode(value)}
          >
            <ToggleButton value="both">Both</ToggleButton>
            <ToggleButton value="timeline">Timeline</ToggleButton>
            <ToggleButton value="graph">Graph</ToggleButton>
          </ToggleButtonGroup>

          <Divider orientation="vertical" flexItem />

          <Tooltip title="Toggle Layout">
            <IconButton size="small" onClick={toggleLayout}>
              <SwapVertIcon />
            </IconButton>
          </Tooltip>

          <Tooltip title="Refresh">
            <IconButton size="small" onClick={handleRefresh}>
              <RefreshIcon />
            </IconButton>
          </Tooltip>
        </Box>
      </Box>

      {/* Error Alert */}
      {error && (
        <Alert 
          severity="error" 
          sx={{ mb: 2 }}
          action={
            <Button color="inherit" size="small" onClick={handleRefresh}>
              Retry
            </Button>
          }
          onClose={() => setError(null)}
        >
          {error}
        </Alert>
      )}

      {/* Coordinated Views */}
      <Grid
        container
        spacing={2}
        direction={layout === 'horizontal' ? 'row' : 'column'}
      >
        {/* Timeline View */}
        {(viewMode === 'both' || viewMode === 'timeline') && (
          <Grid item xs={12} md={viewMode === 'both' ? (layout === 'horizontal' ? 12 : 12) : 12}>
            <Paper sx={{ p: 2 }}>
              <Box display="flex" justifyContent="space-between" alignItems="center" mb={2}>
                <Typography variant="h6">Timeline View</Typography>
                <Tooltip title={timelineFullscreen ? 'Exit Fullscreen' : 'Fullscreen'}>
                  <IconButton
                    size="small"
                    onClick={() => setTimelineFullscreen(!timelineFullscreen)}
                  >
                    {timelineFullscreen ? <FullscreenExitIcon /> : <FullscreenIcon />}
                  </IconButton>
                </Tooltip>
              </Box>
              <EventTimeline
                events={timelineEvents}
                onEventClick={handleTimelineEventClick}
                height={timelineHeight}
                groupBy="type"
                highlightedNodes={highlightedEvents}
              />
            </Paper>
          </Grid>
        )}

        {/* Graph View */}
        {(viewMode === 'both' || viewMode === 'graph') && (
          <Grid item xs={12} md={viewMode === 'both' ? (layout === 'horizontal' ? 12 : 12) : 12}>
            <Paper sx={{ p: 2, overflow: 'hidden' }}>
              <Box display="flex" justifyContent="space-between" alignItems="center" mb={2}>
                <Typography variant="h6">Entity Relationship Graph</Typography>
                <Tooltip title={graphFullscreen ? 'Exit Fullscreen' : 'Fullscreen'}>
                  <IconButton
                    size="small"
                    onClick={() => setGraphFullscreen(!graphFullscreen)}
                  >
                    {graphFullscreen ? <FullscreenExitIcon /> : <FullscreenIcon />}
                  </IconButton>
                </Tooltip>
              </Box>
              <Box sx={{ height: graphHeight, overflow: 'hidden', position: 'relative' }}>
                <EntityVisualization
                  nodes={graphData.nodes}
                  links={graphData.links}
                  onNodeClick={handleGraphNodeClick}
                  height={graphHeight}
                  highlightedNodes={highlightedNodes}
                  vstrikeNetworkId={vstrikeNetworkId}
                />
              </Box>
            </Paper>
          </Grid>
        )}
      </Grid>

      {/* Info Panel */}
      {(highlightedNodes.length > 0 || highlightedEvents.length > 0) && (
        <Paper sx={{ p: 2, mt: 2 }}>
          <Typography variant="subtitle2" gutterBottom>
            Selection Info
          </Typography>
          {highlightedNodes.length > 0 && (
            <Typography variant="body2" color="textSecondary">
              Highlighted Entities: {highlightedNodes.join(', ')}
            </Typography>
          )}
          {highlightedEvents.length > 0 && (
            <Typography variant="body2" color="textSecondary">
              Highlighted Events: {highlightedEvents.length}
            </Typography>
          )}
        </Paper>
      )}
    </Box>
  )
}

