// frontend/src/hooks/__tests__/useSetupChecklist.test.tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

vi.mock('../../services/api', () => ({
  llmProviderApi: { list: vi.fn() },
  mcpApi: { getConnections: vi.fn() },
  aiConfigApi: { getConfig: vi.fn() },
  budgetsApi: { get: vi.fn() },
  configApi: { getOrchestrator: vi.fn() },
}))

import useSetupChecklist from '../useSetupChecklist'
import { llmProviderApi, mcpApi, aiConfigApi, budgetsApi, configApi } from '../../services/api'

// The repo has no renderHook; drive the hook through a tiny component and
// assert on its rendered derived state.
const Harness = () => {
  const { steps, requiredReady, incompleteCount, loading } = useSetupChecklist()
  if (loading) return <div>loading</div>
  return (
    <div>
      <div data-testid="required">{String(requiredReady)}</div>
      <div data-testid="incomplete">{incompleteCount}</div>
      {steps.map((s) => (
        <div key={s.id} data-testid={`step-${s.id}`}>
          {s.ready ? 'ready' : 'not'}
        </div>
      ))}
    </div>
  )
}

const ok = (data: unknown) => ({ data })

describe('useSetupChecklist', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    // Baseline: LLM provider ready, the four optional steps unconfigured.
    ;(llmProviderApi.list as any).mockResolvedValue(ok([{ is_active: true, is_default: true }]))
    ;(mcpApi.getConnections as any).mockResolvedValue(ok({ connections: [] }))
    ;(aiConfigApi.getConfig as any).mockResolvedValue(ok({ components: [], assignments: {} }))
    ;(budgetsApi.get as any).mockResolvedValue(
      ok({ default_vk: '', budget_limit_usd: 0, enforcement_mode: 'warning' }),
    )
    ;(configApi.getOrchestrator as any).mockResolvedValue(ok({ enabled: false }))
  })

  it('computes requiredReady independently of optional gaps', async () => {
    render(<Harness />)
    await waitFor(() => expect(screen.getByTestId('required')).toHaveTextContent('true'))
    expect(screen.getByTestId('incomplete')).toHaveTextContent('4') // the four optional steps
    expect(screen.getByTestId('step-llm-provider')).toHaveTextContent('ready')
    expect(screen.getByTestId('step-data-source')).toHaveTextContent('not')
  })

  it('fails open per-source: a rejected endpoint does not crash the rest', async () => {
    (mcpApi.getConnections as any).mockRejectedValue(new Error('boom'))
    ;(configApi.getOrchestrator as any).mockResolvedValue(ok({ enabled: true }))

    render(<Harness />)
    await waitFor(() => expect(screen.getByTestId('required')).toHaveTextContent('true'))
    expect(screen.getByTestId('step-data-source')).toHaveTextContent('not') // rejected → empty default
    expect(screen.getByTestId('step-autonomy')).toHaveTextContent('ready') // unaffected source still derived
  })
})
