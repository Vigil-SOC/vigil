import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

// The build flag is read at module load, so each case imports the module fresh
// after stubbing — importing once at the top would freeze whichever value ran
// first. `backend` is what GET /api/health reports, or 'down' for no answer.
async function renderWarning(
  devMode: string | undefined,
  backend: boolean | 'down' = false
) {
  vi.resetModules()
  vi.stubEnv('VITE_DEV_MODE', devMode ?? '')
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      backend === 'down'
        ? Promise.reject(new Error('connection refused'))
        : Promise.resolve({ ok: true, json: () => Promise.resolve({ auth_bypassed: backend }) })
    )
  )
  const { default: DevModeWarning } = await import('./DevModeWarning')
  return render(<DevModeWarning />)
}

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('DevModeWarning', () => {
  it('says so when the build was made with the bypass on', async () => {
    await renderWarning('true')

    expect(screen.getByRole('status')).toHaveTextContent(/authentication is bypassed/i)
  })

  // The deployment this exists for: a prebuilt bundle, built with auth on,
  // served by a backend running DEV_MODE=true.
  it('says so when the backend reports the bypass, whatever the build said', async () => {
    await renderWarning('false', true)

    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent(/authentication is bypassed/i)
    )
  })

  it('renders nothing when authentication is on', async () => {
    const { container } = await renderWarning('false')

    await waitFor(() => expect(container).toBeEmptyDOMElement())
  })

  it('renders nothing when the flag is unset', async () => {
    const { container } = await renderWarning(undefined)

    await waitFor(() => expect(container).toBeEmptyDOMElement())
  })

  it('stays quiet when the backend cannot be reached', async () => {
    const { container } = await renderWarning('false', 'down')

    await waitFor(() => expect(container).toBeEmptyDOMElement())
  })

  it('cannot be dismissed', async () => {
    await renderWarning('true')

    // No button, no close affordance: the stripe stays for the session's life.
    expect(screen.queryByRole('button')).toBeNull()
  })

  // The rail is collapsed until someone expands it, and a collapsed item is a
  // 40px square: `.nav-label` is clipped to zero width and the tip only opens
  // on hover. So what an operator sees by default is this element's styling and
  // nothing else, and the warning has to be reachable without a mouse.
  it('carries what makes it legible in a collapsed rail', async () => {
    await renderWarning('true')

    const warning = screen.getByRole('status')
    expect(warning).toHaveClass('dev-mode-warning')
    expect(warning).toHaveAttribute('tabindex', '0')
  })
})
