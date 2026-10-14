import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import FleetWallScreen from './FleetWallScreen'
import PulseScreen from './PulseScreen'
import { monitorApi, type FleetWallPayload, type MonitorMetric, type PulseDay, type PulseDecisions, type PulseSummary, type PulseWork, type WallCell } from '../../services/api'

vi.mock('../../services/api', () => ({
  monitorApi: { wall: vi.fn(), summary: vi.fn(), throughput: vi.fn(), work: vi.fn(), decisions: vi.fn() },
}))

afterEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
})

const m = (value: number | null, over: Partial<MonitorMetric> = {}): MonitorMetric => ({
  value,
  limit: null,
  level: null,
  info: 'how it is counted',
  unmeasured_text: null,
  unmeasured_reason: null,
  ...over,
})
const unmeasured = (reason: string) => m(null, { unmeasured_text: 'Not measured yet', unmeasured_reason: reason })

const cell = (id: string, state: WallCell['state'], over: Partial<WallCell> = {}): WallCell => ({
  case_id: id,
  title: `Case title ${id}`,
  description: null,
  workflow_id: 'full-investigation',
  severity: 'high',
  state,
  reason: state === 'needs_you' ? 'Isolate host ws-17' : 'A run is working this case',
  alerts: 1,
  combined: false,
  heavy: false,
  limit_used: 0.4,
  limit_level: 'good',
  near_limit: false,
  steps: 4,
  owner: null,
  sla_seconds_left: 3600,
  decision: null,
  incident: null,
  ...over,
})

function wall(): FleetWallPayload {
  return {
    now: '2026-10-11T14:22:00Z',
    next_action: { count: 1, case_id: 'c-ask', title: 'Isolate host ws-17', reason: 'beaconing' },
    slots: { ...m(2, { limit: 3, level: 'fair' }), near_limit: 1 },
    closed_today: { ...m(12), yesterday: 9, without_person: 0.75 },
    incidents_today: unmeasured('Incidents are not recorded.'),
    load: m(4, { limit: 500, level: 'good' }),
    facets: [
      { key: 'all', label: 'All open', count: 3, unmeasured_reason: null },
      { key: 'needs_you', label: 'Needs you', count: 1, unmeasured_reason: null },
      { key: 'running', label: 'Running', count: 2, unmeasured_reason: null },
      { key: 'stuck', label: 'Stuck', count: null, unmeasured_reason: 'Stuck detection is not recorded yet.' },
    ],
    bands: [
      {
        workflow_id: 'full-investigation',
        name: 'Full Investigation',
        open: 3,
        running: { ...m(2), cap_text: null },
        today: 7,
        cost_per_case: 0.42,
        mean_steps: 5.5,
        waiting: 0,
        without_person: { ...m(0.75, { level: 'poor' }), sample: 4 },
        cells: [cell('c-ask', 'needs_you'), cell('c-run', 'running', { heavy: true, alerts: 6, combined: true }), cell('c-run2', 'running')],
      },
    ],
    omitted: 0,
    just_closed: [{ case_id: 'c-done', closed_at: '2026-10-11T14:10:00Z', by: 'agents' }],
    just_closed_minutes: 15,
    info: { cell: 'one square per case', kind: 'per workflow' },
  }
}

function day(): PulseDay {
  const n = 24
  return {
    hours: Array.from({ length: n }, (_, i) => `2026-10-10T${String((15 + i) % 24).padStart(2, '0')}:00:00Z`),
    alerts_in: Array.from({ length: n }, (_, i) => i % 5),
    cases_formed: Array.from({ length: n }, () => 1),
    attached: Array.from({ length: n }, () => 0),
    closed_by_agents: Array.from({ length: n }, () => 2),
    closed_by_person: Array.from({ length: n }, () => 0),
    running: Array.from({ length: n }, (_, i) => (i === 10 ? 3 : 1)),
    queued: Array.from({ length: n }, () => 2),
    decisions: Array.from({ length: n }, () => 0),
    cap: 3,
    high_water: 500,
    peak: { running: 3, at: '2026-10-11T01:00:00Z', free: 0 },
    dropped: unmeasured('There is no drop line yet.'),
    approved_by_person: unmeasured('Approved closes are not recorded apart yet.'),
    info: 'per hour',
    queue_info: 'queued each hour',
  }
}

