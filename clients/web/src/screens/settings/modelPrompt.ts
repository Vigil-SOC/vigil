/** Chat (Ask Vigil) is what an agent with no assignment of its own shows.
    With Chat empty, those rows read Not set. An agent that already has its
    own model does not. */
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
  'Chat is still Not set. Pick a model for Chat (Ask Vigil); every agent without its own model uses it.'

export const KEY_SAVED_UNSET = 'Key saved. Chat is still Not set — pick a model.'
