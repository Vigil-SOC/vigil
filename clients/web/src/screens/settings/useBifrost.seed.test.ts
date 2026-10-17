import { describe, expect, it } from 'vitest'
import { isUnsetSeed } from './useBifrost'
import type { BifrostKey, BifrostRoutability } from '../../services/bifrostApi'

const key = (over: Partial<BifrostKey> = {}): BifrostKey => ({ id: 'k1', name: 'k', models: ['*'], weight: 1, ...over })
const verdict = (routable: boolean) =>
  ({ providers: {}, keys: { k1: { provider: 'x', routable, health: routable ? 'healthy' : 'rejected' } } }) as BifrostRoutability
const unsetEnv = { value: '', ref: 'env.ANTHROPIC_API_KEY', type: 'env' }

describe('isUnsetSeed', () => {
  it('is a seed key whose environment variable is empty', () => {
    expect(isUnsetSeed(key({ value: unsetEnv }), verdict(false))).toBe(true)
    expect(isUnsetSeed(key({ value: unsetEnv }), null)).toBe(true)
  })

  it('is a seed key that resolved but cannot route (Ollama at the default URL, not running)', () => {
    const k = key({ value: { value: '', type: 'plain_text' }, ollama_key_config: { url: { value: 'http****1434', ref: 'env.OLLAMA_URL', type: 'env' } } })
    expect(isUnsetSeed(k, verdict(false))).toBe(true)
    expect(isUnsetSeed(k, verdict(true))).toBe(false)
  })

  it('is not an env key an operator set, nor a key typed in, even a rejected one', () => {
    expect(isUnsetSeed(key({ value: { value: 'sk-a****xyz', ref: 'env.ANTHROPIC_API_KEY', type: 'env' } }), verdict(true))).toBe(false)
    expect(isUnsetSeed(key({ value: { value: 'sk-a****xyz', type: 'plain_text' } }), verdict(false))).toBe(false)
  })

  it('reads the older Bifrost shape too', () => {
    expect(isUnsetSeed(key({ value: { value: '', env_var: 'OPENAI_API_KEY', from_env: true } }), null)).toBe(true)
  })
})
