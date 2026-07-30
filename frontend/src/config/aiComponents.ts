// frontend/src/config/aiComponents.ts
//
// Display labels for the backend AI components (ids: services/model_registry.py
// COMPONENTS). Shared by AI Config settings + the setup wizard so they can't drift.
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
  orchestrator_plan: {
    label: 'Orchestrator — Planning',
    description: 'Generates the investigation plan from the initial finding.',
  },
  orchestrator_review: {
    label: 'Orchestrator — Review',
    description: 'Reviews and approves sub-agent output at the end of an investigation.',
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
