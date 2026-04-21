import { useState, useEffect } from 'react'
import { Outlet } from 'react-router-dom'
import { Box, IconButton, Tooltip, alpha, useTheme as useMuiTheme } from '@mui/material'
import { Chat as ChatIcon, Brightness4, Brightness7 } from '@mui/icons-material'
import { useTheme } from '../../contexts/ThemeContext'
import NavigationRail, { COLLAPSED_WIDTH } from './NavigationRail'
import ClaudeDrawer from '../claude/ClaudeDrawer'
import { configApi, orchestratorApi } from '../../services/api'

export default function MainLayout() {
  const [claudeOpen, setClaudeOpen] = useState(false)
  const [investigationData, setInvestigationData] = useState<{
    messages: Array<{ role: 'user' | 'assistant'; content: string }>
    agentId: string
    title: string
  } | null>(null)
  const [enabledIntegrations, setEnabledIntegrations] = useState<string[]>([])
  const [orchestratorEnabled, setOrchestratorEnabled] = useState(false)
  const { mode, toggleTheme } = useTheme()
  const muiTheme = useMuiTheme()

  useEffect(() => {
    configApi.getIntegrations()
      .then(res => setEnabledIntegrations(res.data?.enabled_integrations || []))
      .catch(() => setEnabledIntegrations([]))
    orchestratorApi.getStatus()
      .then(res => setOrchestratorEnabled(res.data?.enabled ?? false))
      .catch(() => setOrchestratorEnabled(false))
  }, [])

  const handleInvestigate = (_findingId: string, agentId: string, prompt: string, title: string) => {
    setInvestigationData({
      messages: [{ role: 'user' as const, content: prompt }],
      agentId,
      title,
    })
    setClaudeOpen(true)
  }

  return (
    <Box sx={{ display: 'flex', minHeight: '100vh', bgcolor: 'background.default' }}>
      <NavigationRail enabledIntegrations={enabledIntegrations} orchestratorEnabled={orchestratorEnabled} />
      
      <Box
        component="main"
        sx={{
          flex: 1,
          ml: `${COLLAPSED_WIDTH}px`,
          minHeight: '100vh',
          display: 'flex',
          flexDirection: 'column',
        }}
      >
        <Box
          sx={{
            position: 'fixed',
            top: 12,
            right: 12,
            zIndex: 1100,
            display: 'flex',
            gap: 0.5,
          }}
        >
          <Tooltip title="Vigil Chat">
            <IconButton
              onClick={() => setClaudeOpen(!claudeOpen)}
              sx={{
                bgcolor: alpha(muiTheme.palette.primary.main, 0.1),
                color: 'primary.main',
                '&:hover': {
                  bgcolor: alpha(muiTheme.palette.primary.main, 0.2),
                },
              }}
            >
              <ChatIcon />
            </IconButton>
          </Tooltip>
          <Tooltip title={`Switch to ${mode === 'dark' ? 'light' : 'dark'} mode`}>
            <IconButton
              onClick={toggleTheme}
              sx={{
                bgcolor: alpha(muiTheme.palette.text.primary, 0.05),
                '&:hover': {
                  bgcolor: alpha(muiTheme.palette.text.primary, 0.1),
                },
              }}
            >
              {mode === 'dark' ? <Brightness7 /> : <Brightness4 />}
            </IconButton>
          </Tooltip>
        </Box>

        <Box sx={{ flex: 1, p: 3, pt: 2 }}>
          <Outlet context={{ handleInvestigate }} />
        </Box>
      </Box>

      <ClaudeDrawer
        open={claudeOpen}
        onClose={() => {
          setClaudeOpen(false)
          setInvestigationData(null)
        }}
        initialMessages={investigationData?.messages}
        initialAgentId={investigationData?.agentId}
        initialTitle={investigationData?.title}
      />
    </Box>
  )
}
