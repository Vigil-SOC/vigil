// frontend/src/setup/setupSteps.ts
//
// The setup-checklist registry: pure data + readiness predicates, no JSX.
// This is the design-agnostic core — it survives the coming UI overhaul.
// `useSetupChecklist` feeds these predicates live backend state; the markup
// just renders the results.
import { INTEGRATIONS } from '../config/integrations'
import type { LLMProvider, AIConfigResponse, BudgetSettings } from '../services/api'

// --- Data-source identification -------------------------------------------

// Categories whose connected integrations mean "Vigil is actually being fed
// telemetry to triage." Enrichment (Threat Intelligence), output (Slack/Jira/
// PagerDuty), identity, sandbox, and forensics are deliberately excluded.
export const DATA_SOURCE_CATEGORIES = new Set<string>([
  'SIEM',
  'EDR/XDR',
  'Cloud Security',
  'Network Security',
  'Data Pipeline',
])

// The catalog (config/integrations.ts) is the source of truth for an
// integration's *category*. But MCP *connection* names are mcp-config.json
// keys, which drift from catalog ids for a handful of real data-source
// servers — they connect under a name the catalog never lists, so deriving
// from the catalog alone would silently miss them (false negative). Keep this
// in sync whenever mcp-config.json gains a data-source server whose key differs
// from its catalog id. (Verified against mcp-config.json on 2026-06-18.)
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

// One snapshot of everything the checklist derives from. useSetupChecklist
// builds this from the live APIs (each source fail-open to an empty default).
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

// Onboarding gating tier:
//  - 'required'    → hard gate; blocks entry to the app (today: only the LLM provider)
//  - 'recommended' → strongly nudged but skippable (data source — a SOC needs telemetry,
//                    but it can also arrive via DeepTempo's pipeline / demo / upload)
//  - 'optional'    → pure nice-to-have
export type SetupTier = 'required' | 'recommended' | 'optional'

// The Settings section a step's "Configure →" action opens. Both shells use the
// same section vocabulary — today's MUI app (TAB_DEFS → /settings?tab=<key>) and
// the redesign (SettingsScreen SECTIONS → setActive(<key>)) — so we store the
// shell-agnostic key here and let each shell build its own navigation. Keeps this
// registry portable when the redesign takes over the protected routes.
export type SettingsSection = 'ai-config' | 'integrations' | 'autoinvestigate'

export interface SetupStep {
  id: SetupStepId
  label: string
  description: string
  tier: SetupTier // gating tier; only 'required' steps drive the hard gate
  settingsSection: SettingsSection // which Settings section "Configure →" opens
  selectReady: (s: SetupState) => boolean
}

export const SETUP_STEPS: SetupStep[] = [
  {
    id: 'llm-provider',
    label: 'Connect an AI provider',
    description: 'Triage, investigation, and chat all run on it.',
    tier: 'required',
    settingsSection: 'ai-config',
    // Mirrors useSetupStatus.isProviderReady (kept in sync intentionally):
    // active + default, no key required (local/keyless providers are valid).
    selectReady: (s) => s.providers.some((p) => p.is_active && p.is_default),
  },
  {
    id: 'data-source',
    label: 'Connect a data source',
    description: 'A SIEM or EDR so Vigil has alerts to triage.',
    tier: 'recommended',
    settingsSection: 'integrations',
    selectReady: (s) =>
      s.connections.some((c) => c.connected && DATA_SOURCE_SERVER_IDS.has(c.name)),
  },
  {
    id: 'model-assignment',
    label: 'Assign models to agents',
    description: 'Pick fast vs. strong models per task — defaults work.',
    tier: 'optional',
    settingsSection: 'ai-config',
    selectReady: (s) => Object.keys(s.assignments ?? {}).length > 0,
  },
  {
    id: 'cost-guardrails',
    label: 'Set cost guardrails',
    description: 'A Bifrost virtual key + spend cap.',
    tier: 'optional',
    settingsSection: 'ai-config',
    selectReady: (s) => !!s.budget?.default_vk?.trim(),
  },
  {
    id: 'autonomy',
    label: 'Enable autonomous mode',
    description: 'Let Vigil triage and investigate 24/7, within your cost caps.',
    tier: 'optional',
    settingsSection: 'autoinvestigate',
    selectReady: (s) => s.orchestratorEnabled,
  },
]
