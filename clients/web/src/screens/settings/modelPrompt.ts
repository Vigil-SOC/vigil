/** Chat (Ask Vigil) is what every unassigned agent shows. With it empty, the
    table reads "Not set" on every row. */
export function chatModelUnset(
  assignments: Record<string, { model_id?: string | null } | undefined> | undefined,
): boolean {
  const id = assignments?.chat_default?.model_id
  return !id || !String(id).trim()
}

/** Settings asks the model table to open Chat's editor and scroll it into view. */
export const PICK_AGENT_MODEL = 'vigil:pick-agent-model'

/** Fired after an assignment is saved or cleared, so the Keys card drops the prompt. */
export const AGENT_MODELS_CHANGED = 'vigil:agent-models-changed'

export function notifyAgentModelsChanged() {
  window.dispatchEvent(new Event(AGENT_MODELS_CHANGED))
}

export function requestAgentModelPick() {
  window.dispatchEvent(new Event(PICK_AGENT_MODEL))
}

export const MODEL_UNSET_NOTE =
  'Every agent is still Not set. Pick a model for Chat (Ask Vigil); the others use it until you choose their own.'

export const KEY_SAVED_UNSET = 'Key saved. Every agent is still Not set — pick a model.'
