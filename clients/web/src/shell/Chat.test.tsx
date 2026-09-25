import { beforeAll, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import Chat from './Chat'
import { reasoningApi } from '../services/api'

vi.mock('./useConversations', () => ({
  useConversations: () => ({ items: [], phase: 'ready', error: null, reload: vi.fn() }),
}))

vi.mock('../services/notifications', () => ({
  notificationService: { notifyInvestigationComplete: vi.fn() },
}))

vi.mock('../services/api', () => ({
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

describe('reasoning trace cost', () => {
  it('shows an all-unpriced session as not priced', async () => {
    vi.mocked(reasoningApi.getSessionSummary).mockResolvedValue({
      total_interactions: 1,
      total_cost_usd: null,
      unpriced_calls: 1,
      total_input_tokens: 10,
      total_output_tokens: 4,
    })
    vi.mocked(reasoningApi.listInteractions).mockResolvedValue({ interactions: [] })

    render(
      <Chat
        open
        onClose={vi.fn()}
        width={420}
        minWidth={360}
        maxWidth={600}
        onWidthChange={vi.fn()}
        onWidthCommit={vi.fn()}
      />,
    )

    fireEvent.click(screen.getByTitle('Reasoning trace'))

    expect(await screen.findByText('not priced')).toBeInTheDocument()
    const header = document.querySelector('.trace-sum')
    expect(header?.textContent).toContain('1 unpriced')
    expect(header?.textContent).not.toContain('$0.0000')
  })
})
