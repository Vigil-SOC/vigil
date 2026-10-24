import { describe, expect, it } from 'vitest'
import { errorText } from './errorText'

const res = (data: unknown) => ({ response: { data }, message: 'Request failed with status code 422' })

describe('errorText', () => {
  it('reads detail, then error, then the message, then the fallback', () => {
    expect(errorText(res({ detail: 'nope', error: 'bad' }), 'FB')).toBe('nope')
    expect(errorText({ response: { data: { error: 'Too many' } } }, 'FB')).toBe('Too many')
    expect(errorText(new Error('Network Error'), 'FB')).toBe('Network Error')
    expect(errorText(undefined, 'FB')).toBe('FB')
  })

  it('falls back when the message is not a string', () => {
    expect(errorText({ message: 42 }, 'FB')).toBe('FB')
  })

  it('skips a blank detail rather than showing an empty line', () => {
    expect(errorText({ response: { data: { detail: '   ' } } }, 'FB')).toBe('FB')
  })

  it('never returns a 422 detail list', () => {
    expect(errorText(res({ detail: [{ loc: ['body'], msg: 'x' }] }), 'FB')).toBe('Request failed with status code 422')
  })
})
