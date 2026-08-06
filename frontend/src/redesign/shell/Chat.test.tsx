import { beforeAll, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import Chat from './Chat'
import { streamFetch } from '../../services/api'

vi.mock('./useConversations', () => ({
  useConversations: () => ({ items: [], phase: 'ready', error: null, reload: vi.fn() }),
}))

vi.mock('../../services/notifications', () => ({
  notificationService: { notifyInvestigationComplete: vi.fn() },
}))

vi.mock('../../services/api', () => ({
  agentsApi: { listAgents: vi.fn(() => new Promise(() => undefined)) },
  aiConfigApi: { getConfig: vi.fn(() => new Promise(() => undefined)) },
  analyticsApi: { estimateCost: vi.fn(() => new Promise(() => undefined)) },
  claudeApi: { getModels: vi.fn(() => new Promise(() => undefined)) },
  conversationsApi: {
    get: vi.fn(),
    delete: vi.fn(),
    update: vi.fn(),
    importHistory: vi.fn(),
  },
  mcpApi: { getStatuses: vi.fn(() => new Promise(() => undefined)) },
  reasoningApi: {
    listInteractions: vi.fn(),
    getSessionSummary: vi.fn(),
    getInteraction: vi.fn(),
  },
  streamFetch: vi.fn(),
}))

beforeAll(() => {
  if (window.PointerEvent) return
  class TestPointerEvent extends MouseEvent {
    pointerId: number

    constructor(type: string, init: PointerEventInit = {}) {
      super(type, init)
      this.pointerId = init.pointerId || 0
    }
  }
  Object.defineProperty(window, 'PointerEvent', { configurable: true, value: TestPointerEvent })
})

describe('Vigil Assistant resize controls', () => {
  it('supports keyboard resizing and exposes a reachable close action', () => {
    const onClose = vi.fn()
    const onWidthChange = vi.fn()
    const onWidthCommit = vi.fn()
    render(
      <Chat
        open
        onClose={onClose}
        width={420}
        minWidth={360}
        maxWidth={600}
        onWidthChange={onWidthChange}
        onWidthCommit={onWidthCommit}
      />,
    )

    const separator = screen.getByRole('separator', { name: 'Resize Vigil Assistant' })
    expect(separator).toHaveAttribute('aria-valuemin', '360')
    expect(separator).toHaveAttribute('aria-valuemax', '600')
    expect(separator).toHaveAttribute('aria-valuenow', '420')

    fireEvent.keyDown(separator, { key: 'ArrowLeft' })
    expect(onWidthChange).toHaveBeenLastCalledWith(436)
    expect(onWidthCommit).toHaveBeenLastCalledWith(436)

    fireEvent.keyDown(separator, { key: 'End' })
    expect(onWidthCommit).toHaveBeenLastCalledWith(600)

    fireEvent.keyDown(separator, { key: 'Home' })
    expect(onWidthCommit).toHaveBeenLastCalledWith(360)

    fireEvent.click(screen.getByRole('button', { name: 'Close Vigil Assistant' }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('widens when the left-edge handle is dragged left', () => {
    const onWidthChange = vi.fn()
    const onWidthCommit = vi.fn()
    render(
      <Chat
        open
        onClose={vi.fn()}
        width={420}
        minWidth={360}
        maxWidth={600}
        onWidthChange={onWidthChange}
        onWidthCommit={onWidthCommit}
      />,
    )

    const separator = screen.getByRole('separator', { name: 'Resize Vigil Assistant' })
    fireEvent.pointerDown(separator, { pointerId: 7, button: 0, clientX: 420 })
    fireEvent.pointerMove(separator, { pointerId: 7, clientX: 370 })
    fireEvent.pointerUp(separator, { pointerId: 7, clientX: 370 })

    expect(onWidthChange).toHaveBeenCalledWith(470)
    expect(onWidthCommit).toHaveBeenCalledWith(470)
  })
})

/** SSE Response-like whose body emits `payloads` then HOLDS the stream open
 *  (never resolves) — mirrors the backend pausing on a requires_approval tool,
 *  so the live approval panel (gated behind `loading`) stays mounted to assert. */
function heldSseResponse(payloads: object[]) {
  const text = payloads.map((p) => `data: ${JSON.stringify(p)}\n`).join('\n') + '\n'
  const chunk = new TextEncoder().encode(text)
  let sent = false
  return {
    ok: true,
    body: {
      getReader() {
        return {
          read() {
            if (sent) return new Promise(() => undefined) // hold open
            sent = true
            return Promise.resolve({ done: false, value: chunk })
          },
        }
      },
    },
  } as unknown as Response
}

function sendPrompt(text: string) {
  const textarea = screen.getByPlaceholderText(/Ask Vigil/i)
  fireEvent.change(textarea, { target: { value: text } })
  fireEvent.keyDown(textarea, { key: 'Enter' })
}

describe('tool-approval notice (#413 PR3e)', () => {
  it('shows a live approval notice and deep-links to the queue in-app', async () => {
    vi.mocked(streamFetch).mockResolvedValue(
      heldSseResponse([
        { type: 'approval_required', tool_name: 'isolate_host', action_id: 'ACT-1' },
      ]),
    )
    const onNavigate = vi.fn()
    render(<Chat open onClose={vi.fn()} onNavigate={onNavigate} />)
    sendPrompt('isolate that host')

    // Notice appears WHILE the stream is held (not after completion).
    const notice = await screen.findByText(/needs approval/i)
    expect(notice.textContent).toMatch(/isolate_host/)

    fireEvent.click(screen.getByRole('button', { name: /Pending Approvals/i }))
    expect(onNavigate).toHaveBeenCalledWith('decisions')
  })

  it('falls back to a /decisions link when onNavigate is absent', async () => {
    vi.mocked(streamFetch).mockResolvedValue(
      heldSseResponse([
        { type: 'approval_required', tool_name: 'isolate_host', action_id: 'ACT-2' },
      ]),
    )
    render(<Chat open onClose={vi.fn()} />)
    sendPrompt('isolate that host')

    await screen.findByText(/needs approval/i)
    // No in-app navigator → a plain anchor to the Decisions route.
    const link = screen.getByRole('link', { name: /Pending Approvals/i })
    expect(link).toHaveAttribute('href', '/decisions')
  })

  it('summarizes when several tools pause in one turn', async () => {
    vi.mocked(streamFetch).mockResolvedValue(
      heldSseResponse([
        { type: 'approval_required', tool_name: 'isolate_host', action_id: 'A1' },
        { type: 'approval_required', tool_name: 'block_ip', action_id: 'A2' },
      ]),
    )
    render(<Chat open onClose={vi.fn()} onNavigate={vi.fn()} />)
    sendPrompt('contain the threat')

    expect(await screen.findByText(/2 tools need approval/i)).toBeInTheDocument()
  })
})

describe('budget banner (#413 PR3e-3)', () => {
  it('renders a budget banner on a typed budget_exceeded error event', async () => {
    vi.mocked(streamFetch).mockResolvedValue(
      heldSseResponse([
        {
          type: 'error',
          code: 'budget_exceeded',
          tier: 'virtual_key',
          content: 'Over budget',
        },
      ]),
    )
    render(<Chat open onClose={vi.fn()} onNavigate={vi.fn()} />)
    sendPrompt('run an expensive analysis')

    const banner = await screen.findByRole('alert')
    expect(banner.textContent).toMatch(/budget exceeded/i)
    expect(banner.textContent).toMatch(/virtual_key/)
    // Not surfaced as the generic connectivity error bubble.
    expect(screen.queryByText(/Could not reach Vigil/i)).not.toBeInTheDocument()
  })

  it('words a rate_limit (429) block as a rate limit, not a budget overage', async () => {
    vi.mocked(streamFetch).mockResolvedValue(
      heldSseResponse([
        { type: 'error', code: 'budget_exceeded', tier: 'rate_limit', content: '' },
      ]),
    )
    render(<Chat open onClose={vi.fn()} onNavigate={vi.fn()} />)
    sendPrompt('go')

    const banner = await screen.findByRole('alert')
    expect(banner.textContent).toMatch(/rate limit/i)
    expect(banner.textContent).not.toMatch(/budget exceeded/i)
  })
})
