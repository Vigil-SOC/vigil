import { bifrostApi, keyRefusal } from '../../services/bifrostApi'

/** What a test of a provider's key (or server) showed: a `good` or a `poor` banner. */
export interface Outcome {
  tone: 'good' | 'poor'
  text: string
}

/** "A", "A and B", "A, B and C", then "A, B, C +N" past three. */
export function modelList(names: string[]): string {
  const shown = names.slice(0, 3)
  if (names.length > 3) return `${shown.join(', ')} +${names.length - 3}`
  return shown.length < 2 ? shown.join('') : `${shown.slice(0, -1).join(', ')} and ${shown[shown.length - 1]}`
}

/**
 * The verdict on a key Bifrost has just been given: its own refusal text, or the
 * models the provider now routes. Failing to list models never fails a pass.
 */
export async function checkKey(provider: string, keyId: string | undefined): Promise<Outcome> {
  const refusal = await keyRefusal(keyId)
  if (refusal) return { tone: 'poor', text: `Key stored, but it cannot route: ${refusal}` }
  let names: string[] = []
  try {
    const { data } = await bifrostApi.providerModels(provider)
    names = (data.models || []).map((m) => m.name)
  } catch {
    /* the pass stands without a model list */
  }
  const lead = provider === 'ollama' ? 'Ollama is reachable.' : 'Key works.'
  if (!names.length) return { tone: 'good', text: lead }
  const verb = provider === 'ollama' ? 'installed' : 'available'
  return { tone: 'good', text: `${lead} ${modelList(names)} ${names.length === 1 ? 'is' : 'are'} ${verb}.` }
}
