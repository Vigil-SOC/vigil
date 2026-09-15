import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

// The flag is read at module load, so each case imports the module fresh after
// stubbing — importing once at the top would freeze whichever value ran first.
async function renderWarning(devMode: string | undefined) {
  vi.resetModules()
  if (devMode === undefined) vi.stubEnv('VITE_DEV_MODE', '')
  else vi.stubEnv('VITE_DEV_MODE', devMode)
  const { default: DevModeWarning } = await import('./DevModeWarning')
  return render(<DevModeWarning />)
}

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('DevModeWarning', () => {
  it('says so when authentication is bypassed', async () => {
    await renderWarning('true')

    expect(screen.getByRole('status')).toHaveTextContent(/authentication is bypassed/i)
  })

  it('renders nothing when authentication is on', async () => {
    const { container } = await renderWarning('false')

    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing when the flag is unset', async () => {
    const { container } = await renderWarning(undefined)

    expect(container).toBeEmptyDOMElement()
  })

  it('cannot be dismissed', async () => {
    await renderWarning('true')

    // No button, no close affordance: the stripe stays for the session's life.
    expect(screen.queryByRole('button')).toBeNull()
  })
})
