// Shared by AI Config settings and the setup wizard, so the two can't drift.
// Ids come from core/llm/providers/registry.py COMPONENTS.
export const CHAT_DEFAULT_KEY = 'chat_default'

export const COMPONENT_LABELS: Record<string, { label: string; description: string }> = {
  chat_default: {
    label: 'Chat (Default)',
    description: 'Fallback for interactive chat and every component below when unset.',
  },
  triage: {
    label: 'Triage Agent',
    description: 'Automated alert triage — cheaper/faster models work well here.',
  },
  investigation: {
    label: 'Investigation Agents',
    description: 'Investigator, Threat Hunter, Correlator, etc. — the heavy lifters.',
  },
  summarization: {
    label: 'Context Summarization',
    description: 'Compresses long conversations — a cheap model is usually fine.',
  },
  reporting: {
    label: 'Report Generation',
    description: 'Reporter agent output — clarity and structure matter more than depth.',
  },
}
