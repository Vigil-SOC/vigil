// frontend/src/setup/__tests__/setupSteps.test.ts
import { describe, it, expect } from 'vitest'
import {
  SETUP_STEPS,
  DATA_SOURCE_CATALOG_IDS,
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

describe('DATA_SOURCE_CATALOG_IDS', () => {
  it('includes catalog data sources, excludes enrichment / identity / internal', () => {
    for (const id of [
      'splunk',
      'crowdstrike',
      'sentinelone',
      'azure-sentinel',
      'elastic-siem',
      'aws-security-hub',
      'gcp-security',
    ])
      expect(DATA_SOURCE_CATALOG_IDS.has(id)).toBe(true)
    // enrichment / identity / reference integrations must not count as a data source
    for (const id of ['virustotal', 'okta', 'github', 'shodan'])
      expect(DATA_SOURCE_CATALOG_IDS.has(id)).toBe(false)
  })
})

describe('step readiness predicates', () => {
  it('llm-provider: ready only when a provider is active AND default', () => {
    expect(ready('llm-provider', state({ providers: [provider()] }))).toBe(true)
    expect(ready('llm-provider', state({ providers: [provider({ is_default: false })] }))).toBe(false)
    expect(ready('llm-provider', emptySetupState())).toBe(false)
  })

  it('data-source: ready once a data-source integration is enabled, not for non-sources', () => {
    expect(ready('data-source', state({ enabledIntegrations: ['splunk'] }))).toBe(true)
    expect(ready('data-source', state({ enabledIntegrations: ['elastic-siem'] }))).toBe(true)
    expect(ready('data-source', state({ enabledIntegrations: [] }))).toBe(false)
    expect(ready('data-source', state({ enabledIntegrations: ['virustotal', 'okta'] }))).toBe(false)
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
