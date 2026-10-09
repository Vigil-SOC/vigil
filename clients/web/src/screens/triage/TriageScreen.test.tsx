import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import TriageScreen from './TriageScreen'
import { triageApi, type TriagePayload } from '../../services/api'

vi.mock('../../services/api', () => ({
  triageApi: { get: vi.fn() },
}))

function payload(overrides: Partial<TriagePayload> = {}): TriagePayload {
  return {
    rows: [
      {
        id: 1,
        kind: 'detection',
        kind_label: 'Alert',
        state: 'launched',
        state_label: 'Started a case',
        source: 'splunk',
        severity_band: 'critical',
        age_seconds: 12,
        ttl_seconds: 14400,
        last_quarter: false,
        score: null,
        trust: null,
        weight: null,
        pickup_seconds: 4,
        workflow_id: 'incident-response',
        case_door: 'case-9',
        document: null,
        source_link: 'https://console.example/alert/9',
        source_evidence: {
          version: 1,
          telemetry_kind: 'dns',
          schema_id: 'dns.v1',
          status: 'not_in_artifact',
          provenance: 'embedded',
          payload_included: false,
        },
        description: 'Odd login',
        finding_id: 'f-9',
        created_at: '2026-10-01T00:00:00',
        decided_at: '2026-10-01T00:00:04',
      },
      {
        id: 2,
        kind: 'human_ask',
        kind_label: 'Ask',
        state: 'merged',
        state_label: 'inv-ghost',
        source: 'Ask',
        severity_band: 'medium',
        age_seconds: 30,
        ttl_seconds: 14400,
        last_quarter: false,
        score: null,
        trust: null,
        weight: null,
        pickup_seconds: 2,
        workflow_id: '',
        case_door: null,
        document: 'please look',
        source_link: null,
        source_evidence: null,
        description: null,
        finding_id: null,
        created_at: '2026-10-01T00:00:00',
        decided_at: '2026-10-01T00:00:02',
      },
    ],
    strip: {
      picked_up: { launched_or_merged: 0, created_today: 0, share: null },
      waiting: 3,
      cases_created_today: 1,
      trust_floor: 'Not measured yet',
    },
    sources: [
      { data_source: 'splunk', arrivals: 2, lag_seconds: 90, quiet: true },
      { data_source: 'never', arrivals: 0, lag_seconds: null, quiet: true },
    ],
    arrival_info: 'Arrivals count every finding stored today. The list is the intake rows.',
    strip_info: {
      picked_up: { source: 'Picked source', calculation: 'Picked calc' },
      waiting: { source: 'Waiting source', calculation: 'Waiting calc' },
      cases_created_today: { source: 'Cases source', calculation: 'Cases calc' },
      trust_floor: { source: 'Floor source', calculation: 'Floor calc', limit: 'Floor limit' },
    },
    breakdown_info: { trust: 'Trust tip', weight: 'Weight tip', score: 'Score tip' },
    unmeasured_text: 'Not measured yet',
    ...overrides,
  }
}

const openCase = vi.fn()