function summary(): PulseSummary {
  return {
    now: '2026-10-11T14:22:00Z',
    gauges: [
      { ...m(0.87, { level: 'poor' }), key: 'without_person', label: 'Handled without a person', sample: 40, yesterday: null },
      { ...m(40), key: 'closed_today', label: 'Cases closed today', sample: null, yesterday: 30 },
      { ...m(6), key: 'time_to_decide', label: 'Median time to decide', sample: 3, yesterday: null },
      { ...m(12.5, { limit: 100, level: 'good' }), key: 'spend_today', label: 'AI spend today', sample: null, yesterday: null },
    ],
    attention: [{ level: 'poor', title: '2 decisions waiting on a person', why: 'Isolate host ws-17', action: 'Open Needs you', door: 'needs_you' }],
    day: day(),
    adjustments: [
      { key: 'concurrency', label: 'Cases running at once · fleet', current: '3', note: 'Read here; change it in Settings.', unmeasured_text: null },
      { key: 'drop_line', label: 'Drop alerts scoring under', current: null, note: 'There is no drop line yet.', unmeasured_text: 'Not measured yet' },
    ],
  }
}

function work(): PulseWork {
  return {
    now: '2026-10-11T14:22:00Z',
    rows: [
      { key: 'needs_you', label: 'Needs you', what: 'A decision waits on a person', count: m(2), ages: [{ label: 'under 15 min', count: 1 }, { label: '15 min to 1 h', count: 1 }, { label: '1 to 4 h', count: 0 }, { label: '4 h to 1 day', count: 0 }, { label: 'over 1 day', count: 0 }], oldest: { minutes: 42, case_id: 'c-ask' } },
      { key: 'stuck', label: 'Stuck', what: 'No explanation moved in 6 or more steps', count: unmeasured('Stuck detection is not recorded yet.'), ages: null, oldest: null },
    ],
    queue: { hours: day().hours, queued: day().queued, high_water: 500, queue_info: 'queued each hour' },
    info: 'how long they waited',
  }
}

function decisions(): PulseDecisions {
  const day = (d: string, alone: number, approved: number | null, person: number) => ({ day: d, agents_alone: alone, approved_by_person: approved, by_person: person })
  return {
    now: '2026-10-11T14:22:00Z',
    who: {
      days: ['05', '06', '07', '08', '09', '10', '11'].map((d) => day(`2026-10-${d}`, 87, 9, 4)),
      approved_unmeasured_reason: null,
      info: 'per day',
    },
    stops: {
      reasons: [{ key: 'budget_reached', label: 'Budget reached' }, { key: 'stuck', label: 'Stuck' }],
      kinds: [{ workflow_id: 'full-investigation', name: 'Investig.' }, { workflow_id: 'phishing-triage', name: 'Phishing' }],
      counts: [[4, 0], [0, 2]],
      median_minutes: 6,
      expired: 2,
      info: 'asked today',
    },
    suggestion: { text: 'Budget stops were extended 6 of 7 times this week.', action: 'Change the default budget', settings_section: 'ai-config' },
  }
}

const props = () => ({ openChat: vi.fn(), go: vi.fn(), goSettings: vi.fn(), openCase: vi.fn(), setViewFull: vi.fn() })

