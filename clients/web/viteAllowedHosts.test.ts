import { describe, expect, it } from 'vitest'
import { parseAllowedHosts } from './viteAllowedHosts'

describe('parseAllowedHosts', () => {
  it('leaves Vite its default when the variable is unset or blank', () => {
    expect(parseAllowedHosts(undefined)).toBeUndefined()
    expect(parseAllowedHosts('')).toBeUndefined()
    expect(parseAllowedHosts('  \n ')).toBeUndefined()
    expect(parseAllowedHosts(' , ,')).toBeUndefined()
  })

  it('returns exactly the hosts listed, with nothing added', () => {
    expect(parseAllowedHosts('demoripe.vigilsoc.org')).toEqual(['demoripe.vigilsoc.org'])
  })

  it('splits on commas and whitespace and drops empties', () => {
    expect(parseAllowedHosts(' a.example.com, b.example.com  c.example.com,')).toEqual([
      'a.example.com',
      'b.example.com',
      'c.example.com',
    ])
  })

  it('keeps a leading dot so subdomains can be admitted', () => {
    expect(parseAllowedHosts('.example.com')).toEqual(['.example.com'])
  })

  it('turns the host check off only for the word true', () => {
    expect(parseAllowedHosts('true')).toBe(true)
    expect(parseAllowedHosts(' TRUE ')).toBe(true)
    expect(parseAllowedHosts('true.example.com')).toEqual(['true.example.com'])
  })
})
