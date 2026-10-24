import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Cost, describeCost, fmtCost } from './cost'

describe('the one rule for a spend amount', () => {
  it('renders a priced amount as dollars', () => {
    expect(fmtCost(1.23456)).toBe('$1.23')
    expect(fmtCost(1.23456, 'exact', 4)).toBe('$1.2346')
  })

  it('never turns an unpriced cost into a dollar figure or a dash', () => {
    for (const usd of [null, undefined, NaN]) {
      expect(fmtCost(usd)).toBe('not priced')
      expect(fmtCost(usd)).not.toMatch(/[$—]/)
    }
    expect(fmtCost(12.5, 'unknown')).toBe('not priced')
    expect(describeCost({ usd: 0, source: 'unknown' }).kind).toBe('not-priced')
  })

  it('keeps a real zero and a not-billed model apart from each other and from unknown', () => {
    expect(fmtCost(0)).toBe('$0.00')
    expect(fmtCost(0, 'exact', 3)).toBe('$0.000')
    expect(fmtCost(0, 'zero')).toBe('not billed')
    expect(fmtCost(0, 'unknown')).toBe('not priced')
  })

  it('renders the hint as text on the label, pointing at the gateway pricing overrides and the self-hosted attestation', () => {
    render(<Cost usd={0} source="zero" />)
    const el = screen.getByText('not billed')
    expect(el).toHaveAttribute('title', expect.stringMatching(/gateway’s pricing overrides.*config\.self_hosted/))
    expect(el.style.color).toBe('')
  })

  it('keeps a caller title behind the hint on a label', () => {
    render(<Cost usd={null} title="Token count via tiktoken." />)
    expect(screen.getByText('not priced')).toHaveAttribute('title', expect.stringMatching(/config\.self_hosted\) if it costs nothing\. Token count via tiktoken\.$/))
  })
})