describe('Fleet Wall', () => {
  it('draws every open case in its workflow band, the strip against its limits, and what is not measured', async () => {
    vi.mocked(monitorApi.wall).mockResolvedValue({ data: wall() } as never)
    const p = props()
    render(<MemoryRouter><FleetWallScreen {...p} /></MemoryRouter>)
    expect(await screen.findByRole('heading', { level: 1, name: 'Fleet Wall' })).toBeInTheDocument()
    expect(screen.getByText(/1 case is waiting on you/)).toBeInTheDocument()
    expect(screen.getByText('Full Investigation')).toBeInTheDocument()
    expect(screen.getAllByRole('listitem')).toHaveLength(3)
    expect(screen.getByRole('listitem', { name: 'Case c-run, Running' })).toHaveClass('heavy')
    expect(screen.getByText('+3 vs yesterday')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Stuck/ })).toBeDisabled()

    fireEvent.click(screen.getByRole('button', { name: 'Review the first one' }))
    expect(p.openCase).toHaveBeenCalledWith('c-ask')
  })

  it('thins the wall to one state and remembers it for the session', async () => {
    vi.mocked(monitorApi.wall).mockResolvedValue({ data: wall() } as never)
    render(<MemoryRouter><FleetWallScreen {...props()} /></MemoryRouter>)
    const facets = await screen.findByRole('group', { name: 'Filter by state' })
    fireEvent.click(within(facets).getByRole('button', { name: /Needs you/ }))
    expect(screen.getByRole('listitem', { name: 'Case c-run, Running' })).toHaveClass('dim')
    expect(screen.getByRole('listitem', { name: 'Case c-ask, Needs you' })).not.toHaveClass('dim')
    expect(sessionStorage.getItem('vigil.fleetWall.facet')).toBe('needs_you')
  })

  it('opens a selected case from its panel, and clears the selection on Escape', async () => {
    vi.mocked(monitorApi.wall).mockResolvedValue({ data: wall() } as never)
    const p = props()
    render(<MemoryRouter><FleetWallScreen {...p} /></MemoryRouter>)
    fireEvent.click(await screen.findByRole('listitem', { name: 'Case c-ask, Needs you' }))
    const panel = screen.getByRole('region', { name: 'Selected case c-ask' })
    expect(within(panel).getByText(/Isolate host ws-17/)).toBeInTheDocument()
    fireEvent.click(within(panel).getByRole('button', { name: 'Decide in the case' }))
    expect(p.openCase).toHaveBeenCalledWith('c-ask')
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('region', { name: 'Selected case c-ask' })).not.toBeInTheDocument()
  })
})

describe('Pulse', () => {
  it('opens on Summary: gauges against their limits, what needs attention, the day, and the adjustments', async () => {
    vi.mocked(monitorApi.summary).mockResolvedValue({ data: summary() } as never)
    const p = props()
    render(<MemoryRouter><PulseScreen {...p} /></MemoryRouter>)
    expect(await screen.findByText('Handled without a person')).toBeInTheDocument()
    expect(screen.getByText('87%')).toBeInTheDocument()
    expect(screen.getByText('Poor')).toBeInTheDocument()
    expect(screen.getByText('$12.50')).toBeInTheDocument()
    expect(screen.getByRole('img', { name: /Alerts in, cases closed and cases running/ })).toBeInTheDocument()
    expect(screen.getAllByText(/Not measured yet/).length).toBeGreaterThan(0)
    fireEvent.click(screen.getByRole('button', { name: 'Open Needs you' }))
    expect(p.go).toHaveBeenCalledWith('home')
  })

  it('shows what waits by age on Paused and stuck, and the oldest opens its case', async () => {
    vi.mocked(monitorApi.work).mockResolvedValue({ data: work() } as never)
    const p = props()
    render(<MemoryRouter initialEntries={['/pulse?tab=work']}><PulseScreen {...p} /></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button', { name: 'Oldest: 42 min →' }))
    expect(p.openCase).toHaveBeenCalledWith('c-ask')
    expect(screen.getByText('Stuck')).toBeInTheDocument()
  })

  it('marks the tabs this release does not build', async () => {
    render(<MemoryRouter initialEntries={['/pulse?tab=system']}><PulseScreen {...props()} /></MemoryRouter>)
    expect(await screen.findByText(/Provider latency and errors/)).toBeInTheDocument()
    expect(monitorApi.summary).not.toHaveBeenCalled()
  })

  it('shows who decided today and over 7 days, each stop by reason and kind, and opens the suggested setting', async () => {
    vi.mocked(monitorApi.decisions).mockResolvedValue({ data: decisions() } as never)
    const p = props()
    render(<MemoryRouter initialEntries={['/pulse?tab=decisions']}><PulseScreen {...p} /></MemoryRouter>)
    expect(await screen.findByRole('heading', { name: 'Who decided' })).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'Agents alone 87%, Agents, approved by you 9%, You decided 4%' })).toBeInTheDocument()
    expect(within(screen.getByRole('list', { name: /last 7 days/ })).getAllByRole('listitem')).toHaveLength(7)
    expect(screen.getByText(/Median time to decide: 6 min\. 2 expired/)).toBeInTheDocument()
    const grid = screen.getByRole('table')
    expect(within(grid).getByRole('row', { name: /Budget reached/ })).toHaveTextContent('4')
    fireEvent.click(screen.getByRole('button', { name: 'Change the default budget' }))
    expect(p.goSettings).toHaveBeenCalledWith('ai-config')
  })
})
