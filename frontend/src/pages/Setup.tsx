import { useState } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import {
  Box,
  Card,
  CardContent,
  Container,
  Alert,
  Typography,
  CircularProgress,
} from '@mui/material'
import { useAuth } from '../contexts/AuthContext'
import useSetupStatus from '../hooks/useSetupStatus'
import ProviderConfigSteps from '../components/settings/ProviderConfigSteps'

// First-access wizard. Reached when SetupGate finds no working LLM provider.
// Lives outside SetupGate so it's always reachable; redirects home once a
// provider is configured.
const Setup = () => {
  const navigate = useNavigate()
  const { hasPermission } = useAuth()
  const { configured, loading } = useSetupStatus()
  const [error, setError] = useState<string | null>(null)

  const canConfigure = hasPermission('settings.write')

  if (loading) {
    return (
      <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'center', minHeight: '100vh' }}>
        <CircularProgress size={48} />
      </Box>
    )
  }

  // Already set up (or a transient error failed open) — don't show the wizard.
  if (configured) {
    return <Navigate to="/" replace />
  }

  // finalSave awaits before calling onSaved, so the provider exists by now.
  // SetupGate re-checks readiness on navigation and will admit us to the app.
  const handleSaved = () => navigate('/', { replace: true })

  return (
    <Container maxWidth="sm" sx={{ py: 6 }}>
      <Box sx={{ mb: 4, textAlign: 'center' }}>
        <Typography variant="h4" gutterBottom>
          Welcome to Vigil
        </Typography>
        <Typography variant="body1" color="text.secondary">
          Let&apos;s connect an AI provider to get started. Vigil&apos;s triage,
          investigation, and chat all run on it — it&apos;s the one thing required
          before you can use the platform.
        </Typography>
      </Box>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      <Card>
        <CardContent>
          {canConfigure ? (
            <ProviderConfigSteps
              existing={null}
              forceDefault
              initialProviderType="ollama"
              onSaved={handleSaved}
              onError={setError}
            />
          ) : (
            <Alert severity="info">
              Vigil isn&apos;t set up yet. Ask an administrator to add an AI provider
              in Settings → AI Config.
            </Alert>
          )}
        </CardContent>
      </Card>
    </Container>
  )
}

export default Setup
