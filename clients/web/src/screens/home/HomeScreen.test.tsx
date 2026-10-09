import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import HomeScreen, { dropChips, parseCreatedAt } from './HomeScreen'
import { ToastProvider } from '../../shell/toast'
import type { ConsoleScreenProps } from '../../shared/types'
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

function renderHome(extra: Partial<ConsoleScreenProps> = {}) {
  return render(
    <ToastProvider>
      <MemoryRouter>
        <HomeScreen {...props} {...extra} />
      </MemoryRouter>
    </ToastProvider>,
  )
}

const flush = () => act(async () => { await Promise.resolve() })
const tick = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms) })

function mockQueue(items: NeedsYouItem[]) {
  vi.mocked(approvalsApi.needsYou).mockResolvedValue({ data: { count: items.length, items } } as never)
}

const doneSteps = [
  { id: 'connect_tools', title: 'Connect more tools', state_line: '1 of 2 integrations connected', done: true, href: '/settings?section=integrations' },
  { id: 'notify', title: 'Where Vigil pings you', state_line: 'Slack or PagerDuty route is set', done: true, href: '/settings?section=integrations' },
  { id: 'rules', title: 'Link detection rules', state_line: 'Detection rules are on disk', done: true, href: '/settings?section=data&tab=detection' },
  { id: 'per_agent', title: 'Pick a model per agent', state_line: 'Agents use more than one model', done: true, href: '/settings?section=ai-config&tab=assignment' },
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

function triageWith(rows: unknown[], share: number | null = 0.5) {
  vi.mocked(triageApi.get).mockResolvedValue({
    data: { strip: { picked_up: { launched_or_merged: 1, created_today: 2, share } }, rows },
  } as never)
}

const today = () => new Date().toISOString().slice(0, 19)
const droppedRow = (over: Record<string, unknown> = {}) => ({
  kind: 'detection',
  state: 'expired',
  source: 'Okta',
  finding_id: 'f-9',
  decided_at: today(),
  ...over,
})

describe('Home suggestion chips', () => {
  beforeEach(() => {
    vi.mocked(approvalsApi.needsYou).mockResolvedValue({ data: { count: 0, items: [] } } as never)
  })

  it('shows two chips for an alert that expired today, and a click fills the bar with the exact text', async () => {
    triageWith([droppedRow()])
    const fillCommand = vi.fn()
    renderHome({ fillCommand })
    fireEvent.click(await screen.findByRole('button', { name: '/investigate f-9' }))
    expect(fillCommand).toHaveBeenLastCalledWith('/investigate f-9')
    fireEvent.click(screen.getByRole('button', { name: 'Why was the Okta alert dropped?' }))
    expect(fillCommand).toHaveBeenLastCalledWith('/ask Why was the Okta alert dropped? Finding f-9')
    expect(screen.getByText('·')).toBeInTheDocument()
    expect(screen.getByText(/50% of alerts picked up automatically today/)).toBeInTheDocument()
  })

  it('names the finding when the row has no source', async () => {
    triageWith([droppedRow({ source: '' })])
    const fillCommand = vi.fn()
    renderHome({ fillCommand })
    fireEvent.click(await screen.findByRole('button', { name: 'Why was alert f-9 dropped?' }))
    expect(fillCommand).toHaveBeenCalledWith('/ask Why was alert f-9 dropped? Finding f-9')
  })

  it('shows no chips and no dot without a matching row, and none without fillCommand', async () => {
    triageWith([droppedRow({ decided_at: '2020-01-01T00:00:00' }), droppedRow({ state: 'decided' }), droppedRow({ kind: 'approval' })])
    const { unmount } = renderHome({ fillCommand: vi.fn() })
    expect(await screen.findByText(/50% of alerts/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /investigate|dropped/ })).not.toBeInTheDocument()
    expect(screen.queryByText('·')).not.toBeInTheDocument()
    unmount()

    triageWith([droppedRow()])
    renderHome()
    expect(await screen.findByText(/50% of alerts/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /investigate|dropped/ })).not.toBeInTheDocument()
    expect(screen.queryByText('·')).not.toBeInTheDocument()
  })

  it('picks the newest of today (UTC) from zone-less or zoned timestamps', () => {
    const now = Date.UTC(2026, 9, 7, 12, 0, 0)
    const rows = [
      droppedRow({ finding_id: 'old', decided_at: '2026-10-06T23:59:59' }),
      droppedRow({ finding_id: 'a', decided_at: '2026-10-07T01:00:00' }),
      droppedRow({ finding_id: 'b', decided_at: '2026-10-07T09:00:00Z' }),
      droppedRow({ finding_id: null }),
    ] as never
    expect(dropChips(rows, now)[0].text).toBe('/investigate b')
  })
})

