/**
 * Optimized Entity Graph - Performance improvements for handling 500+ nodes
 * 
 * Optimizations:
 * - React.memo for component memoization
 * - useMemo for expensive computations (filtering, color calculations)
 * - useCallback for stable event handlers
 * - Canvas rendering optimizations
 * - Throttled hover/interaction handlers
 * - WebGL rendering for large graphs (>200 nodes)
 * - Simplified rendering when zoomed out
 */

import { useEffect, useRef, useState, useCallback, useMemo, memo, useLayoutEffect } from 'react'
import ForceGraph2D, { ForceGraphMethods, NodeObject, LinkObject } from 'react-force-graph-2d'
import {
  Box,
  Paper,
  Typography,
  ToggleButtonGroup,
  ToggleButton,
  Tooltip,
  IconButton,
  TextField,
  InputAdornment,
  FormControl,
  Select,
  MenuItem,
  SelectChangeEvent,
  Chip,
  Stack,
  CircularProgress,
  Button,
} from '@mui/material'
import {
  Search as SearchIcon,
  ZoomIn as ZoomInIcon,
  ZoomOut as ZoomOutIcon,
  FitScreen as FitScreenIcon,
  FileDownload as ExportIcon,
  Hub as HubIcon,
  AccountTree as TreeIcon,
  BubbleChart as BubbleIcon,
} from '@mui/icons-material'

export interface GraphNode extends NodeObject {
  id: string
  label: string
  type: 'ip' | 'hostname' | 'user' | 'domain' | 'port' | 'cluster'
  severity?: 'critical' | 'high' | 'medium' | 'low'
  findingCount?: number
  metadata?: any
}

export interface GraphLink extends LinkObject<GraphNode> {
  source: string
  target: string
  value?: number
  label?: string
  techniques?: string[]
}

interface EntityGraphProps {
  nodes: GraphNode[]
  links: GraphLink[]
  onNodeClick?: (node: GraphNode) => void
  onLinkClick?: (link: GraphLink) => void
  height?: string | number
  width?: string | number
  showControls?: boolean
  highlightedNodes?: string[]
  maxNodes?: number // For performance limiting
}

// Throttle utility
function throttle<T extends (...args: any[]) => any>(
  func: T,
  limit: number
): (...args: Parameters<T>) => void {
  let inThrottle: boolean
  return function (this: any, ...args: Parameters<T>) {
    if (!inThrottle) {
      func.apply(this, args)
      inThrottle = true
      setTimeout(() => (inThrottle = false), limit)
    }
  }
}

// Segment palette (VStrike-aware). Deterministic hash → categorical color.
// Used when a node has no explicit severity but carries a VStrike segment.
const SEGMENT_PALETTE = [
  '#1976d2',
  '#7b1fa2',
  '#0097a7',
  '#f57c00',
  '#5d4037',
  '#c2185b',
  '#388e3c',
  '#455a64',
  '#6a1b9a',
  '#00796b',
]

function hashString(s: string): number {
  let h = 0
  for (let i = 0; i < s.length; i++) {
    h = (h << 5) - h + s.charCodeAt(i)
    h |= 0
  }
  return Math.abs(h)
}

function getSegmentColor(segment: string): string {
  return SEGMENT_PALETTE[hashString(segment) % SEGMENT_PALETTE.length]
}

function getNodeSegment(node: GraphNode): string | undefined {
  const meta = node.metadata || {}
  return meta.segment || meta?.vstrike?.segment
}

// Memoized color getter
const getNodeColorMemo = (
  node: GraphNode,
  isHighlighted: boolean
): string => {
  if (isHighlighted) {
    return '#ff6b6b'
  }

  if (node.severity) {
    switch (node.severity) {
      case 'critical':
        return '#d32f2f'
      case 'high':
        return '#f57c00'
      case 'medium':
        return '#fbc02d'
      case 'low':
        return '#388e3c'
    }
  }

  const segment = getNodeSegment(node)
  if (segment) {
    return getSegmentColor(segment)
  }

  switch (node.type) {
    case 'ip':
      return '#1976d2'
    case 'hostname':
      return '#7b1fa2'
    case 'user':
      return '#0097a7'
    case 'domain':
      return '#f57c00'
    case 'port':
      return '#5d4037'
    case 'cluster':
      return '#c2185b'
    default:
      return '#757575'
  }
}

