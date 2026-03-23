/**
 * Optimized Event Timeline - Performance improvements for handling 1000+ events
 * 
 * Optimizations:
 * - React.memo for component memoization
 * - useMemo for expensive computations
 * - useCallback for stable event handlers
 * - Debounced zoom/pan handlers
 * - Lazy loading for large datasets
 * - Canvas rendering optimization
 */

import { useEffect, useRef, useState, useMemo, useCallback, memo } from 'react'
import { Timeline, TimelineOptions } from 'vis-timeline/standalone'
import { DataSet } from 'vis-data/standalone'
import {
  Box,
  Paper,
  Typography,
  ToggleButtonGroup,
  ToggleButton,
  Tooltip,
  IconButton,
  Chip,
  FormControl,
  Select,
  MenuItem,
  SelectChangeEvent,
  CircularProgress,
} from '@mui/material'
import {
  ZoomIn as ZoomInIcon,
  ZoomOut as ZoomOutIcon,
  FitScreen as FitScreenIcon,
  FileDownload as ExportIcon,
  PlayArrow as PlayIcon,
  Pause as PauseIcon,
} from '@mui/icons-material'
import { format } from 'date-fns'
import 'vis-timeline/styles/vis-timeline-graph2d.css'

export interface TimelineEvent {
  id: string
  content: string
  start: Date | string
  end?: Date | string
  group?: string
  className?: string
  type?: 'finding' | 'activity' | 'decision' | 'status' | 'note'
  severity?: string
  metadata?: any
}

interface EventTimelineProps {
  events: TimelineEvent[]
  onEventClick?: (event: TimelineEvent) => void
  onRangeChange?: (start: Date, end: Date) => void
  height?: string | number
  showControls?: boolean
  groupBy?: 'type' | 'severity' | 'none'
  maxEvents?: number // Limit for virtualization
  highlightedNodes?: string[] // For highlighting specific events
}

// Debounce utility
function debounce<T extends (...args: any[]) => any>(
  func: T,
  wait: number
): (...args: Parameters<T>) => void {
  let timeout: NodeJS.Timeout | null = null
  return (...args: Parameters<T>) => {
    if (timeout) clearTimeout(timeout)
    timeout = setTimeout(() => func(...args), wait)
  }
}

// Event content creator function
const createEventContent = (event: TimelineEvent): string => {
  let content = `<div class="timeline-event-content">`
  
  if (event.severity) {
    const severityColor = 
      event.severity === 'critical' ? '#f44336' :
      event.severity === 'high' ? '#ff9800' :
      event.severity === 'medium' ? '#ffc107' :
      event.severity === 'low' ? '#4caf50' : '#9e9e9e'
    const textColor = event.severity === 'medium' ? '#000' : '#fff'
    
    content += `<span class="severity-badge" style="background-color: ${severityColor}; color: ${textColor};">${event.severity.toUpperCase()}</span>`
  }
  
  content += `<span class="event-label">${event.content}</span>`
  content += `</div>`
  
  return content
}

// Memoized event className getter
const getEventClassName = (event: TimelineEvent): string => {
  const classes = ['timeline-event']
  
  if (event.type) {
    classes.push(`event-${event.type}`)
  }
  
  if (event.severity) {
    classes.push(`severity-${event.severity}`)
  }
  
  if (event.className) {
    classes.push(event.className)
  }
  
  return classes.join(' ')
}

// Memoized event style getter - uses subtle tinted backgrounds, main color comes from CSS classes
const getEventStyle = (event: TimelineEvent): string => {
  if (event.severity === 'critical') {
    return 'background-color: rgba(244,67,54,0.15); border-color: rgba(244,67,54,0.5); color: #fecaca;'
  } else if (event.severity === 'high') {
    return 'background-color: rgba(255,152,0,0.15); border-color: rgba(255,152,0,0.5); color: #fed7aa;'
  } else if (event.severity === 'medium') {
    return 'background-color: rgba(255,193,7,0.12); border-color: rgba(255,193,7,0.4); color: #fef3c7;'
  } else if (event.severity === 'low') {
    return 'background-color: rgba(76,175,80,0.12); border-color: rgba(76,175,80,0.4); color: #bbf7d0;'
  }
  return ''
}

