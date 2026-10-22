import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import DevModeWarning from './DevModeWarning'

describe('DevModeWarning', () => {
  it('says authentication is bypassed', () => {
    render(<DevModeWarning />)

    expect(screen.getByRole('status')).toHaveTextContent(/authentication is bypassed/i)
  })

  it('cannot be dismissed', () => {
    render(<DevModeWarning />)

    // No button, no close affordance: the stripe stays for the session's life.
    expect(screen.queryByRole('button')).toBeNull()
  })

  // The tip is hidden in the header, so what an operator sees by default is
  // this element's styling and nothing else, and the warning has to be
  // reachable without a mouse.
  it('carries what makes it legible in the header', () => {
    render(<DevModeWarning />)

    const warning = screen.getByRole('status')
    expect(warning).toHaveClass('dev-mode-warning')
    expect(warning).toHaveAttribute('tabindex', '0')
  })
})
