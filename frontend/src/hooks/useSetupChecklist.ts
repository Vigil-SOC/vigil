// frontend/src/hooks/useSetupChecklist.ts
//
// Soft-checklist state: fetches every domain the setup steps read and runs each
// readiness predicate. Purely additive — the hard gate lives in useSetupStatus /
// SetupGate. `requiredReady` / `incompleteCount` are scaffolding for a planned
// dashboard nudge, currently exercised only by tests.
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
  requiredReady: boolean
  incompleteCount: number
  loading: boolean
  refetch: () => void
}

// Every source fail-opens to its empty default (Promise.allSettled), so one
// flaky endpoint can't crash the page. Advisory — not a security control.
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

  // Flip `loading` only on the initial load, not refetches: a refetch updates the
  // steps in place, and blanking the list to the loader mid-save made it flash.
  const refetch = useCallback(() => {
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
