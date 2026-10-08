import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import McpSurfacePanel from './McpSurfacePanel'
import { mcpApi } from '../../services/api'

vi.mock('../../services/api', () => ({
  mcpApi: {
    getSurface: vi.fn(),
    setSurfaceEnabled: vi.fn(),
    mintCredential: vi.fn(),
    revokeCredential: vi.fn(),
  },
}))

const cred = {
  credential_id: 'c1',
  label: 'the platform',
  created_at: '2026-09-12T10:00:00Z',
  last_used_at: null,
  expires_at: null,
}
const surface = (over = {}) => ({ data: { enabled: true, path: '/mcp', credentials: [cred], ...over } })

function setup() {
  const notify = vi.fn()
  render(<McpSurfacePanel notify={notify} />)
  return notify
}

describe('McpSurfacePanel', () => {
  beforeEach(() => {
    vi.mocked(mcpApi.getSurface).mockResolvedValue(surface() as never)
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.clearAllMocks()
  })

  it('says it is loading, then lists the credential with its never-used and no-expiry wording', async () => {
    setup()
    expect(screen.getByRole('status')).toHaveTextContent('Loading the MCP server')
    expect(await screen.findByText('the platform')).toBeInTheDocument()
    expect(screen.getByText('never used')).toBeInTheDocument()
    expect(screen.getByText('does not expire')).toBeInTheDocument()
    expect(screen.getByRole('switch', { name: 'Vigil MCP server' })).toHaveAttribute('aria-checked', 'true')
  })

  it('shows an error with Retry instead of a blank panel', async () => {
    vi.mocked(mcpApi.getSurface).mockRejectedValueOnce(new Error('boom'))
    const notify = setup()
    expect(await screen.findByRole('alert')).toHaveTextContent('Couldn’t load the MCP server settings')
    expect(notify).toHaveBeenCalledWith('err', 'Could not read the MCP surface settings')
    fireEvent.click(screen.getByRole('button', { name: /Retry/ }))
    expect(await screen.findByText('the platform')).toBeInTheDocument()
  })

  it('shows the empty state and the no-credential warning when on with none', async () => {
    vi.mocked(mcpApi.getSurface).mockResolvedValue(surface({ credentials: [] }) as never)
    setup()
    expect(await screen.findByText('No credentials yet')).toBeInTheDocument()
    expect(screen.getByText(/no credential exists/)).toBeInTheDocument()
  })

  it('turns the surface on and off from the switch', async () => {
    vi.mocked(mcpApi.getSurface).mockResolvedValue(surface({ enabled: false }) as never)
    vi.mocked(mcpApi.setSurfaceEnabled).mockResolvedValue({} as never)
    const notify = setup()
    fireEvent.click(await screen.findByRole('switch', { name: 'Vigil MCP server' }))
    await waitFor(() => expect(mcpApi.setSurfaceEnabled).toHaveBeenCalledWith(true))
    await waitFor(() => expect(notify).toHaveBeenCalledWith('info', expect.stringContaining('MCP surface is on')))
  })

  it('revokes only after the full hold, never on a click or an early release', async () => {
    vi.mocked(mcpApi.revokeCredential).mockResolvedValue({} as never)
    const notify = setup()
    const button = await screen.findByRole('button', { name: /^Hold to revoke/ })
    vi.useFakeTimers()

    fireEvent.click(button)
    fireEvent.pointerDown(button)
    act(() => void vi.advanceTimersByTime(800))
    fireEvent.pointerUp(button)
    act(() => void vi.advanceTimersByTime(1600))
    expect(mcpApi.revokeCredential).not.toHaveBeenCalled()

    fireEvent.pointerDown(button)
    await act(async () => void vi.advanceTimersByTime(1600))
    expect(mcpApi.revokeCredential).toHaveBeenCalledWith('c1')
    expect(notify).toHaveBeenCalledWith('ok', 'Credential revoked')
  })
})
