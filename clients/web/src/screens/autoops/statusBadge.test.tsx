import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { PrioBadge } from './statusBadge'

describe('PrioBadge', () => {
  it('paints unknown with the faint token', () => {
    render(<PrioBadge prio="unknown" />)
    expect(screen.getByText('unknown')).toHaveStyle({ color: 'var(--tx-faint)' })
  })

  it('does not paint an unrecognized name as unknown', () => {
    render(<PrioBadge prio="urgent" />)
    expect(screen.getByText('urgent')).toHaveStyle({ color: 'var(--tx-3)' })
  })
})
