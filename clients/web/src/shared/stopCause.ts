/* Why the model path stopped a run, and the one thing to do next. The agent layer names the
   cause where the call failed; every surface reads the words and the action from this table. */

/** Restates STOP_CAUSES in services/agent/contracts/events.ts, which must match. */
export type StopCause =
  | 'rate_limited'
  | 'provider_auth_refused'
  | 'model_unavailable'
  | 'gateway_budget_refused'
  | 'output_truncated'
  | 'unknown'

const NEW_RUN = { label: 'Start a new run', to: '/workflows' }

interface StopEntry {
  /** The outcome badge's word. */
  badge: string
  sentence: string
  action: { label: string; to: string }
}

export const STOP_CAUSES: Record<StopCause, StopEntry> = {
  rate_limited: {
    badge: 'rate limited',
    sentence: 'The model provider rate limited this run, so it stopped.',
    action: NEW_RUN,
  },
  provider_auth_refused: {
    badge: 'key refused',
    sentence: 'The model provider refused the API key this run used.',
    action: { label: 'Check provider keys', to: '/settings?section=ai-config&tab=keys' },
  },
  model_unavailable: {
    badge: 'model unavailable',
    sentence: 'The model this run uses did not answer or is not available.',
    action: NEW_RUN,
  },
  gateway_budget_refused: {
    badge: 'gateway budget',
    sentence: 'The AI gateway refused the call because its spending limit is used up.',
    action: { label: 'Review the spending limit', to: '/settings?section=ai-config&tab=budgets' },
  },
  output_truncated: {
    badge: 'output cut off',
    sentence: 'The model reached its output limit before it finished answering.',
    action: { label: 'Review the model for this agent', to: '/settings?section=ai-config&tab=assignment' },
  },
  unknown: {
    badge: 'model error',
    sentence: 'A model call failed and Vigil could not tell why.',
    action: NEW_RUN,
  },
}

/** Null reads as today: a run a model call did not stop, or one that ended before causes were recorded.
 *  A word this build has no entry for, from a newer agent, is still a model stop. */
export function stopCauseOf(raw: unknown): StopCause | null {
  if (typeof raw !== 'string' || raw === '') return null
  return Object.keys(STOP_CAUSES).includes(raw) ? (raw as StopCause) : 'unknown'
}
