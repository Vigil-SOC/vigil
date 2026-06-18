import { useCallback, useEffect, useState } from 'react'
import { llmProviderApi, LLMProvider } from '../services/api'

// "Configured" = the user has an active provider marked as the default.
// Requiring is_default (not just is_active) matches the runtime: active-but-no-
// default is exactly where default-resolution fails and chat breaks. We don't
// require an API key here — local providers (Ollama, or an OpenAI-compatible
// server like vLLM/LM Studio) can be keyless; the wizard's Test step is what
// proves a provider actually works.
const isProviderReady = (p: LLMProvider): boolean => p.is_active && p.is_default

export interface SetupStatus {
  configured: boolean
  loading: boolean
  refetch: () => void
}

const useSetupStatus = (): SetupStatus => {
  const [configured, setConfigured] = useState(false)
  const [loading, setLoading] = useState(true)

  const refetch = useCallback(() => {
    setLoading(true)
    llmProviderApi
      .list()
      .then((res) => setConfigured((res.data || []).some(isProviderReady)))
      // Fail open: this gate is UX routing, not a security control (auth is
      // enforced upstream). A transient backend error shouldn't trap an
      // already-configured user behind the wizard. A genuinely fresh install
      // returns an empty list (a success, not an error), so the gate still
      // fires for new users.
      .catch(() => setConfigured(true))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    refetch()
  }, [refetch])

  return { configured, loading, refetch }
}

export default useSetupStatus
