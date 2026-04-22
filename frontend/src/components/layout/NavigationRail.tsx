import { useState } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import {
  Box,
  IconButton,
  List,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Tooltip,
  Typography,
  alpha,
  useTheme,
} from '@mui/material'
import {
  Dashboard as DashboardIcon,
  Folder as FolderIcon,
  Psychology as AIIcon,
  Timeline as TimelineIcon,
  Settings as SettingsIcon,
  ChevronLeft,
  ChevronRight,
  Shield,
  Assessment as MetricsIcon,
  BarChart as AnalyticsIcon,
  AttachMoney as CostIcon,
  AccountTree as SkillsIcon,
  Build as BuilderIcon,
  SmartToy as OrchestratorIcon,
} from '@mui/icons-material'
import UserMenu from '../auth/UserMenu'

const COLLAPSED_WIDTH = 64
const EXPANDED_WIDTH = 220

interface NavItem {
  id: string
  label: string
  icon: React.ReactNode
  path: string
}

const navItems: NavItem[] = [
  { id: 'dashboard', label: 'Dashboard', icon: <DashboardIcon />, path: '/' },
  { id: 'cases', label: 'Cases', icon: <FolderIcon />, path: '/cases' },
  { id: 'case-metrics', label: 'Case Metrics', icon: <MetricsIcon />, path: '/case-metrics' },
  { id: 'analytics', label: 'Analytics', icon: <AnalyticsIcon />, path: '/analytics' },
  { id: 'cost-analytics', label: 'LLM Cost', icon: <CostIcon />, path: '/analytics/cost' },
  { id: 'ai-decisions', label: 'AI Decisions', icon: <AIIcon />, path: '/ai-decisions' },
  { id: 'skills', label: 'Skills', icon: <SkillsIcon />, path: '/skills' },
  { id: 'workflow-builder', label: 'Workflow Builder', icon: <BuilderIcon />, path: '/workflow-builder' },
  { id: 'orchestrator', label: 'Auto Ops', icon: <OrchestratorIcon />, path: '/orchestrator' },
  { id: 'timesketch', label: 'Timesketch', icon: <TimelineIcon />, path: '/timesketch' },
  { id: 'settings', label: 'Settings', icon: <SettingsIcon />, path: '/settings' },
]

interface NavigationRailProps {
  enabledIntegrations?: string[]
  orchestratorEnabled?: boolean
}

export default function NavigationRail({ enabledIntegrations = [], orchestratorEnabled = false }: NavigationRailProps) {
  const [expanded, setExpanded] = useState(false)
  const navigate = useNavigate()
  const location = useLocation()
  const theme = useTheme()
  const isDark = theme.palette.mode === 'dark'

  const filteredItems = navItems.filter(item => {
    if (item.id === 'timesketch') return enabledIntegrations.includes('timesketch')
    if (item.id === 'orchestrator') return orchestratorEnabled
    return true
  })

  const width = expanded ? EXPANDED_WIDTH : COLLAPSED_WIDTH

  return (
    <Box
      sx={{
        width,
        minWidth: width,
        height: '100vh',
        display: 'flex',
        flexDirection: 'column',
        bgcolor: isDark ? 'background.default' : 'background.paper',
        borderRight: 1,
        borderColor: 'divider',
        transition: 'width 0.2s ease',
        overflow: 'hidden',
        position: 'fixed',
        left: 0,
        top: 0,
        zIndex: 1200,
      }}
    >
      <Box
        sx={{
          p: 2,
          display: 'flex',
          alignItems: 'center',
          justifyContent: expanded ? 'space-between' : 'center',
          gap: 1.5,
          minHeight: 64,
        }}
      >
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5 }}>
          <Box
            sx={{
              width: 32,
              height: 32,
              borderRadius: 1.5,
              bgcolor: 'primary.main',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              flexShrink: 0,
            }}
          >
            <Shield sx={{ fontSize: 20, color: 'white' }} />
          </Box>
          {expanded && (
            <Typography
              variant="subtitle1"
              sx={{
                fontWeight: 700,
                whiteSpace: 'nowrap',
                overflow: 'hidden',
                letterSpacing: '-0.02em',
              }}
            >
              DeepTempo SOC
            </Typography>
          )}
        </Box>
        {expanded && <UserMenu />}
      </Box>

      <List sx={{ flex: 1, py: 1 }}>
        {filteredItems.map((item) => {
          // Check if the current path and search params match the nav item
          const isActive = item.path.includes('?')
            ? location.pathname + location.search === item.path
            : location.pathname === item.path
          const button = (
            <ListItemButton
              key={item.id}
              selected={isActive}
              onClick={() => navigate(item.path)}
              sx={{
                minHeight: 44,
                justifyContent: expanded ? 'flex-start' : 'center',
                px: expanded ? 2 : 0,
                mx: 1,
                borderRadius: 2,
                '&.Mui-selected': {
                  bgcolor: alpha(theme.palette.primary.main, 0.12),
                  '& .MuiListItemIcon-root': {
                    color: 'primary.main',
                  },
                  '& .MuiListItemText-primary': {
                    color: 'primary.main',
                    fontWeight: 600,
                  },
                },
                '&:hover': {
                  bgcolor: alpha(theme.palette.primary.main, 0.08),
                },
              }}
            >
              <ListItemIcon
                sx={{
                  minWidth: expanded ? 36 : 'auto',
                  color: isActive ? 'primary.main' : 'text.secondary',
                }}
              >
                {item.icon}
              </ListItemIcon>
              {expanded && (
                <ListItemText
                  primary={item.label}
                  primaryTypographyProps={{
                    fontSize: '0.875rem',
                    fontWeight: isActive ? 600 : 500,
                  }}
                />
              )}
            </ListItemButton>
          )

          return expanded ? (
            button
          ) : (
            <Tooltip key={item.id} title={item.label} placement="right" arrow>
              {button}
            </Tooltip>
          )
        })}
      </List>

      <Box sx={{ p: 1, borderTop: 1, borderColor: 'divider' }}>
        <IconButton
          onClick={() => setExpanded(!expanded)}
          sx={{
            width: '100%',
            borderRadius: 2,
            py: 1,
            color: 'text.secondary',
            '&:hover': {
              bgcolor: alpha(theme.palette.primary.main, 0.08),
            },
          }}
        >
          {expanded ? <ChevronLeft /> : <ChevronRight />}
        </IconButton>
      </Box>
    </Box>
  )
}

export { COLLAPSED_WIDTH, EXPANDED_WIDTH }
