// Row copy for the Settings model table.
// Ids come from core/llm/providers/registry.py COMPONENTS.
export const CHAT_DEFAULT_KEY = 'chat_default'
// Where ai_model_configs.settings keeps a component's same-provider fallback model.
export const FALLBACK_KEY = 'fallback_model_id'

export const COMPONENT_LABELS: Record<string, { label: string; description: string }> = {
  chat_default: {
    label: 'Chat (Ask Vigil)',
    description: 'Default for chat and anything not set below',
  },
  triage: {
    label: 'Triage agent',
    description: 'Runs on every alert: fast and cheap',
  },
  investigation: {
    label: 'Investigation agents',
    description: 'Investigation, correlation, forensics, malware, network',
  },
  summarization: {
    label: 'Summarizing long context',
    description: 'Condenses long documents before a hunt reads them',
  },
  reporting: {
    label: 'Reporting agent',
    description: 'Summaries and briefs',
  },
}

export const AI_CONFIG_DESC =
  'Which models Vigil uses, for which agent, and what happens when one is unavailable. Keys are stored encrypted and never shown again.'
