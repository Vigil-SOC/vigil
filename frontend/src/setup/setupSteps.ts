// frontend/src/setup/setupSteps.ts
//
// The setup-checklist registry: pure data + readiness predicates, no JSX.
// useSetupChecklist feeds these predicates live backend state.
import { INTEGRATIONS } from '../config/integrations'
import type { LLMProvider, AIConfigResponse, BudgetSettings } from '../services/api'

// --- Data-source identification -------------------------------------------

// Categories whose connected integrations mean Vigil is actually being fed
// telemetry. Enrichment / output / identity / sandbox / forensics are excluded.
export const DATA_SOURCE_CATEGORIES = new Set<string>([
  'SIEM',
  'EDR/XDR',
  'Cloud Security',
  'Network Security',
  'Data Pipeline',
])

// MCP connection names are mcp-config.json keys, which drift from catalog ids
// for a few real data-source servers. Keep in sync when mcp-config.json gains a
// data-source server whose key differs from its catalog id. (Verified 2026-06-18.)
export const MCP_ONLY_DATA_SOURCE_IDS = [
  'elastic', // catalog id: elastic-siem (SIEM)
  'splunk-selfhosted', // SIEM, no catalog entry
  'aws-security', // catalog id: aws-security-hub (Cloud Security)
  'gcp-scc', // catalog id: gcp-security (Cloud Security)
] as const

export const DATA_SOURCE_SERVER_IDS = new Set<string>([
  ...INTEGRATIONS.filter((i) => DATA_SOURCE_CATEGORIES.has(i.category)).map((i) => i.id),
  ...MCP_ONLY_DATA_SOURCE_IDS,
])

// --- Normalized backend state the predicates read -------------------------

export interface McpConnection {
  name: string
  connected: boolean
}

// One snapshot of everything the checklist derives from (each source fail-open).
export interface SetupState {
  providers: LLMProvider[]
  connections: McpConnection[]
  assignments: AIConfigResponse['assignments']
  budget: BudgetSettings | null
  orchestratorEnabled: boolean
}

export const emptySetupState = (): SetupState => ({
  providers: [],
  connections: [],
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

// Gating tier: 'required' drives the hard gate (today: only the LLM provider);
// 'recommended' is strongly nudged but skippable; 'optional' is nice-to-have.
export type SetupTier = 'required' | 'recommended' | 'optional'

// Shell-agnostic Settings section key — each shell builds its own navigation to it.
export type SettingsSection = 'ai-config' | 'integrations' | 'autoinvestigate'

export interface SetupStep {
  id: SetupStepId
  label: string
  description: string
  // Status word shown as a tag once the step is satisfied (replaces the button).
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
    selectReady: (s) =>
      s.connections.some((c) => c.connected && DATA_SOURCE_SERVER_IDS.has(c.name)),
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