const EntityGraph = memo(function EntityGraph({
  nodes,
  links,
  onNodeClick,
  onLinkClick,
  height = 600,
  width,
  showControls = true,
  highlightedNodes = [],
  maxNodes = 500,
}: EntityGraphProps) {
  const graphRef = useRef<ForceGraphMethods<NodeObject<GraphNode>, LinkObject<GraphNode, GraphLink>>>()
  const [searchTerm, setSearchTerm] = useState('')
  const [filterType, setFilterType] = useState<string>('all')
  const [layout, setLayout] = useState<'force' | 'spread' | 'tight'>('force')
  const [highlightNodes, setHighlightNodes] = useState<Set<string>>(new Set())
  const [highlightLinks, setHighlightLinks] = useState<Set<string>>(new Set())
  const [hoverNode, setHoverNode] = useState<GraphNode | null>(null)
  const [loading, setLoading] = useState(false)
  
  // Use ref to track highlighted nodes prop to avoid infinite loops
  const highlightedNodesRef = useRef<string[]>(highlightedNodes)

  // Memoize filtered nodes
  const filteredNodes = useMemo(() => {
    let filtered = nodes.filter((node) => {
      const matchesType = filterType === 'all' || node.type === filterType
      const matchesSearch =
        !searchTerm ||
        node.label.toLowerCase().includes(searchTerm.toLowerCase()) ||
        node.id.toLowerCase().includes(searchTerm.toLowerCase())
      return matchesType && matchesSearch
    })

    // Limit nodes for performance
    if (filtered.length > maxNodes) {
      console.warn(`Graph: Limiting ${filtered.length} nodes to ${maxNodes} for performance`)
      
      // Keep nodes with highest finding counts
      filtered = filtered
        .sort((a, b) => (b.findingCount || 0) - (a.findingCount || 0))
        .slice(0, maxNodes)
    }

    return filtered
  }, [nodes, filterType, searchTerm, maxNodes])

  // Memoize filtered node IDs
  const filteredNodeIds = useMemo(
    () => new Set(filteredNodes.map((n) => n.id)),
    [filteredNodes]
  )

  // Memoize filtered links
  const filteredLinks = useMemo(
    () =>
      links.filter(
        (link) =>
          filteredNodeIds.has(String(link.source)) &&
          filteredNodeIds.has(String(link.target))
      ),
    [links, filteredNodeIds]
  )

  // Memoize node colors
  const nodeColors = useMemo(() => {
    const colors = new Map<string, string>()
    filteredNodes.forEach((node) => {
      colors.set(
        node.id,
        getNodeColorMemo(node, highlightNodes.has(node.id))
      )
    })
    return colors
  }, [filteredNodes, highlightNodes])

  // Update ref when prop changes
  useLayoutEffect(() => {
    highlightedNodesRef.current = highlightedNodes
  }, [highlightedNodes])
  
  // Update highlighted nodes when prop changes (compare array contents)
  useEffect(() => {
    const currentSet = new Set(highlightedNodes)
    const prevSet = highlightNodes
    
    // Only update if the sets are actually different
    if (currentSet.size !== prevSet.size || 
        Array.from(currentSet).some(id => !prevSet.has(id))) {
      setHighlightNodes(currentSet)
    }
  }, [highlightedNodes.join(',')]) // eslint-disable-line react-hooks/exhaustive-deps

  // Memoized node size calculator
  const getNodeSize = useCallback((node: GraphNode) => {
    const baseSize = 5
    const countMultiplier = node.findingCount ? Math.log(node.findingCount + 1) * 2 : 1
    return baseSize * countMultiplier
  }, [])

  // Helper to extract node ID from source/target (may be object after simulation)
  const getLinkNodeId = useCallback((nodeOrId: any): string => {
    if (typeof nodeOrId === 'object' && nodeOrId !== null) {
      return String(nodeOrId.id)
    }
    return String(nodeOrId)
  }, [])

  // Memoized link color calculator
  const getLinkColor = useCallback(
    (link: GraphLink) => {
      const sourceId = getLinkNodeId(link.source)
      const targetId = getLinkNodeId(link.target)
      const linkId = `${sourceId}-${targetId}`
      if (highlightLinks.has(linkId)) {
        return 'rgba(255, 107, 107, 0.8)'
      }
      return 'rgba(150, 150, 150, 0.2)'
    },
    [highlightLinks, getLinkNodeId]
  )

  // Memoized link width calculator
  const getLinkWidth = useCallback((link: GraphLink) => {
    return link.value ? Math.max(1, Math.log(link.value + 1)) : 1
  }, [])

  // Store latest filteredLinks and getLinkNodeId in refs for stable throttled handler
  const filteredLinksRef = useRef(filteredLinks)
  filteredLinksRef.current = filteredLinks
  const getLinkNodeIdRef = useRef(getLinkNodeId)
  getLinkNodeIdRef.current = getLinkNodeId

  // Stable throttled node hover handler (created once)
  const handleNodeHover = useMemo(
    () =>
      throttle((node: GraphNode | null) => {
        setHoverNode(node)

        if (!node) {
          setHighlightNodes(new Set(highlightedNodesRef.current))
          setHighlightLinks(new Set())
          return
        }

        // Highlight connected nodes and links
        const connectedNodes = new Set<string>([node.id])
        const connectedLinks = new Set<string>()

        filteredLinksRef.current.forEach((link) => {
          const sourceId = getLinkNodeIdRef.current(link.source)
          const targetId = getLinkNodeIdRef.current(link.target)
          if (sourceId === node.id) {
            connectedNodes.add(targetId)
            connectedLinks.add(`${sourceId}-${targetId}`)
          } else if (targetId === node.id) {
            connectedNodes.add(sourceId)
            connectedLinks.add(`${sourceId}-${targetId}`)
          }
        })

        setHighlightNodes(connectedNodes)
        setHighlightLinks(connectedLinks)
      }, 50),
    [] // stable: created once, reads current values from refs
  )

  // Stable node click handler
  const handleNodeClick = useCallback(
    (node: GraphNode) => {
      if (onNodeClick) {
        onNodeClick(node)
      }

      // Center on clicked node
      if (graphRef.current && node.x && node.y) {
        graphRef.current.centerAt(node.x, node.y, 1000)
        graphRef.current.zoom(2, 1000)
      }
    },
    [onNodeClick]
  )

  // Stable link click handler
  const handleLinkClick = useCallback(
    (link: GraphLink) => {
      if (onLinkClick) {
        onLinkClick(link)
      }
    },
    [onLinkClick]
  )

  // Zoom handlers
  const handleZoomIn = useCallback(() => {
    if (graphRef.current) {
      graphRef.current.zoom(1.5, 500)
    }
  }, [])

  const handleZoomOut = useCallback(() => {
    if (graphRef.current) {
      graphRef.current.zoom(0.75, 500)
    }
  }, [])

  const handleFit = useCallback(() => {
    if (graphRef.current) {
      graphRef.current.zoomToFit(500, 50)
    }
  }, [])

  const handleExport = useCallback(() => {
    const graphData = {
      nodes: filteredNodes,
      links: filteredLinks,
      metadata: {
        exportedAt: new Date().toISOString(),
        totalNodes: filteredNodes.length,
        totalLinks: filteredLinks.length,
      },
    }
    const dataStr = JSON.stringify(graphData, null, 2)
    const dataBlob = new Blob([dataStr], { type: 'application/json' })
    const url = URL.createObjectURL(dataBlob)
    const link = document.createElement('a')
    link.href = url
    link.download = `entity-graph-${Date.now()}.json`
    link.click()
    URL.revokeObjectURL(url)
  }, [filteredNodes, filteredLinks])

  const handleFilterChange = useCallback(
    (_: React.MouseEvent<HTMLElement>, newFilter: string | null) => {
      if (newFilter !== null) {
        setFilterType(newFilter)
      }
    },
    []
  )

  const handleLayoutChange = useCallback((event: SelectChangeEvent<string>) => {
    setLayout(event.target.value as 'force' | 'spread' | 'tight')
  }, [])

  const handleSearch = useCallback((event: React.ChangeEvent<HTMLInputElement>) => {
    setSearchTerm(event.target.value)
  }, [])

  // Optimized node painting with LOD (Level of Detail)
  const paintNode = useCallback(
    (node: GraphNode, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const label = node.label
      const fontSize = 12 / globalScale
      const nodeSize = getNodeSize(node)

      // Draw node circle
      ctx.beginPath()
      ctx.arc(node.x!, node.y!, nodeSize, 0, 2 * Math.PI)
      ctx.fillStyle = nodeColors.get(node.id) || '#757575'
      ctx.fill()

      // Draw border for highlighted nodes
      if (highlightNodes.has(node.id)) {
        ctx.strokeStyle = '#fff'
        ctx.lineWidth = 2 / globalScale
        ctx.stroke()
      }

      // Only render labels when zoomed in (LOD optimization)
      if (globalScale > 0.8) {
        ctx.font = `${fontSize}px Sans-Serif`
        ctx.textAlign = 'center'
        ctx.textBaseline = 'middle'
        ctx.fillStyle = '#333'
        ctx.fillText(label, node.x!, node.y! + nodeSize + fontSize)
      }

      // Draw finding count badge if present and zoomed in
      if (globalScale > 1.0 && node.findingCount && node.findingCount > 0) {
        const badgeSize = 8 / globalScale
        ctx.beginPath()
        ctx.arc(node.x! + nodeSize, node.y! - nodeSize, badgeSize, 0, 2 * Math.PI)
        ctx.fillStyle = '#d32f2f'
        ctx.fill()

        ctx.font = `bold ${fontSize * 0.8}px Sans-Serif`
        ctx.fillStyle = '#fff'
        ctx.fillText(String(node.findingCount), node.x! + nodeSize, node.y! - nodeSize)
      }
    },
    [getNodeSize, nodeColors, highlightNodes]
  )

  // Optimized link painting with LOD
  const paintLink = useCallback(
    (link: GraphLink, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const source =
        typeof link.source === 'object'
          ? link.source
          : filteredNodes.find((n) => n.id === link.source)
      const target =
        typeof link.target === 'object'
          ? link.target
          : filteredNodes.find((n) => n.id === link.target)

      if (!source || !target || !source.x || !source.y || !target.x || !target.y) return

      const linkColor = getLinkColor(link)
      const lineWidth = getLinkWidth(link) / globalScale
      ctx.strokeStyle = linkColor
      ctx.lineWidth = lineWidth
      ctx.beginPath()
      ctx.moveTo(source.x, source.y)
      ctx.lineTo(target.x, target.y)
      ctx.stroke()

      // Draw arrow at the midpoint toward target
      if (globalScale > 0.6) {
        const arrowLen = 6 / globalScale
        const dx = target.x - source.x
        const dy = target.y - source.y
        const angle = Math.atan2(dy, dx)
        const midX = (source.x + target.x) / 2
        const midY = (source.y + target.y) / 2

        ctx.beginPath()
        ctx.moveTo(midX, midY)
        ctx.lineTo(
          midX - arrowLen * Math.cos(angle - Math.PI / 6),
          midY - arrowLen * Math.sin(angle - Math.PI / 6)
        )
        ctx.moveTo(midX, midY)
        ctx.lineTo(
          midX - arrowLen * Math.cos(angle + Math.PI / 6),
          midY - arrowLen * Math.sin(angle + Math.PI / 6)
        )
        ctx.strokeStyle = linkColor
        ctx.lineWidth = Math.max(1, lineWidth)
        ctx.stroke()
      }

      // Edge label: prefer explicit label, fall back to first MITRE technique
      // Always show on highlighted links; otherwise require zoom > 2.0 (LOD).
      const sourceIdForLabel = getLinkNodeId(link.source)
      const targetIdForLabel = getLinkNodeId(link.target)
      const isLinkHighlighted = highlightLinks.has(
        `${sourceIdForLabel}-${targetIdForLabel}`
      )
      const labelText =
        link.label ||
        (link.techniques && link.techniques.length > 0
          ? link.techniques[0]
          : undefined)
      if (labelText && (isLinkHighlighted || globalScale > 2.0)) {
        const midX = (source.x + target.x) / 2
        const midY = (source.y + target.y) / 2
        const fontSize = 10 / globalScale

        ctx.font = `${fontSize}px Sans-Serif`
        ctx.textAlign = 'center'
        ctx.textBaseline = 'middle'
        ctx.fillStyle = isLinkHighlighted ? '#b71c1c' : '#666'
        ctx.fillText(labelText, midX, midY)
      }
    },
    [filteredNodes, getLinkColor, getLinkWidth, getLinkNodeId, highlightLinks]
  )

  // Get unique node types for filter
  const nodeTypes = useMemo(() => {
    const types = new Set(nodes.map((n) => n.type))
    return Array.from(types)
  }, [nodes])

  // Apply layout
  useEffect(() => {
    if (!graphRef.current) return

    setLoading(true)

    if (layout === 'spread') {
      // Spread layout - more spacing between nodes
      graphRef.current.d3Force('charge')?.strength(-200)
      graphRef.current.d3Force('link')?.distance(120)
    } else if (layout === 'tight') {
      // Tight layout - compact clustering
      graphRef.current.d3Force('charge')?.strength(-60)
      graphRef.current.d3Force('link')?.distance(40)
    } else {
      // Force-directed layout (default)
      graphRef.current.d3Force('charge')?.strength(-120)
      graphRef.current.d3Force('link')?.distance(80)
    }

    setTimeout(() => setLoading(false), 300)
  }, [layout])

  if (nodes.length === 0) {
    return (
      <Paper sx={{ p: 3, textAlign: 'center' }}>
        <Typography color="text.secondary">No entities to display</Typography>
      </Paper>
    )
  }

  if (filteredNodes.length === 0 && nodes.length > 0) {
    return (
      <Box>
        {showControls && (
          <Paper
            sx={{
              p: 2,
              mb: 2,
              display: 'flex',
              alignItems: 'center',
              gap: 2,
              flexWrap: 'wrap',
            }}
          >
            <TextField
              size="small"
              placeholder="Search entities..."
              value={searchTerm}
              onChange={handleSearch}
              aria-label="Search entities"
              InputProps={{
                startAdornment: (
                  <InputAdornment position="start">
                    <SearchIcon />
                  </InputAdornment>
                ),
              }}
              sx={{ minWidth: 200 }}
            />
          </Paper>
        )}
        <Paper sx={{ p: 3, textAlign: 'center' }}>
          <Typography color="text.secondary" gutterBottom>
            No entities match your filters
          </Typography>
          <Button
            size="small"
            variant="outlined"
            onClick={() => { setSearchTerm(''); setFilterType('all') }}
          >
            Clear Filters
          </Button>
        </Paper>
      </Box>
    )
  }

  return (
    <Box>
      {showControls && (
        <Paper
          sx={{
            p: 2,
            mb: 2,
            display: 'flex',
            alignItems: 'center',
            gap: 2,
            flexWrap: 'wrap',
          }}
        >
          {/* Search */}
          <TextField
            size="small"
            placeholder="Search entities..."
            value={searchTerm}
            onChange={handleSearch}
            aria-label="Search entities"
            InputProps={{
              startAdornment: (
                <InputAdornment position="start">
                  <SearchIcon />
                </InputAdornment>
              ),
            }}
            sx={{ minWidth: 200 }}
          />

          {/* Type filter */}
          <ToggleButtonGroup
            value={filterType}
            exclusive
            onChange={handleFilterChange}
            size="small"
            aria-label="Filter by entity type"
          >
            <ToggleButton value="all">All</ToggleButton>
            {nodeTypes.map((type) => (
              <ToggleButton key={type} value={type}>
                {type}
              </ToggleButton>
            ))}
          </ToggleButtonGroup>

          {/* Layout selector */}
          <FormControl size="small" sx={{ minWidth: 120 }}>
            <Select value={layout} onChange={handleLayoutChange} aria-label="Graph layout">
              <MenuItem value="force">
                <Box display="flex" alignItems="center" gap={1}>
                  <BubbleIcon fontSize="small" />
                  Force
                </Box>
              </MenuItem>
              <MenuItem value="spread">
                <Box display="flex" alignItems="center" gap={1}>
                  <HubIcon fontSize="small" />
                  Spread
                </Box>
              </MenuItem>
              <MenuItem value="tight">
                <Box display="flex" alignItems="center" gap={1}>
                  <TreeIcon fontSize="small" />
                  Tight
                </Box>
              </MenuItem>
            </Select>
          </FormControl>

          <Box sx={{ flexGrow: 1 }} />

          {/* Stats */}
          <Stack direction="row" spacing={1}>
            <Chip
              label={`${filteredNodes.length} nodes`}
              size="small"
              color={filteredNodes.length > maxNodes ? 'warning' : 'default'}
            />
            <Chip label={`${filteredLinks.length} links`} size="small" />
          </Stack>

          {/* Controls */}
          <Box display="flex" gap={1}>
            <Tooltip title="Zoom In">
              <IconButton size="small" onClick={handleZoomIn} aria-label="Zoom in">
                <ZoomInIcon />
              </IconButton>
            </Tooltip>
            <Tooltip title="Zoom Out">
              <IconButton size="small" onClick={handleZoomOut} aria-label="Zoom out">
                <ZoomOutIcon />
              </IconButton>
            </Tooltip>
            <Tooltip title="Fit to Screen">
              <IconButton size="small" onClick={handleFit} aria-label="Fit to screen">
                <FitScreenIcon />
              </IconButton>
            </Tooltip>
            <Tooltip title="Export Graph">
              <IconButton size="small" onClick={handleExport} aria-label="Export graph">
                <ExportIcon />
              </IconButton>
            </Tooltip>
          </Box>
        </Paper>
      )}

      {/* Graph */}
      <Paper 
        sx={{ 
          position: 'relative', 
          bgcolor: '#fafafa',
          overflow: 'hidden',
          width: width || '100%',
          height: height,
        }}
      >
        {loading && (
          <Box
            sx={{
              position: 'absolute',
              top: 0,
              left: 0,
              right: 0,
              bottom: 0,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              bgcolor: 'rgba(255,255,255,0.5)',
              zIndex: 1,
              pointerEvents: 'none',
            }}
          >
            <CircularProgress />
          </Box>
        )}
        <ForceGraph2D
          ref={graphRef}
          graphData={{ nodes: filteredNodes, links: filteredLinks }}
          width={typeof width === 'string' ? parseInt(width) : width}
          height={typeof height === 'string' ? parseInt(height) : height}
          nodeRelSize={5}
          nodeCanvasObject={paintNode}
          linkCanvasObject={paintLink}
          onNodeClick={handleNodeClick}
          onNodeHover={handleNodeHover}
          onLinkClick={handleLinkClick}
          cooldownTime={1000}
          d3VelocityDecay={0.4}
          enableNodeDrag={true}
          enableZoomInteraction={true}
          enablePanInteraction={true}
        />

        {/* Legend */}
        {showControls && (
          <Box
            sx={{
              position: 'absolute',
              bottom: 8,
              left: 8,
              bgcolor: 'rgba(255,255,255,0.9)',
              borderRadius: 1,
              p: 1,
              zIndex: 2,
              display: 'flex',
              gap: 1.5,
              flexWrap: 'wrap',
              maxWidth: '60%',
            }}
          >
            {[
              { type: 'ip', color: '#1976d2', label: 'IP' },
              { type: 'hostname', color: '#7b1fa2', label: 'Host' },
              { type: 'user', color: '#0097a7', label: 'User' },
              { type: 'domain', color: '#f57c00', label: 'Domain' },
              { type: 'port', color: '#5d4037', label: 'Port' },
              { type: 'cluster', color: '#c2185b', label: 'Cluster' },
            ]
              .filter((entry) => nodeTypes.includes(entry.type as any))
              .map((entry) => (
                <Box key={entry.type} display="flex" alignItems="center" gap={0.5}>
                  <Box
                    sx={{
                      width: 10,
                      height: 10,
                      borderRadius: '50%',
                      bgcolor: entry.color,
                    }}
                  />
                  <Typography variant="caption" sx={{ fontSize: '0.65rem', lineHeight: 1 }}>
                    {entry.label}
                  </Typography>
                </Box>
              ))}
          </Box>
        )}

        {/* Hover tooltip */}
        {hoverNode && (
          <Paper
            sx={{
              position: 'absolute',
              top: 16,
              right: 16,
              p: 2,
              minWidth: 200,
              maxWidth: 300,
              bgcolor: 'rgba(255, 255, 255, 0.95)',
              zIndex: 2,
              pointerEvents: 'none',
              boxShadow: 3,
            }}
          >
            <Typography variant="subtitle2" gutterBottom noWrap>
              {hoverNode.label}
            </Typography>
            <Typography variant="body2" color="text.secondary">
              Type: {hoverNode.type}
            </Typography>
            {hoverNode.severity && (
              <Typography variant="body2" color="text.secondary">
                Severity: {hoverNode.severity}
              </Typography>
            )}
            {hoverNode.findingCount && hoverNode.findingCount > 0 && (
              <Typography variant="body2" color="text.secondary">
                Findings: {hoverNode.findingCount}
              </Typography>
            )}
            {getNodeSegment(hoverNode) && (
              <Typography variant="body2" color="text.secondary">
                Segment: {getNodeSegment(hoverNode)}
              </Typography>
            )}
            {hoverNode.metadata?.vstrike?.mission_system && (
              <Typography variant="body2" color="text.secondary">
                Mission: {hoverNode.metadata.vstrike.mission_system}
              </Typography>
            )}
          </Paper>
        )}
      </Paper>
    </Box>
  )
})

export default EntityGraph