function renderScreen(path = '/triage') {
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/triage" element={<TriageScreen openChat={vi.fn()} go={vi.fn()} goSettings={vi.fn()} openCase={openCase} setViewFull={vi.fn()} />} />
        <Route path="/cases" element={<p>Cases page</p>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('TriageScreen', () => {
  it('shows the strip, expands a detection under its row, and Rescue sends nothing', async () => {
    vi.mocked(triageApi.get).mockResolvedValue({ data: payload() } as never)
    renderScreen('/triage?source=splunk')
    const strip = await screen.findByLabelText('Intake strip')
    expect(within(strip).getByLabelText('Waiting in line')).toHaveTextContent('3')
    expect(within(strip).getByLabelText('Trust floor')).toHaveTextContent('Not measured yet')
    const picked = within(strip).getByLabelText('Picked up automatically')
    expect(picked).not.toHaveTextContent('%')
    expect(within(strip).getByLabelText('splunk')).toHaveTextContent('Quiet')
    expect(within(strip).getByLabelText('never')).toHaveTextContent('Quiet')
    const info = payload().arrival_info
    expect(within(within(strip).getByLabelText('splunk')).getByRole('button', { name: info })).toBeInTheDocument()
    expect(within(within(strip).getByLabelText('never')).getByRole('button', { name: info })).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: info })).toHaveLength(3)
    expect(triageApi.get).toHaveBeenCalledWith({ source: 'splunk' })

    fireEvent.click(screen.getByText('Odd login'))
    const panel = await screen.findByLabelText('Expanded row')
    // drawn as its own full-width row directly under the clicked row
    expect(panel.closest('tr')?.previousElementSibling).toBe(screen.getByText('Odd login').closest('tr'))
    expect(panel.closest('td')).toHaveAttribute('colspan', '9')
    expect(panel).toHaveTextContent('How the ranking was worked out')
    expect(panel).toHaveTextContent('Severity bandcritical')
    expect(panel).toHaveTextContent('Time to live4h')
    expect(panel).toHaveTextContent('In the last quarter of its waitNo')
    for (const label of ['Source trust', 'Weight', 'Score against a floor']) {
      expect(panel).toHaveTextContent(`${label}Not measured yet`)
    }
    expect(within(panel).getAllByRole('button', { name: /tip$/ })).toHaveLength(3)
    expect(screen.getByRole('link', { name: 'Open in source' })).toHaveAttribute('href', 'https://console.example/alert/9')
    expect(screen.getByRole('link', { name: 'Open in source' })).toHaveAttribute('target', '_blank')
    expect(screen.getByRole('link', { name: 'Open in source' })).toHaveAttribute('rel', 'noopener noreferrer')
    expect(screen.getByText(/records omitted from this list/)).toBeInTheDocument()
    const calls = vi.mocked(triageApi.get).mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Rescue' }))
    expect(screen.getByRole('button', { name: 'Rescue' })).toBeDisabled()
    expect(vi.mocked(triageApi.get).mock.calls.length).toBe(calls)
    // a click inside the panel leaves it open
    fireEvent.click(panel)
    expect(screen.getByLabelText('Expanded row')).toBeInTheDocument()

    expect(screen.getByRole('link', { name: 'Started a case' })).toHaveAttribute('href', '/cases?case=case-9')
    fireEvent.click(screen.getByRole('link', { name: 'Started a case' }))
    expect(openCase).toHaveBeenCalledWith('case-9')
    expect(screen.queryByText('Cases page')).not.toBeInTheDocument()
    const ghost = screen.getByText('inv-ghost')
    expect(ghost.closest('a')).toBeNull()

    // a second click on the row closes it
    fireEvent.click(screen.getByText('Odd login'))
    expect(screen.queryByLabelText('Expanded row')).not.toBeInTheDocument()
  })

  it('hides Open in source without a link and shows the new columns', async () => {
    vi.mocked(triageApi.get).mockResolvedValue({ data: payload() } as never)
    renderScreen()
    await screen.findByLabelText('Intake strip')
    for (const name of ['Alert, in the source’s words', 'Arrived']) {
      expect(screen.getByRole('columnheader', { name })).toBeInTheDocument()
    }
    // an Ask shows its document line; the detection shows its description, truncated by CSS with the full text in title
    expect(screen.getByText('please look')).toBeInTheDocument()
    expect(screen.getByText('Odd login')).toHaveAttribute('title', 'Odd login')
    // naive UTC created_at is read as UTC, not local
    expect(screen.getAllByText(new Date('2026-10-01T00:00:00Z').toLocaleString())).toHaveLength(2)

    fireEvent.click(screen.getByText('please look'))
    await screen.findByLabelText('Expanded row')
    expect(screen.queryByRole('link', { name: 'Open in source' })).not.toBeInTheDocument()
    expect(screen.queryByText(/records omitted/)).not.toBeInTheDocument()
  })

  it('names the state filter options in the column’s words', async () => {
    vi.mocked(triageApi.get).mockResolvedValue({ data: payload() } as never)
    renderScreen()
    await screen.findByLabelText('Intake strip')
    fireEvent.click(screen.getByRole('button', { name: /Filter/ }))
    for (const label of ['Waiting', 'Picked up or started a case', 'Added to a case', 'Expired']) {
      expect(screen.getByText(label)).toBeInTheDocument()
    }
    expect(screen.queryByText('Launched')).not.toBeInTheDocument()
  })

  it('opens an ⓘ on each strip figure from the payload', async () => {
    vi.mocked(triageApi.get).mockResolvedValue({ data: payload() } as never)
    renderScreen()
    const strip = await screen.findByLabelText('Intake strip')
    const cases: [string, string, string][] = [
      ['Picked up automatically', 'Picked source', 'Picked calc'],
      ['Waiting in line', 'Waiting source', 'Waiting calc'],
      ['Cases created today', 'Cases source', 'Cases calc'],
    ]
    for (const [tile, source, calculation] of cases) {
      const scope = within(within(strip).getByLabelText(tile))
      fireEvent.click(scope.getByRole('button'))
      expect(scope.getByRole('tooltip')).toHaveTextContent(`Source ${source}`)
      expect(scope.getByRole('tooltip')).toHaveTextContent(`Calculation ${calculation}`)
    }
    const floor = within(within(strip).getByLabelText('Trust floor'))
    fireEvent.click(floor.getByRole('button'))
    expect(floor.getByRole('tooltip')).toHaveTextContent('Floor source Floor calc Floor limit')
  })

  it('shows the loading state', async () => {
    vi.mocked(triageApi.get).mockReturnValue(new Promise(() => {}) as never)
    renderScreen()
    expect(screen.getByText('Loading triage…')).toBeInTheDocument()
  })

  it('shows the error state with a retry', async () => {
    vi.mocked(triageApi.get).mockRejectedValue(new Error('down'))
    renderScreen()
    expect((await screen.findAllByText('Couldn’t load triage')).length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })

  it('shows the empty state when intake has no rows', async () => {
    vi.mocked(triageApi.get).mockResolvedValue({ data: payload({ rows: [] }) } as never)
    renderScreen()
    expect(await screen.findByText('Nothing in intake.')).toBeInTheDocument()
  })
})
