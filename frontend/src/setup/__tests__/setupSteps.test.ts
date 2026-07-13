// frontend/src/setup/__tests__/setupSteps.test.ts
import { describe, it, expect } from 'vitest'
import {
  SETUP_STEPS,
  DATA_SOURCE_SERVER_IDS,
  emptySetupState,
  type SetupState,
  type SetupStepId,
} from '../setupSteps'
import type { LLMProvider } from '../../services/api'

const ready = (id: SetupStepId, s: SetupState): boolean =>
  SETUP_STEPS.find((step) => step.id === id)!.selectReady(s)

const provider = (over: Partial<LLMProvider> = {}): LLMProvider =>
  ({ is_active: true, is_default: true, ...over }) as LLMProvider

const state = (over: Partial<SetupState> = {}): SetupState => ({ ...emptySetupState(), ...over })

const budget = (vk: string) => ({
  default_vk: vk,
  budget_limit_usd: 0,
  enforcement_mode: 'warning' as const,
})

describe('DATA_SOURCE_SERVER_IDS', () => {
  it('includes catalog sources + mcp-only drift aliases, excludes non-sources', () => {
    // real MCP data-source servers + the drift aliases that would otherwise be
    // false negatives (connection name differs from the catalog id)
    for (const id of [
      'splunk',
      'crowdstrike',
      'sentinelone',
      'azure-sentinel',
      'elastic',
      'splunk-selfhosted',
      'aws-security',
      'gcp-scc',
    ])
      expect(DATA_SOURCE_SERVER_IDS.has(id)).toBe(true)
    // enrichment / output / internal servers must not count as a data source
    for (const id of ['virustotal', 'slack', 'jira', 'approval', 'deeptempo-findings'])
      expect(DATA_SOURCE_SERVER_IDS.has(id)).toBe(false)
  })
})

describe('step readiness predicates', () => {
  it('llm-provider: ready only when a provider is active AND default', () => {
    expect(ready('llm-provider', state({ providers: [provider()] }))).toBe(true)
    expect(ready('llm-provider', state({ providers: [provider({ is_default: false })] }))).toBe(false)
    expect(ready('llm-provider', emptySetupState())).toBe(false)
  })

  it('data-source: ready for a connected source (incl. drift alias), not for non-sources or disconnected', () => {
    expect(ready('data-source', state({ connections: [{ name: 'splunk', connected: true }] }))).toBe(true)
    expect(ready('data-source', state({ connections: [{ name: 'elastic', connected: true }] }))).toBe(true)
    expect(ready('data-source', state({ connections: [{ name: 'splunk', connected: false }] }))).toBe(false)
    expect(
      ready(
        'data-source',
        state({
          connections: [
            { name: 'approval', connected: true },
            { name: 'virustotal', connected: true },
          ],
        }),
      ),
    ).toBe(false)
  })

  it('model-assignment: ready once any component is assigned', () => {
    expect(ready('model-assignment', state({ assignments: { triage: {} as never } }))).toBe(true)
    expect(ready('model-assignment', state({ assignments: {} }))).toBe(false)
  })

  it('cost-guardrails: ready only with a non-empty virtual key', () => {
    expect(ready('cost-guardrails', state({ budget: budget('sk-bf-123') }))).toBe(true)
    expect(ready('cost-guardrails', state({ budget: budget('  ') }))).toBe(false)
    expect(ready('cost-guardrails', state({ budget: null }))).toBe(false)
  })

  it('autonomy: reflects the live orchestrator enabled flag', () => {
    expect(ready('autonomy', state({ orchestratorEnabled: true }))).toBe(true)
    expect(ready('autonomy', state({ orchestratorEnabled: false }))).toBe(false)
  })

  it('empty state leaves every step not-ready', () => {
    for (const step of SETUP_STEPS) expect(step.selectReady(emptySetupState())).toBe(false)
  })
})
