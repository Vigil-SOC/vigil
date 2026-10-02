import { describe, expect, it, vi, afterEach } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import HomeScreen, { parseCreatedAt } from './HomeScreen'
import { approvalsApi, type NeedsYouItem } from '../../services/api'

vi.mock('../../services/api', () => ({
  approvalsApi: {
    needsYou: vi.fn(),
    approve: vi.fn(),
    reject: vi.fn(),
  },
}))

const props = {
  openChat: vi.fn(),
  go: vi.fn(),
  goSettings: vi.fn(),
  setViewFull: vi.fn(),
}

function item(over: Partial<NeedsYouItem> = {}): NeedsYouItem {
  return {
    kind: 'approval',
    source_id: 'act-1',
    title: 'Block 1.2.3.4',
    reason: 'beacon',
    created_at: new Date().toISOString(),
    reversibility: 'reversible',
    case_id: 'case-9',
    ...over,
  }
}

function renderHome() {
  render(
    <MemoryRouter>
      <HomeScreen {...props} />
    </MemoryRouter>,
  )
}

afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('Home', () => {
  it('reads a zone-less created_at as UTC', () => {
    expect(parseCreatedAt('2026-10-01T23:00:00')).toBe(Date.UTC(2026, 9, 1, 23, 0, 0))
    expect(parseCreatedAt('2026-10-01T23:00:00Z')).toBe(Date.UTC(2026, 9, 1, 23, 0, 0))
  })

  it('says the board is clear when nothing is waiting', async () => {
    vi.mocked(approvalsApi.needsYou).mockResolvedValue({ data: { count: 0, items: [] } } as never)
    renderHome()
    expect(await screen.findByText('Board clear.')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Needs your attention' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Cases →' })).toHaveAttribute('href', '/cases')
    expect(screen.getByRole('region', { name: 'Setup' })).toBeEmptyDOMElement()
  })

  it('approves a reversible row on one press, and an irreversible row only after a hold', async () => {
    vi.mocked(approvalsApi.needsYou).mockResolvedValue({
      data: {
        count: 2,
        items: [
          item({ source_id: 'act-rev', reversibility: 'reversible', case_id: 'case-9' }),
          item({
            source_id: 'act-irr',
            title: 'Isolate host',
            kind: 'checkpoint',
            reversibility: 'irreversible',
            case_id: null,
          }),
        ],
      },
    } as never)
    vi.mocked(approvalsApi.approve).mockResolvedValue({} as never)
    renderHome()

    expect(await screen.findByText('2 decisions wait on you. Everything else is running.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open case' })).toHaveAttribute('href', '/cases?case=case-9')
    expect(screen.getAllByRole('link', { name: 'Open case' })).toHaveLength(1)

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    expect(approvalsApi.approve).toHaveBeenCalledWith('act-rev')
    await act(async () => {
      await Promise.resolve()
    })

    const hold = screen.getByRole('button', { name: /Press and hold to confirm/ })
    vi.useFakeTimers()
    fireEvent.pointerDown(hold)
    act(() => {
      vi.advanceTimersByTime(200)
    })
    fireEvent.pointerUp(hold)
    act(() => {
      vi.advanceTimersByTime(1600)
    })
    expect(approvalsApi.approve).not.toHaveBeenCalledWith('act-irr')

    fireEvent.pointerDown(hold)
    act(() => {
      vi.advanceTimersByTime(1600)
    })
    expect(approvalsApi.approve).toHaveBeenCalledWith('act-irr')
  })

  it('sends the typed reason when rejecting', async () => {
    vi.mocked(approvalsApi.needsYou).mockResolvedValue({
      data: { count: 1, items: [item({ source_id: 'act-no', title: 'Disable account' })] },
    } as never)
    vi.mocked(approvalsApi.reject).mockResolvedValue({} as never)
    renderHome()

    expect(await screen.findByText('1 decision waits on you. Everything else is running.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Rejection reason' }), {
      target: { value: 'not our host' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }))
    expect(approvalsApi.reject).toHaveBeenCalledWith('act-no', 'not our host')
  })
})
