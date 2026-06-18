import { ReactNode } from 'react'
import { Navigate } from 'react-router-dom'
import { Box, CircularProgress, Typography } from '@mui/material'
import useSetupStatus from '../../hooks/useSetupStatus'

interface Props {
  children: ReactNode
}

// First-access gate: blocks the app until a working LLM provider exists.
// Rendered inside ProtectedRoute (the user is already authenticated) and around
// MainLayout. The /setup route lives OUTSIDE this gate so it stays reachable
// when unconfigured (no redirect loop).
const SetupGate = ({ children }: Props) => {
  const { configured, loading } = useSetupStatus()

  if (loading) {
    return (
      <Box
        sx={{
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          minHeight: '100vh',
          gap: 2,
        }}
      >
        <CircularProgress size={48} />
        <Typography variant="body1" color="text.secondary">
          Loading...
        </Typography>
      </Box>
    )
  }

  if (!configured) {
    return <Navigate to="/setup" replace />
  }

  return <>{children}</>
}

export default SetupGate
