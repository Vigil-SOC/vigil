import { keysApi } from './api'

type Json = Record<string, unknown>

/** The browser refused the key step; the message is for the person. */
export class KeyProblem extends Error {}

/** The browser prompt ended with no key used (cancelled or timed out): not a failure, nothing is sent. */
export class NoKeyUsed extends KeyProblem {
  constructor() {
    super('No key was used. Try again, or use a code instead.')
  }
}

export const keysSupported = () =>
  typeof PublicKeyCredential !== 'undefined' && 'parseRequestOptionsFromJSON' in PublicKeyCredential

// The browser runs one prompt at a time: a new one (or cancelPrompt) ends the one still waiting.
let current: AbortController | null = null
// counts cancellations, so a prompt not yet started (its options still loading) is dropped too
let epoch = 0
export const cancelPrompt = () => {
  epoch++
  current?.abort()
}

// A cancelled or timed-out prompt is "no key used"; anything else is a real error.
async function prompt<T>(ask: (signal: AbortSignal) => Promise<T>): Promise<T> {
  current?.abort()
  const mine = (current = new AbortController())
  try {
    return await ask(mine.signal)
  } catch (e) {
    const name = (e as DOMException)?.name
    if (name === 'NotAllowedError' || name === 'AbortError') throw new NoKeyUsed()
    if (name === 'InvalidStateError') throw new KeyProblem('That key is already added. Use a different one.')
    // anything else from the browser (an address it won't use keys at, no support)
    if (e instanceof DOMException) throw new KeyProblem("Your browser couldn't use a security key here.")
    throw e
  }
}

/** The key's answer to server-made request options (the server's JSON form: `options_to_json`). */
export function assertWith(options: Json): Promise<Json> {
  return prompt(async (signal) => {
    const publicKey = PublicKeyCredential.parseRequestOptionsFromJSON(options as never)
    const cred = (await navigator.credentials.get({ publicKey, signal })) as PublicKeyCredential | null
    if (!cred) throw new NoKeyUsed()
    return cred.toJSON() as Json
  })
}

/** Fresh options for a check ("touch your security key"), then the prompt. */
export async function confirmWithKey(): Promise<{ assertion: Json; challenge: string }> {
  const asked = epoch
  const { data } = await keysApi.assertionOptions()
  if (asked !== epoch) throw new NoKeyUsed()
  return { assertion: await assertWith(data.options), challenge: data.challenge }
}

/** A new key from the browser, for server-made creation options. */
export function createCredential(options: Json): Promise<Json> {
  return prompt(async (signal) => {
    const publicKey = PublicKeyCredential.parseCreationOptionsFromJSON(options as never)
    const cred = (await navigator.credentials.create({ publicKey, signal })) as PublicKeyCredential | null
    if (!cred) throw new NoKeyUsed()
    return cred.toJSON() as Json
  })
}

/** Where the docs page for "keys unavailable" lives. */
export const SECURITY_KEYS_DOCS = 'https://github.com/Vigil-SOC/vigil/blob/main/docs/security-keys.md'
