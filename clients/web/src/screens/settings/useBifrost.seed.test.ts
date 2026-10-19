import { describe, expect, it } from 'vitest'
import { isUnsetSeed } from './useBifrost'
import type { BifrostKey, BifrostRoutability, KeyHealth } from '../../services/bifrostApi'

const key = (over: Partial<BifrostKey> = {}): BifrostKey => ({ id: 'k1', name: 'k', models: ['*'], weight: 1, ...over })
const verdict = (health: KeyHealth) =>
  ({ providers: {}, keys: { k1: { provider: 'x', routable: health === 'healthy', health } } }) as BifrostRoutability
const unsetEnv = { value: '', ref: 'env.ANTHROPIC_API_KEY', type: 'env' }

describe('isUnsetSeed', () => {
  it('is the backend’s placeholder verdict, not a reading of the key itself', () => {
    expect(isUnsetSeed(key({ value: unsetEnv }), verdict('placeholder'))).toBe(true)
    // the same key shape with any other verdict is a key the operator can see and fix
    expect(isUnsetSeed(key({ value: unsetEnv }), verdict('unverified'))).toBe(false)
    expect(isUnsetSeed(key({ value: unsetEnv }), verdict('rejected'))).toBe(false)
  })

  it('is not an env key an operator set, nor a key typed in, even a rejected one', () => {
    expect(isUnsetSeed(key({ value: { value: 'sk-a****xyz', ref: 'env.ANTHROPIC_API_KEY', type: 'env' } }), verdict('healthy'))).toBe(false)
    expect(isUnsetSeed(key({ value: { value: 'sk-a****xyz', type: 'plain_text' } }), verdict('rejected'))).toBe(false)
  })

  it('is nothing when the verdict could not be asked for', () => {
    expect(isUnsetSeed(key({ value: unsetEnv }), null)).toBe(false)
    expect(isUnsetSeed(key({ id: 'other', value: unsetEnv }), verdict('placeholder'))).toBe(false)
  })
})
