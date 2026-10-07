import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import HomeScreen, { parseCreatedAt } from './HomeScreen'
import { approvalsApi, configApi, triageApi, type NeedsYouItem } from '../../services/api'

vi.mock('../../services/api', () => ({
  approvalsApi: {
    needsYou: vi.fn(),
    approve: vi.fn(),
    reject: vi.fn(),
  },
  configApi: {
    getSetupSteps: vi.fn(),
    setDemoMode: vi.fn(),
  },
  triageApi: {
    get: vi.fn(),
  },
}))

const props = {
  openChat: vi.fn(),
  go: vi.fn(),
  goSettings: vi.fn(),
  openCase: vi.fn(),
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

const doneSteps = [
  { id: 'connect_tools', title: 'Connect more tools', state_line: '1 of 2 integrations connected', done: true, href: '/settings?section=integrations' },
  { id: 'notify', title: 'Where Vigil pings you', state_line: 'Slack or PagerDuty route is set', done: true, href: '/settings?section=integrations' },
  { id: 'rules', title: 'Link detection rules', state_line: 'Detection rules are on disk', done: true, href: '/settings?section=data&tab=detection' },
  { id: 'per_agent', title: 'Pick a model per agent', state_line: 'Agents use more than one model', done: true, href: '/settings?section=ai-config' },
]

beforeEach(() => {
  sessionStorage.clear()
  vi.mocked(configApi.getSetupSteps).mockResolvedValue({
    data: { steps: doneSteps, alerts_exist: 3, demo_enabled: false },
  } as never)
  vi.mocked(triageApi.get).mockResolvedValue({
    data: { strip: { picked_up: { share: null } } },
  } as never)
})

afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
  sessionStorage.clear()
})

describe('Home', () => {
  it('reads a zone-less created_at as UTC', () => {
    expect(parseCreatedAt('2026-10-01T23:00:00')).toBe(Date.UTC(2026, 9, 1, 23, 0, 0))
    expect(parseCreatedAt('2026-10-01T23:00:00Z')).toBe(Date.UTC(2026, 9, 1, 23, 0, 0))
  })

  it('shows the pickup share under the headline and omits it when the share is null', async () => {
    vi.mocked(approvalsApi.needsYou).mockResolvedValue({ data: { count: 0, items: [] } } as never)
    vi.mocked(triageApi.get).mockResolvedValue({
      data: { strip: { picked_up: { share: 0.423 } } },
    } as never)
    const { unmount } = render(
      <MemoryRouter>
        <HomeScreen {...props} />
      </MemoryRouter>,
    )
    expect(await screen.findByText('42.3% of alerts picked up automatically today')).toBeInTheDocument()
    expect(screen.getByText('Board clear.')).toBeInTheDocument()

    unmount()
    vi.mocked(triageApi.get).mockResolvedValue({
      data: { strip: { picked_up: { share: null } } },
    } as never)
    renderHome()
    expect(await screen.findByText('Board clear.')).toBeInTheDocument()
    expect(screen.queryByText(/picked up automatically today/)).not.toBeInTheDocument()
  })

  it('omits the pickup line when the triage read fails', async () => {
    vi.mocked(approvalsApi.needsYou).mockResolvedValue({ data: { count: 1, items: [item()] } } as never)
    vi.mocked(triageApi.get).mockRejectedValue(new Error('triage down'))
    renderHome()
    expect(await screen.findByText('1 decision waits on you. Everything else is running.')).toBeInTheDocument()
    expect(screen.queryByText(/picked up automatically today/)).not.toBeInTheDocument()
  })

  it('says the board is clear when nothing is waiting', async () => {
    vi.mocked(approvalsApi.needsYou).mockResolvedValue({ data: { count: 0, items: [] } } as never)
    renderHome()
    expect(await screen.findByText('Board clear.')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Needs your attention' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Cases →' })).toHaveAttribute('href', '/cases')
    expect(screen.getByRole('heading', { name: 'Get more from Vigil' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Browse integrations →' })).toHaveAttribute(
      'href',
      '/settings?section=integrations',
    )
    expect(screen.queryByRole('button', { name: 'Not now' })).not.toBeInTheDocument()
  })

  it('hides a step for this tab and drops the demo action when demo is on', async () => {
    vi.mocked(approvalsApi.needsYou).mockResolvedValue({ data: { count: 0, items: [] } } as never)
    vi.mocked(configApi.getSetupSteps).mockResolvedValue({
      data: {
        steps: [
          {
            id: 'connect_tools',
            title: 'Connect more tools',
            state_line: '0 of 4 integrations connected',
            done: false,
            href: '/settings?section=integrations',
          },
          doneSteps[1],
        ],
        alerts_exist: 0,
        demo_enabled: true,
      },
    } as never)
    renderHome()

    const step = (await screen.findByRole('heading', { name: 'Connect more tools' })).closest('li')
    expect(step).not.toBeNull()
    expect(within(step as HTMLElement).getByRole('link', { name: 'Set up' })).toHaveAttribute(
      'href',
      '/settings?section=integrations',
    )
    expect(screen.getByRole('link', { name: 'Connect data' })).toHaveAttribute(
      'href',
      '/settings?section=data',
    )
    expect(screen.queryByRole('button', { name: 'Explore with demo data' })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Browse integrations →' })).toBeInTheDocument()

    fireEvent.click(within(step as HTMLElement).getByRole('button', { name: 'Not now' }))
    expect(screen.queryByRole('heading', { name: 'Connect more tools' })).not.toBeInTheDocument()
    expect(JSON.parse(sessionStorage.getItem('vigil.home.setup.hidden') || '[]')).toEqual(['connect_tools'])
    expect(screen.getByRole('link', { name: 'Browse integrations →' })).toBeInTheDocument()
  })

  it('shows the demo-mode message and leaves the action once demo is on', async () => {
    vi.mocked(approvalsApi.needsYou).mockResolvedValue({ data: { count: 0, items: [] } } as never)
    vi.mocked(configApi.getSetupSteps).mockResolvedValue({
      data: { steps: doneSteps, alerts_exist: 0, demo_enabled: false },
    } as never)
    vi.mocked(configApi.setDemoMode).mockResolvedValue({
      data: { message: 'Demo mode enabled. Restart the server for changes to take effect.' },
    } as never)
    renderHome()

    fireEvent.click(await screen.findByRole('button', { name: 'Explore with demo data' }))
    expect(configApi.setDemoMode).toHaveBeenCalledWith(true)
    expect(
      await screen.findByText('Demo mode enabled. Restart the server for changes to take effect.'),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Explore with demo data' })).not.toBeInTheDocument()
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
    fireEvent.click(screen.getByRole('link', { name: 'Open case' }))
    expect(props.openCase).toHaveBeenCalledWith('case-9')

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