describe('Home', () => {
  it('reads a zone-less created_at as UTC', () => {
    expect(parseCreatedAt('2026-10-01T23:00:00')).toBe(Date.UTC(2026, 9, 1, 23, 0, 0))
    expect(parseCreatedAt('2026-10-01T23:00:00Z')).toBe(Date.UTC(2026, 9, 1, 23, 0, 0))
  })

  it('shows the pickup share under the headline and omits it when the share is null', async () => {
    vi.mocked(approvalsApi.needsYou).mockResolvedValue({ data: { count: 0, items: [] } } as never)
    vi.mocked(triageApi.get).mockResolvedValue({
      data: { strip: { picked_up: { launched_or_merged: 3, created_today: 7, share: 0.423 } } },
    } as never)
    const { unmount } = renderHome()
    expect(await screen.findByText('42% of alerts picked up automatically today')).toBeInTheDocument()
    expect(screen.getByText('Board clear.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'How the pickup share is calculated' }))
    expect(screen.getByRole('tooltip')).toHaveTextContent('Source Intake triggers')
    expect(screen.getByRole('tooltip')).toHaveTextContent('Launched or merged ÷ arrived today (UTC), 3 of 7')
    expect(screen.getByRole('tooltip')).toHaveTextContent('Limit None')

    unmount()
    vi.mocked(triageApi.get).mockResolvedValue({
      data: { strip: { picked_up: { share: null } } },
    } as never)
    renderHome()
    expect(await screen.findByText('Board clear.')).toBeInTheDocument()
    expect(screen.queryByText(/picked up automatically today/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'How the pickup share is calculated' })).not.toBeInTheDocument()
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

  it('shows the Board clear block with the heading ⓘ only once a load succeeded with nothing waiting', async () => {
    let resolve: (value: never) => void = () => {}
    vi.mocked(approvalsApi.needsYou).mockReturnValue(new Promise((r) => { resolve = r }) as never)
    renderHome()
    await screen.findByRole('heading', { name: 'Needs your attention' })
    expect(screen.queryByRole('heading', { name: 'Board clear' })).not.toBeInTheDocument() // loading
    await act(async () => resolve({ data: { count: 0, items: [] } } as never))
    expect(await screen.findByRole('heading', { name: 'Board clear' })).toBeInTheDocument()
    expect(screen.getByText(/Nothing waits on you\./)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'How Needs your attention is calculated' }))
    expect(screen.getByRole('tooltip')).toHaveTextContent('Source Pending approvals and checkpoints (B1)')
    expect(screen.getByRole('tooltip')).toHaveTextContent('Calculation Oldest first, top four shown')
  })

  it('does not read a failed load as Board clear', async () => {
    vi.mocked(approvalsApi.needsYou).mockRejectedValue(new Error('queue down'))
    renderHome()
    expect(await screen.findByRole('alert')).toHaveTextContent('queue down')
    expect(screen.queryByRole('heading', { name: 'Board clear' })).not.toBeInTheDocument()
    expect(screen.queryByText('Board clear.')).not.toBeInTheDocument()
  })

  it('has no Board clear block while decisions wait', async () => {
    mockQueue([item()])
    renderHome()
    await screen.findByRole('heading', { name: 'Block 1.2.3.4' })
    expect(screen.queryByRole('heading', { name: 'Board clear' })).not.toBeInTheDocument()
  })

  it('hides a step for this tab when alerts exist, and keeps Get more from Vigil', async () => {
    vi.mocked(approvalsApi.needsYou).mockResolvedValue({ data: { count: 0, items: [] } } as never)
    vi.mocked(configApi.getSetupSteps).mockResolvedValue({
      data: {
        steps: [{ ...doneSteps[0], done: false, state_line: '0 of 4 integrations connected' }, doneSteps[1]],
        alerts_exist: 5,
        demo_enabled: false,
      },
    } as never)
    renderHome()

    const step = (await screen.findByRole('heading', { name: 'Connect more tools' })).closest('li')
    expect(within(step as HTMLElement).getByRole('link', { name: 'Set up' })).toHaveAttribute(
      'href',
      '/settings?section=integrations',
    )
    expect(screen.queryByText('No alerts yet')).not.toBeInTheDocument()
    expect(screen.queryByText(/of \d+ done/)).not.toBeInTheDocument()

    fireEvent.click(within(step as HTMLElement).getByRole('button', { name: 'Not now' }))
    expect(screen.queryByRole('heading', { name: 'Connect more tools' })).not.toBeInTheDocument()
    expect(JSON.parse(sessionStorage.getItem('vigil.home.setup.hidden') || '[]')).toEqual(['connect_tools'])
    expect(screen.getByRole('link', { name: 'Browse integrations →' })).toBeInTheDocument()
  })

  it('lays the open steps out in one numbered row and Not now renumbers the rest', async () => {
    vi.mocked(approvalsApi.needsYou).mockResolvedValue({ data: { count: 0, items: [] } } as never)
    vi.mocked(configApi.getSetupSteps).mockResolvedValue({
      data: {
        steps: [
          { ...doneSteps[0], done: false },
          doneSteps[1],
          { ...doneSteps[2], done: false },
          { ...doneSteps[3], done: false },
        ],
        alerts_exist: 5,
        demo_enabled: false,
      },
    } as never)
    renderHome()
    await screen.findByRole('heading', { name: 'Connect more tools' })
    const rows = () => Array.from(document.querySelectorAll<HTMLElement>('.home-step'))
    expect(rows().map((row) => row.querySelector('.home-step-no')?.textContent)).toEqual(['1', '2', '3'])
    expect((document.querySelector('.home-steps') as HTMLElement).style.gridTemplateColumns).toBe(
      'repeat(3, minmax(0, 1fr))',
    )
    expect(screen.getByText('Fixed order · most impact first')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'How Get more from Vigil is calculated' }))
    expect(screen.getByRole('tooltip')).toHaveTextContent('Source Setup steps (B8)')

    fireEvent.click(within(rows()[0]).getByRole('button', { name: 'Not now' }))
    expect(rows().map((row) => row.querySelector('h3')?.textContent)).toEqual([
      'Link detection rules',
      'Pick a model per agent',
    ])
    expect(rows().map((row) => row.querySelector('.home-step-no')?.textContent)).toEqual(['1', '2'])
  })

  it('serves the custom skill step as a fifth card whose Add opens the Skills tab, and counts it on first run', async () => {
    const skill = { id: 'custom_skill', title: 'Add a custom skill', state_line: 'Teach Vigil your team’s playbooks', done: false, href: '/workflows?tab=skills' }
    vi.mocked(approvalsApi.needsYou).mockResolvedValue({ data: { count: 0, items: [] } } as never)
    vi.mocked(configApi.getSetupSteps).mockResolvedValue({
      data: { steps: [...doneSteps.map((step) => ({ ...step, done: false })), skill], alerts_exist: 5, demo_enabled: false },
    } as never)
    const { unmount } = renderHome()
    const card = (await screen.findByRole('heading', { name: 'Add a custom skill' })).closest('li') as HTMLElement
    expect(within(card).getByRole('link', { name: 'Add' })).toHaveAttribute('href', '/workflows?tab=skills')
    expect(card.querySelector('.home-step-no')).toHaveTextContent('5')
    expect((document.querySelector('.home-steps') as HTMLElement).style.gridTemplateColumns).toBe('repeat(5, minmax(0, 1fr))')
    unmount()

    vi.mocked(configApi.getSetupSteps).mockResolvedValue({
      data: { steps: [...doneSteps, { ...skill, done: true, state_line: '1 custom skill' }], alerts_exist: 0, demo_enabled: false },
    } as never)
    renderHome()
    expect(await screen.findByText('5 of 5 done')).toBeInTheDocument()
  })

  it('says there is nothing to suggest when no step is open', async () => {
    vi.mocked(approvalsApi.needsYou).mockResolvedValue({ data: { count: 0, items: [] } } as never)
    renderHome()
    expect(await screen.findByText('Nothing to suggest right now.')).toBeInTheDocument()
  })

  it('first run puts the checklist under Needs your attention with no sub-line, Board clear or Get more', async () => {
    vi.mocked(approvalsApi.needsYou).mockResolvedValue({ data: { count: 0, items: [] } } as never)
    vi.mocked(configApi.getSetupSteps).mockResolvedValue({
      data: { steps: doneSteps, alerts_exist: 0, demo_enabled: false },
    } as never)
    renderHome()
    await screen.findByText('Get Vigil ready')
    const needs = screen.getByRole('region', { name: 'Needs your attention' })
    expect(within(needs).getByText('No alerts yet')).toBeInTheDocument()
    expect(within(needs).queryByText(/open · oldest first/)).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Board clear' })).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Get more from Vigil' })).not.toBeInTheDocument()
    expect(screen.queryByText('Nothing to suggest right now.')).not.toBeInTheDocument()
  })

  it('shows the kind chip for an approval and a checkpoint, and the case and reason in one line', async () => {
    mockQueue([
      item({ source_id: 'a', title: 'Block IP', kind: 'approval', reason: 'beacon', case_id: 'case-9' }),
      item({ source_id: 'b', title: 'Declare incident', kind: 'checkpoint', reason: '', case_id: null }),
    ])
    renderHome()
    const first = (await screen.findByRole('heading', { name: 'Block IP' })).closest('article') as HTMLElement
    expect(within(first).getByText('Approval')).toBeInTheDocument()
    expect(within(first).getByText('Case case-9 · beacon')).toHaveAttribute('title', 'Case case-9 · beacon')
    const second = screen.getByRole('heading', { name: 'Declare incident' }).closest('article') as HTMLElement
    expect(within(second).getByText('Checkpoint')).toBeInTheDocument()
    expect(second.querySelector('.home-card-meta')).toBeNull()
    expect(within(second).queryByRole('link', { name: 'Open case' })).not.toBeInTheDocument()
  })

  it('the Needs sub-line says showing 4 only while more wait, and the +N tile expands the list', async () => {
    const six = [
      ...Array.from({ length: 4 }, (_, i) => item({ source_id: `a${i}`, title: `Card ${i}` })),
      item({ source_id: 'r1', title: 'Rest 1', kind: 'approval' }),
      item({ source_id: 'r2', title: 'Rest 2', kind: 'checkpoint' }),
    ]
    mockQueue(six.slice(0, 3))
    const { unmount } = renderHome()
    expect(await screen.findByText('3 open · oldest first')).toBeInTheDocument()
    expect(screen.queryByLabelText('More waiting')).not.toBeInTheDocument()
    unmount()

    mockQueue(six)
    renderHome()
    expect(await screen.findByText('6 open · oldest first · showing 4')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Rest 1' })).not.toBeInTheDocument()
    const tile = screen.getByLabelText('More waiting')
    expect(within(tile).getByRole('heading', { name: '+2 more waiting' })).toBeInTheDocument()
    expect(within(tile).getByText('Approval').nextSibling).toHaveTextContent('1')
    expect(within(tile).getByText('Checkpoint').nextSibling).toHaveTextContent('1')

    fireEvent.click(within(tile).getByRole('button', { name: 'Show all' }))
    expect(screen.getByRole('heading', { name: 'Rest 1' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Rest 2' })).toBeInTheDocument()
    expect(screen.queryByLabelText('More waiting')).not.toBeInTheDocument()
    expect(screen.getByText('6 open · oldest first')).toBeInTheDocument()
  })

  it('first run lists every step with done ones ticked and counts them from the response', async () => {
    vi.mocked(approvalsApi.needsYou).mockResolvedValue({ data: { count: 0, items: [] } } as never)
    vi.mocked(configApi.getSetupSteps).mockResolvedValue({
      data: {
        steps: [{ ...doneSteps[0], done: false }, doneSteps[1], { ...doneSteps[2], done: false }],
        alerts_exist: 0,
        demo_enabled: false,
      },
    } as never)
    renderHome()

    expect(await screen.findByText('1 of 3 done')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Get Vigil ready' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Get more from Vigil' })).not.toBeInTheDocument()
    const rows = within(screen.getByRole('list', { name: 'Setup steps' })).getAllByRole('listitem')
    expect(rows.map((row) => row.querySelector('h3')?.textContent)).toEqual([
      'Connect more tools',
      'Where Vigil pings you (done)',
      'Link detection rules',
    ])
    expect(rows[1]).toHaveClass('done')
    expect(rows[0]).not.toHaveClass('done')
    expect(within(rows[2]).getByRole('link', { name: 'Link' })).toHaveAttribute(
      'href',
      '/settings?section=data&tab=detection',
    )
    expect(screen.queryByRole('button', { name: 'Not now' })).not.toBeInTheDocument()
    expect(screen.getByText('No alerts yet')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Connect data' })).toHaveAttribute('href', '/settings?section=data')
  })

  it('first run: Take the tour calls startTour and is hidden without it', async () => {
    vi.mocked(approvalsApi.needsYou).mockResolvedValue({ data: { count: 0, items: [] } } as never)
    vi.mocked(configApi.getSetupSteps).mockResolvedValue({
      data: { steps: doneSteps, alerts_exist: 0, demo_enabled: false },
    } as never)
    const startTour = vi.fn()
    const { unmount } = renderHome({ startTour })
    fireEvent.click(await screen.findByRole('button', { name: 'Take the tour' }))
    expect(startTour).toHaveBeenCalledTimes(1)

    unmount()
    renderHome()
    await screen.findByText('Not ready to connect yet?')
    expect(screen.queryByRole('button', { name: 'Take the tour' })).not.toBeInTheDocument()
  })

  it('first run with demo on drops Explore with demo data but keeps the tour', async () => {
    vi.mocked(approvalsApi.needsYou).mockResolvedValue({ data: { count: 0, items: [] } } as never)
    vi.mocked(configApi.getSetupSteps).mockResolvedValue({
      data: { steps: doneSteps, alerts_exist: 0, demo_enabled: true },
    } as never)
    renderHome({ startTour: vi.fn() })
    expect(await screen.findByRole('button', { name: 'Take the tour' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Explore with demo data' })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Connect data' })).toBeInTheDocument()
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

  it('approves a reversible row after the 8 s fuse, not before', async () => {
    mockQueue([item({ source_id: 'act-rev' })])
    vi.mocked(approvalsApi.approve).mockResolvedValue({} as never)
    renderHome()
    expect(await screen.findByText('1 decision waits on you. Everything else is running.')).toBeInTheDocument()

    vi.useFakeTimers()
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    expect(screen.getByText('Approving: Block 1.2.3.4')).toBeInTheDocument()
    expect(screen.getByText('Board clear.')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Block 1.2.3.4' })).not.toBeInTheDocument()

    await tick(7999)
    expect(approvalsApi.approve).not.toHaveBeenCalled()
    mockQueue([])
    await tick(1)
    expect(approvalsApi.approve).toHaveBeenCalledWith('act-rev')
    expect(screen.getByText('Approved: Block 1.2.3.4')).toBeInTheDocument()
    expect(vi.mocked(approvalsApi.needsYou).mock.calls.length).toBeGreaterThan(1) // reloaded
  })

  it('keeps the card hidden while the commit is in flight', async () => {
    mockQueue([item({ source_id: 'act-rev' })])
    let finish: (v: unknown) => void = () => {}
    vi.mocked(approvalsApi.approve).mockReturnValue(new Promise((resolve) => (finish = resolve)) as never)
    renderHome()
    await screen.findByRole('heading', { name: 'Block 1.2.3.4' })

    vi.useFakeTimers()
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await tick(8000)
    expect(approvalsApi.approve).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('heading', { name: 'Block 1.2.3.4' })).not.toBeInTheDocument()
    mockQueue([])
    finish({})
    await tick(0)
    expect(screen.getByText('Approved: Block 1.2.3.4')).toBeInTheDocument()
  })

  it('Undo before 8 s sends nothing and brings the card back', async () => {
    mockQueue([item({ source_id: 'act-rev' })])
    renderHome()
    await screen.findByRole('heading', { name: 'Block 1.2.3.4' })

    vi.useFakeTimers()
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await tick(3000)
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }))
    expect(screen.getByRole('heading', { name: 'Block 1.2.3.4' })).toBeInTheDocument()
    expect(screen.getByText('1 decision waits on you. Everything else is running.')).toBeInTheDocument()
    await tick(10_000)
    expect(approvalsApi.approve).not.toHaveBeenCalled()
    expect(screen.queryByText(/Approved:/)).not.toBeInTheDocument()
  })

  it('rejects through the fuse with the typed reason', async () => {
    mockQueue([item({ source_id: 'act-no', title: 'Disable account' })])
    vi.mocked(approvalsApi.reject).mockResolvedValue({} as never)
    renderHome()
    await screen.findByRole('heading', { name: 'Disable account' })

    fireEvent.click(screen.getByRole('button', { name: 'Reject' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Rejection reason' }), {
      target: { value: 'not our host' },
    })
    vi.useFakeTimers()
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }))
    expect(approvalsApi.reject).not.toHaveBeenCalled()
    await tick(8000)
    expect(approvalsApi.reject).toHaveBeenCalledWith('act-no', 'not our host')
    expect(screen.getByText('Rejected: Disable account')).toBeInTheDocument()
  })

  it('raises an error toast when the commit fails, and the card comes back on reload', async () => {
    mockQueue([item({ source_id: 'act-rev' })])
    vi.mocked(approvalsApi.approve).mockRejectedValue({ response: { data: { detail: 'already decided' } } })
    renderHome()
    await screen.findByRole('heading', { name: 'Block 1.2.3.4' })

    vi.useFakeTimers()
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await tick(8000)
    expect(screen.getByRole('alert')).toHaveTextContent('already decided')
    expect(screen.getByRole('heading', { name: 'Block 1.2.3.4' })).toBeInTheDocument()
  })

  it('keeps the fuse running when Home is left, and keeps the card hidden when it is reopened', async () => {
    mockQueue([item({ source_id: 'act-rev' })])
    vi.mocked(approvalsApi.approve).mockResolvedValue({} as never)
    const first = renderHome()
    await screen.findByRole('heading', { name: 'Block 1.2.3.4' })

    vi.useFakeTimers()
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await tick(3000)
    // leave Home, keep the shell (provider) alive
    first.rerender(<ToastProvider>{null}</ToastProvider>)
    await tick(2000)
    first.rerender(
      <ToastProvider>
        <MemoryRouter>
          <HomeScreen {...props} />
        </MemoryRouter>
      </ToastProvider>,
    )
    await flush()
    await tick(0)
    // the server still lists the item; the fuse keeps it off the board
    expect(screen.queryByRole('heading', { name: 'Block 1.2.3.4' })).not.toBeInTheDocument()
    expect(screen.getByText('Board clear.')).toBeInTheDocument()

    expect(approvalsApi.approve).not.toHaveBeenCalled()
    await tick(3000)
    expect(approvalsApi.approve).toHaveBeenCalledTimes(1)
    expect(approvalsApi.approve).toHaveBeenCalledWith('act-rev')
  })

  it('keeps an irreversible approve on hold-to-confirm, with no fuse', async () => {
    mockQueue([item({ source_id: 'act-irr', title: 'Isolate host', kind: 'checkpoint', reversibility: 'irreversible', case_id: null })])
    vi.mocked(approvalsApi.approve).mockResolvedValue({} as never)
    renderHome()
    const hold = await screen.findByRole('button', { name: /Press and hold to confirm/ })

    vi.useFakeTimers()
    fireEvent.pointerDown(hold)
    act(() => {
      vi.advanceTimersByTime(200)
    })
    fireEvent.pointerUp(hold)
    act(() => {
      vi.advanceTimersByTime(1600)
    })
    expect(approvalsApi.approve).not.toHaveBeenCalled()

    fireEvent.pointerDown(hold)
    act(() => {
      vi.advanceTimersByTime(1600)
    })
    expect(approvalsApi.approve).toHaveBeenCalledWith('act-irr') // at once, no 8 s wait
    await flush()
    expect(screen.getByText('Approved: Isolate host')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Undo' })).not.toBeInTheDocument()
  })
})
