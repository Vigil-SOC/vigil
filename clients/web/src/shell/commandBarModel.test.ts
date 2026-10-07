import { describe, expect, it } from 'vitest'
import { huntTitle } from './commandBarModel'

describe('huntTitle', () => {
  it('keeps a short hypothesis whole', () => {
    expect(huntTitle('  rare   beacon ')).toBe('rare beacon')
  })

  it('ends on a whole word, within the limit', () => {
    const text = 'a service account key was used from a new network and then read customer-exports'
    const title = huntTitle(text)
    expect(title).toBe('a service account key was used from a new network and then')
    expect(title.length).toBeLessThanOrEqual(60)
    expect(text.startsWith(`${title} `)).toBe(true)
  })

  it('keeps a word that ends exactly at the limit, and drops trailing punctuation', () => {
    expect(huntTitle('alpha beta, gamma delta', 11)).toBe('alpha beta')
    expect(huntTitle('alpha beta gamma', 10)).toBe('alpha beta')
  })

  it('hard-cuts a single word longer than the limit', () => {
    expect(huntTitle('x'.repeat(100), 20)).toBe('x'.repeat(20))
  })
})