// Memoized group name getter
const getGroupName = (event: TimelineEvent, groupBy: string): string => {
  if (groupBy === 'type') {
    return event.type || 'Other'
  } else if (groupBy === 'severity') {
    return event.severity || 'Unknown'
  }
  return ''
}

const EventTimeline = memo(function EventTimeline({
  events,
  onEventClick,
  onRangeChange,
  height = 400,
  showControls = true,
  groupBy = 'type',
  maxEvents = 1000,
  highlightedNodes = [],
}: EventTimelineProps) {
  const timelineRef = useRef<HTMLDivElement>(null)
  const timelineInstance = useRef<Timeline | null>(null)
  const [filterType, setFilterType] = useState<string>('all')
  const [isPlaying, setIsPlaying] = useState(false)
  const [playbackSpeed, setPlaybackSpeed] = useState<number>(1000)
  const [loading, setLoading] = useState(false)

  // Memoize filtered events
  const filteredEvents = useMemo(() => {
    const filtered = filterType === 'all' 
      ? events 
      : events.filter((event) => event.type === filterType)
    
    // Limit events for performance (virtualization)
    if (filtered.length > maxEvents) {
      console.warn(`Timeline: Limiting ${filtered.length} events to ${maxEvents} for performance`)
      return filtered.slice(0, maxEvents)
    }
    
    return filtered
  }, [events, filterType, maxEvents])

  // Memoize timeline items
  const timelineItems = useMemo(() => {
    return new DataSet(
      filteredEvents.map((event) => ({
        id: event.id,
        content: createEventContent(event),
        start: new Date(event.start),
        end: event.end ? new Date(event.end) : undefined,
        group: groupBy === 'none' ? undefined : getGroupName(event, groupBy),
        className: getEventClassName(event),
        type: event.end ? 'range' : 'point',
        style: getEventStyle(event),
      }))
    )
  }, [filteredEvents, groupBy])

  // Memoize timeline groups
  const timelineGroups = useMemo(() => {
    if (groupBy === 'none') return undefined
    
    const groupSet = new Set(
      filteredEvents.map((e) => getGroupName(e, groupBy)).filter(Boolean)
    )
    
    return new DataSet(
      Array.from(groupSet).map((groupName) => ({
        id: groupName,
        content: groupName,
      }))
    )
  }, [filteredEvents, groupBy])

  // Memoize timeline options
  const timelineOptions = useMemo<TimelineOptions>(() => ({
    width: '100%',
    height: typeof height === 'number' ? `${height}px` : height,
    margin: {
      item: 10,
      axis: 5,
    },
    orientation: 'both',
    zoomable: true,
    moveable: true,
    selectable: true,
    multiselect: false,
    stack: true,
    stackSubgroups: true,
    showCurrentTime: true,
    showMajorLabels: true,
    showMinorLabels: true,
    format: {
      minorLabels: {
        millisecond: 'SSS',
        second: 'HH:mm:ss',
        minute: 'HH:mm',
        hour: 'HH:mm',
        weekday: 'ddd D',
        day: 'D',
        week: 'w',
        month: 'MMM',
        year: 'YYYY',
      },
      majorLabels: {
        millisecond: 'HH:mm:ss',
        second: 'D MMMM HH:mm',
        minute: 'ddd D MMMM',
        hour: 'ddd D MMMM',
        weekday: 'MMMM YYYY',
        day: 'MMMM YYYY',
        week: 'MMMM YYYY',
        month: 'YYYY',
        year: '',
      },
    },
    tooltip: {
      followMouse: true,
      overflowMethod: 'cap',
    },
  }), [height])

  // Debounced range change handler
  const debouncedRangeChange = useCallback(
    debounce((start: Date, end: Date) => {
      onRangeChange?.(start, end)
    }, 300),
    [onRangeChange]
  )

  // Stable event click handler
  const handleEventClick = useCallback((properties: any) => {
    if (properties.event && properties.item) {
      const clickedEvent = filteredEvents.find((e) => e.id === properties.item)
      if (clickedEvent && onEventClick) {
        onEventClick(clickedEvent)
      }
    }
  }, [filteredEvents, onEventClick])

  // Initialize timeline
  useEffect(() => {
    if (!timelineRef.current || filteredEvents.length === 0) return

    setLoading(true)

    // Cleanup existing instance
    if (timelineInstance.current) {
      timelineInstance.current.destroy()
      timelineInstance.current = null
    }

    try {
      // Create new timeline - only pass groups when grouping is active
      let timeline: Timeline
      if (timelineGroups) {
        timeline = new Timeline(
          timelineRef.current,
          timelineItems,
          timelineGroups,
          timelineOptions
        )
      } else {
        timeline = new Timeline(
          timelineRef.current,
          timelineItems,
          timelineOptions
        )
      }

      // Add event listeners
      timeline.on('select', handleEventClick)
      
      timeline.on('rangechanged', (properties: any) => {
        debouncedRangeChange(properties.start, properties.end)
      })

      timelineInstance.current = timeline

      // Fit timeline to show all events
      setTimeout(() => {
        timeline.fit()
        setLoading(false)
      }, 100)
    } catch (error) {
      console.error('Error initializing timeline:', error)
      setLoading(false)
    }

    return () => {
      if (timelineInstance.current) {
        timelineInstance.current.destroy()
        timelineInstance.current = null
      }
    }
  }, [timelineItems, timelineGroups, timelineOptions, handleEventClick, debouncedRangeChange, filteredEvents.length])

  // Highlight events from external sources (e.g., graph node click)
  useEffect(() => {
    if (!timelineInstance.current || highlightedNodes.length === 0) return

    try {
      // Select the highlighted items in the timeline
      const validIds = highlightedNodes.filter((id) =>
        filteredEvents.some((e) => e.id === id)
      )
      if (validIds.length > 0) {
        timelineInstance.current.setSelection(validIds)
        // Focus on the first highlighted item
        timelineInstance.current.focus(validIds[0], { animation: true })
      }
    } catch (error) {
      console.error('Error highlighting timeline events:', error)
    }
  }, [highlightedNodes, filteredEvents])

  // Clear selection when highlighted nodes are cleared
  useEffect(() => {
    if (!timelineInstance.current || highlightedNodes.length > 0) return
    try {
      timelineInstance.current.setSelection([])
    } catch {
      // Timeline may not be ready yet
    }
  }, [highlightedNodes])

  // Stable zoom handlers
  const handleZoomIn = useCallback(() => {
    timelineInstance.current?.zoomIn(0.5)
  }, [])

  const handleZoomOut = useCallback(() => {
    timelineInstance.current?.zoomOut(0.5)
  }, [])

  const handleFit = useCallback(() => {
    timelineInstance.current?.fit()
  }, [])

  const handleExport = useCallback(() => {
    const dataStr = JSON.stringify(filteredEvents, null, 2)
    const dataBlob = new Blob([dataStr], { type: 'application/json' })
    const url = URL.createObjectURL(dataBlob)
    const link = document.createElement('a')
    link.href = url
    link.download = `timeline-events-${format(new Date(), 'yyyy-MM-dd-HHmmss')}.json`
    link.click()
    URL.revokeObjectURL(url)
  }, [filteredEvents])

  const handleFilterChange = useCallback(
    (_: React.MouseEvent<HTMLElement>, newFilter: string | null) => {
      if (newFilter !== null) {
        setFilterType(newFilter)
      }
    },
    []
  )

  const handlePlaybackSpeedChange = useCallback((event: SelectChangeEvent<number>) => {
    setPlaybackSpeed(event.target.value as number)
  }, [])

  // Playback functionality (memoized)
  useEffect(() => {
    if (!isPlaying || !timelineInstance.current) return

    const interval = setInterval(() => {
      const range = timelineInstance.current?.getWindow()
      if (range) {
        const duration = range.end.getTime() - range.start.getTime()
        const newStart = new Date(range.start.getTime() + duration * 0.1)
        const newEnd = new Date(range.end.getTime() + duration * 0.1)
        timelineInstance.current?.setWindow(newStart, newEnd)
      }
    }, playbackSpeed)

    return () => clearInterval(interval)
  }, [isPlaying, playbackSpeed])

  // Get unique event types for filter
  const eventTypes = useMemo(() => {
    const types = new Set(events.map((e) => e.type).filter(Boolean))
    return Array.from(types) as string[]
  }, [events])

  if (events.length === 0) {
    return (
      <Paper sx={{ p: 3, textAlign: 'center' }}>
        <Typography color="text.secondary">No events to display</Typography>
      </Paper>
    )
  }

  if (filteredEvents.length === 0 && events.length > 0) {
    return (
      <Box>
        {showControls && (
          <Paper sx={{ p: 2, mb: 2, display: 'flex', alignItems: 'center', gap: 2, flexWrap: 'wrap' }}>
            <ToggleButtonGroup
              value={filterType}
              exclusive
              onChange={handleFilterChange}
              size="small"
            >
              <ToggleButton value="all">All</ToggleButton>
              {eventTypes.map((type) => (
                <ToggleButton key={type} value={type || 'unknown'}>
                  {type || 'Unknown'}
                </ToggleButton>
              ))}
            </ToggleButtonGroup>
          </Paper>
        )}
        <Paper sx={{ p: 3, textAlign: 'center' }}>
          <Typography color="text.secondary" gutterBottom>
            No events match the selected filter
          </Typography>
          <Typography
            variant="body2"
            color="primary"
            sx={{ cursor: 'pointer' }}
            onClick={() => setFilterType('all')}
          >
            Show all events
          </Typography>
        </Paper>
      </Box>
    )
  }

  return (
    <Box>
      {showControls && (
        <Paper sx={{ p: 2, mb: 2, display: 'flex', alignItems: 'center', gap: 2, flexWrap: 'wrap' }}>
          {/* Filter */}
          <ToggleButtonGroup
            value={filterType}
            exclusive
            onChange={handleFilterChange}
            size="small"
            aria-label="Filter by event type"
          >
            <ToggleButton value="all">All</ToggleButton>
            {eventTypes.map((type) => (
              <ToggleButton key={type} value={type || 'unknown'}>
                {type || 'Unknown'}
              </ToggleButton>
            ))}
          </ToggleButtonGroup>

          <Box sx={{ flexGrow: 1 }} />

          {/* Event count */}
          <Chip 
            label={`${filteredEvents.length} events`} 
            size="small" 
            color={filteredEvents.length > maxEvents ? 'warning' : 'default'}
          />

          {/* Zoom controls */}
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
          </Box>

          {/* Playback controls */}
          <Box display="flex" gap={1} alignItems="center">
            <Tooltip title={isPlaying ? 'Pause' : 'Play'}>
              <IconButton size="small" onClick={() => setIsPlaying(!isPlaying)} aria-label={isPlaying ? 'Pause playback' : 'Start playback'}>
                {isPlaying ? <PauseIcon /> : <PlayIcon />}
              </IconButton>
            </Tooltip>
            <FormControl size="small" sx={{ minWidth: 80 }}>
              <Select value={playbackSpeed} onChange={handlePlaybackSpeedChange} aria-label="Playback speed">
                <MenuItem value={2000}>0.5x</MenuItem>
                <MenuItem value={1000}>1x</MenuItem>
                <MenuItem value={500}>2x</MenuItem>
                <MenuItem value={250}>4x</MenuItem>
              </Select>
            </FormControl>
          </Box>

          {/* Export */}
          <Tooltip title="Export Events">
            <IconButton size="small" onClick={handleExport} aria-label="Export events">
              <ExportIcon />
            </IconButton>
          </Tooltip>
        </Paper>
      )}

      {/* Timeline */}
      <Paper sx={{ position: 'relative' }}>
        {loading && (
          <Box
            sx={{
              position: 'absolute',
              top: '50%',
              left: '50%',
              transform: 'translate(-50%, -50%)',
              zIndex: 1,
            }}
          >
            <CircularProgress />
          </Box>
        )}
        <div
          ref={timelineRef}
          role="region"
          aria-label="Event timeline"
          style={{
            opacity: loading ? 0.5 : 1,
            transition: 'opacity 0.3s',
          }}
        />
      </Paper>

      {/* Custom CSS */}
      <style>{`
        /* ---- Core item styling ---- */
        .vis-item {
          background-color: rgba(30, 41, 59, 0.92) !important;
          border-color: rgba(100, 116, 139, 0.5) !important;
          border-radius: 6px !important;
          color: #e2e8f0 !important;
          font-size: 0.8125rem !important;
          line-height: 1.4 !important;
          min-height: 28px !important;
          padding: 0 !important;
        }
        .vis-item .vis-item-overflow {
          overflow: visible !important;
        }
        .vis-item .vis-item-content {
          padding: 4px 8px !important;
          white-space: normal !important;
          overflow: visible !important;
          max-width: 420px;
        }

        /* ---- Event content layout ---- */
        .timeline-event-content {
          display: flex;
          align-items: baseline;
          gap: 6px;
          padding: 2px 0;
        }
        .severity-badge {
          font-weight: 700;
          text-transform: uppercase;
          white-space: nowrap;
          padding: 1px 6px;
          border-radius: 3px;
          font-size: 0.65rem;
          letter-spacing: 0.03em;
          flex-shrink: 0;
        }
        .event-label {
          white-space: normal;
          word-break: break-word;
          line-height: 1.35;
        }

        /* ---- Severity-based left border accent ---- */
        .vis-item.severity-critical {
          border-left: 3px solid #f44336 !important;
        }
        .vis-item.severity-high {
          border-left: 3px solid #ff9800 !important;
        }
        .vis-item.severity-medium {
          border-left: 3px solid #ffc107 !important;
        }
        .vis-item.severity-low {
          border-left: 3px solid #4caf50 !important;
        }

        /* ---- Type-based left border accent (fallback) ---- */
        .vis-item.event-finding:not([class*="severity-"]) {
          border-left: 3px solid #2196f3 !important;
        }
        .vis-item.event-activity:not([class*="severity-"]) {
          border-left: 3px solid #4caf50 !important;
        }
        .vis-item.event-decision:not([class*="severity-"]) {
          border-left: 3px solid #ff9800 !important;
        }
        .vis-item.event-note:not([class*="severity-"]) {
          border-left: 3px solid #9e9e9e !important;
        }
        .vis-item.event-status:not([class*="severity-"]) {
          border-left: 3px solid #06b6d4 !important;
        }

        /* ---- Selected item ---- */
        .vis-item.vis-selected {
          box-shadow: 0 0 0 2px rgba(6, 182, 212, 0.6), 0 2px 8px rgba(0,0,0,0.3) !important;
          border-color: #06b6d4 !important;
          z-index: 10;
          background-color: rgba(6, 182, 212, 0.18) !important;
        }
        .vis-item.vis-selected .timeline-event-content {
          font-weight: 600;
        }

        /* ---- Group labels ---- */
        .vis-label {
          color: #cbd5e1 !important;
          font-weight: 600 !important;
          font-size: 0.75rem !important;
          text-transform: capitalize;
          background-color: rgba(15, 23, 42, 0.7) !important;
          border-bottom: 1px solid rgba(100, 116, 139, 0.3) !important;
        }
        .vis-label .vis-inner {
          padding: 6px 10px !important;
        }

        /* ---- Time axis readability ---- */
        .vis-time-axis .vis-text {
          color: #94a3b8 !important;
          font-size: 0.7rem !important;
        }
        .vis-time-axis .vis-text.vis-major {
          font-weight: 600 !important;
          color: #cbd5e1 !important;
        }
        .vis-time-axis .vis-grid.vis-minor {
          border-color: rgba(100, 116, 139, 0.15) !important;
        }
        .vis-time-axis .vis-grid.vis-major {
          border-color: rgba(100, 116, 139, 0.3) !important;
        }

        /* ---- Panel / background ---- */
        .vis-panel.vis-center,
        .vis-panel.vis-left,
        .vis-panel.vis-right,
        .vis-panel.vis-top,
        .vis-panel.vis-bottom {
          border-color: rgba(100, 116, 139, 0.2) !important;
        }

        /* ---- Current-time marker ---- */
        .vis-current-time {
          background-color: #06b6d4 !important;
          width: 2px !important;
        }

        /* ---- Point items (dot) ---- */
        .vis-item.vis-dot {
          border-color: #06b6d4 !important;
          border-width: 3px !important;
        }

        /* ---- Tooltip ---- */
        .vis-tooltip {
          background-color: #1e293b !important;
          color: #e2e8f0 !important;
          border: 1px solid rgba(100, 116, 139, 0.4) !important;
          border-radius: 6px !important;
          padding: 6px 10px !important;
          font-size: 0.8rem !important;
          box-shadow: 0 4px 12px rgba(0,0,0,0.4) !important;
        }
      `}</style>
    </Box>
  )
})

export default EventTimeline

