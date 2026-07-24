// frontend/src/setup/setupSteps.ts
//
// The setup-checklist registry: step data + readiness predicates (no JSX).
import { INTEGRATIONS } from '../config/integrations'
import type { LLMProvider, AIConfigResponse, BudgetSettings } from '../services/api'

// --- Data-source identification -------------------------------------------

// Categories whose configured integrations mean Vigil is actually being fed
// telemetry. Enrichment / output / identity / sandbox / forensics are excluded.
export const DATA_SOURCE_CATEGORIES = new Set<string>([
  'SIEM',
  'EDR/XDR',
  'Cloud Security',
  'Network Security',
  'Data Pipeline',
])

// Catalog ids of every data-source integration. Readiness keys off the user's
// enabled_integrations (catalog ids) — NOT live MCP connectivity: several
// data-source servers are keyless stdio processes that boot (and so report
// "connected") with no credentials, which would flip this step to done before
// the user connected anything.
export const DATA_SOURCE_CATALOG_IDS = new Set<string>(
  INTEGRATIONS.filter((i) => DATA_SOURCE_CATEGORIES.has(i.category)).map((i) => i.id),
)

// --- Normalized backend state the predicates read -------------------------

export interface SetupState {
  providers: LLMProvider[]
  enabledIntegrations: string[]
  assignments: AIConfigResponse['assignments']
  budget: BudgetSettings | null
  orchestratorEnabled: boolean
}

export const emptySetupState = (): SetupState => ({
  providers: [],
  enabledIntegrations: [],
  assignments: {},
  budget: null,
  orchestratorEnabled: false,
})

// --- The step registry ----------------------------------------------------

export type SetupStepId =
  | 'llm-provider'
  | 'data-source'
  | 'model-assignment'
  | 'cost-guardrails'
  | 'autonomy'

// 'required' drives the hard gate; 'recommended' is nudged but skippable.
export type SetupTier = 'required' | 'recommended' | 'optional'

export type SettingsSection = 'ai-config' | 'integrations' | 'autoinvestigate'

export interface SetupStep {
  id: SetupStepId
  label: string
  description: string
  doneLabel: string
  tier: SetupTier
  settingsSection: SettingsSection
  selectReady: (s: SetupState) => boolean
}

export const SETUP_STEPS: SetupStep[] = [
  {
    id: 'llm-provider',
    label: 'Connect an AI provider',
    description: 'Triage, investigation, and chat all run on it.',
    doneLabel: 'Connected',
    tier: 'required',
    settingsSection: 'ai-config',
    // Mirrors useSetupStatus.isProviderReady (kept in sync): active + default,
    // no key required (local/keyless providers are valid).
    selectReady: (s) => s.providers.some((p) => p.is_active && p.is_default),
  },
  {
    id: 'data-source',
    label: 'Connect a data source',
    description: 'A SIEM or EDR so Vigil has alerts to triage.',
    doneLabel: 'Connected',
    tier: 'recommended',
    settingsSection: 'integrations',
    selectReady: (s) => s.enabledIntegrations.some((id) => DATA_SOURCE_CATALOG_IDS.has(id)),
  },
  {
    id: 'model-assignment',
    label: 'Assign models to agents',
    description: 'Pick fast vs. strong models per task — defaults work.',
    doneLabel: 'Configured',
    tier: 'optional',
    settingsSection: 'ai-config',
    selectReady: (s) => Object.keys(s.assignments ?? {}).length > 0,
  },
  {
    id: 'cost-guardrails',
    label: 'Set cost guardrails',
    description: 'Cap how much Vigil spends each month.',
    doneLabel: 'Configured',
    tier: 'optional',
    settingsSection: 'ai-config',
    selectReady: (s) => !!s.budget?.default_vk?.trim(),
  },
  {
    id: 'autonomy',
    label: 'Enable autonomous mode',
    description: 'Let Vigil triage and investigate 24/7, within your cost caps.',
    doneLabel: 'Enabled',
    tier: 'optional',
    settingsSection: 'autoinvestigate',
    selectReady: (s) => s.orchestratorEnabled,
  },
]
