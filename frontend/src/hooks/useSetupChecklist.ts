// frontend/src/hooks/useSetupChecklist.ts
//
// Soft-checklist state: fetches every domain the setup steps care about and
// runs each step's readiness predicate. Purely additive — the hard gate still
// lives in useSetupStatus / SetupGate; this only drives the (dismissible,
// non-blocking) checklist + nudge.
import { useCallback, useEffect, useState } from 'react'
import { llmProviderApi, mcpApi, aiConfigApi, budgetsApi, configApi } from '../services/api'
import {
  SETUP_STEPS,
  emptySetupState,
  type SetupState,
  type SetupStep,
  type McpConnection,
} from '../setup/setupSteps'

export interface ChecklistStep extends SetupStep {
  ready: boolean
}

export interface SetupChecklist {
  steps: ChecklistStep[]
  requiredReady: boolean // all required steps satisfied (today: just the LLM)
  incompleteCount: number // optional steps still not ready — drives the nudge
  loading: boolean
  refetch: () => void
}

// Pull the live state behind the checklist. Every call fail-opens to its empty
// default (Promise.allSettled), so one flaky endpoint can't crash the page or
// hide the rest. This is advisory — not a security control.
const fetchSetupState = async (): Promise<SetupState> => {
  const base = emptySetupState()
  const [providers, connections, aiConfig, budget, orchestrator] = await Promise.allSettled([
    llmProviderApi.list(),
    mcpApi.getConnections(),
    aiConfigApi.getConfig(),
    budgetsApi.get(),
    configApi.getOrchestrator(),
  ])

  if (providers.status === 'fulfilled') base.providers = providers.value.data || []
  if (connections.status === 'fulfilled')
    base.connections = (connections.value.data?.connections ?? []).map((c: McpConnection) => ({
      name: c.name,
      connected: !!c.connected,
    }))
  if (aiConfig.status === 'fulfilled') base.assignments = aiConfig.value.data?.assignments ?? {}
  if (budget.status === 'fulfilled') base.budget = budget.value.data ?? null
  if (orchestrator.status === 'fulfilled')
    base.orchestratorEnabled = !!orchestrator.value.data?.enabled

  return base
}

const useSetupChecklist = (): SetupChecklist => {
  const [state, setState] = useState<SetupState>(emptySetupState)
  const [loading, setLoading] = useState(true)

  const refetch = useCallback(() => {
    setLoading(true)
    fetchSetupState()
      .then(setState)
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    refetch()
  }, [refetch])

  const steps: ChecklistStep[] = SETUP_STEPS.map((step) => ({
    ...step,
    ready: step.selectReady(state),
  }))
  const requiredReady = steps.every((s) => s.tier !== 'required' || s.ready)
  const incompleteCount = steps.filter((s) => s.tier !== 'required' && !s.ready).length

  return { steps, requiredReady, incompleteCount, loading, refetch }
}

export default useSetupChecklist
