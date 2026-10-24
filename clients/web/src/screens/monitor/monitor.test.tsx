import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import FleetWallScreen from './FleetWallScreen'
import { monitorApi, type FleetWallPayload, type MonitorMetric, type WallCell } from '../../services/api'

vi.mock('../../services/api', () => ({
  monitorApi: { wall: vi.fn() },
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
