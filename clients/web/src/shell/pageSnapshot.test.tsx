import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { PAGE_SNAPSHOT_MAX, getPageSnapshot, usePageSnapshot } from './pageSnapshot'

function Screen({ k, text }: { k: string; text: string }) {
  usePageSnapshot(k, text)
  return null
}

describe('pageSnapshot', () => {
  it('returns what a mounted screen registered, and forgets it on unmount', () => {
    const view = render(<Screen k="triage" text="Triage queue: 3 items" />)
    expect(getPageSnapshot('triage')).toBe('Triage queue: 3 items')
    expect(getPageSnapshot('overview')).toBeUndefined()
    expect(getPageSnapshot(null)).toBeUndefined()
    view.unmount()
    expect(getPageSnapshot('triage')).toBeUndefined()
  })

  it('sends nothing for an empty snapshot', () => {
    render(<Screen k="empty" text="" />)
    expect(getPageSnapshot('empty')).toBeUndefined()
  })

  it('caps by code points, never leaving half of a surrogate pair', () => {
    const emoji = '\u{1F600}'
    render(<Screen k="long" text={emoji.repeat(PAGE_SNAPSHOT_MAX + 5)} />)
    const out = getPageSnapshot('long') ?? ''
    expect(Array.from(out)).toHaveLength(PAGE_SNAPSHOT_MAX)
    expect(out).toBe(emoji.repeat(PAGE_SNAPSHOT_MAX))
  })
})
