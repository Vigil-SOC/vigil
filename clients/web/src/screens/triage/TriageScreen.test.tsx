import { describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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
        title: null,
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
        state_label: 'Added to a case',
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
        title: null,
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
    matched: 12,
    counts: {
      total: 12,
      kind: { detection: 7, schedule: 3, human_ask: 2 },
      source: { splunk: 7, Schedule: 3, Ask: 2 },
      state: { queued: 3, launched: 5, merged: 4 },
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
  it('draws the tiles in the board’s order, with Trust floor not measured', async () => {
    vi.mocked(triageApi.get).mockResolvedValue({ data: payload() } as never)
    renderScreen()
    const strip = await screen.findByLabelText('Intake strip')
    expect(screen.getByRole('heading', { name: 'Triage queue' })).toBeInTheDocument()
    const tiles = Array.from(strip.children).map((tile) => tile.getAttribute('aria-label'))
    expect(tiles).toEqual(['Picked up automatically', 'Cases created today', 'Waiting in line', 'Trust floor'])
    expect(within(strip).getByLabelText('Waiting in line')).toHaveTextContent('3')
    expect(within(strip).getByLabelText('Cases created today')).toHaveTextContent('1')
    expect(within(strip).getByLabelText('Trust floor')).toHaveTextContent('Not measured yet')
    expect(within(strip).getByLabelText('Picked up automatically')).not.toHaveTextContent('%')
  })

  it('shows a whole percent with its count, and no per-source strip', async () => {
    const base = payload()
    vi.mocked(triageApi.get).mockResolvedValue({
      data: { ...base, strip: { ...base.strip, picked_up: { launched_or_merged: 13, created_today: 15, share: 13 / 15 } } },
    } as never)
    renderScreen()
    const picked = within(await screen.findByLabelText('Intake strip')).getByLabelText('Picked up automatically')
    expect(picked).toHaveTextContent('87%')
    expect(picked).toHaveTextContent('13 of 15 today')
    expect(screen.queryByLabelText('Sources')).not.toBeInTheDocument()
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

  it('counts the chips from counts, not from the rows', async () => {
    vi.mocked(triageApi.get).mockResolvedValue({ data: payload() } as never)
    renderScreen()
    await screen.findByLabelText('Intake strip')
    const filters = within(screen.getByRole('group', { name: 'Filters' }))
    // two rows are loaded; every figure here is bigger than that
    expect(filters.getAllByRole('button', { name: 'All 12' })).toHaveLength(3)
    expect(filters.getByRole('button', { name: 'Alert 7' })).toBeInTheDocument()
    expect(filters.getAllByRole('button', { name: 'Ask 2' })).toHaveLength(2) // the Kind chip and the Source chip
    expect(filters.getByRole('button', { name: 'Splunk 7' })).toHaveAttribute('title', '2 arrived today · Quiet')
    expect(filters.getByRole('button', { name: 'Waiting for a slot 3' })).toBeInTheDocument()
    expect(filters.getByRole('button', { name: 'Picked up or started a case 5' })).toBeInTheDocument()
    expect(filters.getByRole('button', { name: 'Added to a case 4' })).toBeInTheDocument()
    expect(filters.getByRole('button', { name: 'Expired 0' })).toBeInTheDocument()
    // no filter and under the cap: no summary
    expect(screen.queryByText(/Showing/)).not.toBeInTheDocument()
    // the arrival ⓘ follows the Source label
    fireEvent.click(screen.getByRole('button', { name: payload().arrival_info }))
    expect(screen.getByRole('tooltip')).toHaveTextContent(payload().arrival_info)
  })

  it('sets a chip’s param and refetches; All clears it; Clear drops them all', async () => {
    vi.mocked(triageApi.get).mockResolvedValue({ data: payload() } as never)
    renderScreen()
    await screen.findByLabelText('Intake strip')
    const filters = within(screen.getByRole('group', { name: 'Filters' }))
    // the chip shows the label; the request keeps the stored id
    fireEvent.click(filters.getByRole('button', { name: 'Splunk 7' }))
    await waitFor(() => expect(triageApi.get).toHaveBeenLastCalledWith({ source: 'splunk' }))
    fireEvent.click(filters.getAllByRole('button', { name: 'All 12' })[1])
    await waitFor(() => expect(triageApi.get).toHaveBeenLastCalledWith({}))
    fireEvent.click(filters.getByRole('button', { name: 'Waiting for a slot 3' }))
    await waitFor(() => expect(triageApi.get).toHaveBeenLastCalledWith({ state: 'queued' }))
    expect(filters.getByRole('button', { name: 'Waiting for a slot 3' })).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(filters.getByRole('button', { name: 'Alert 7' }))
    await waitFor(() => expect(triageApi.get).toHaveBeenLastCalledWith({ kind: 'detection', state: 'queued' }))
    // the summary counts the rows shown against every row stored
    expect(screen.getByText(/Showing 2 of 12/)).toBeInTheDocument()

    fireEvent.click(filters.getAllByRole('button', { name: 'All 12' })[0])
    await waitFor(() => expect(triageApi.get).toHaveBeenLastCalledWith({ state: 'queued' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
    await waitFor(() => expect(triageApi.get).toHaveBeenLastCalledWith({}))
    expect(screen.queryByText(/Showing/)).not.toBeInTheDocument()
  })

  it('keeps a linked source as a selected chip with 0, and says when the cap hides rows', async () => {
    const base = payload()
    vi.mocked(triageApi.get).mockResolvedValue({ data: { ...base, matched: 340, counts: { ...base.counts, total: 340 } } } as never)
    renderScreen('/triage?source=microsoft_defender')
    await screen.findByLabelText('Intake strip')
    expect(screen.getByRole('button', { name: 'Defender 0' })).toHaveAttribute('aria-pressed', 'true')
    expect(triageApi.get).toHaveBeenCalledWith({ source: 'microsoft_defender' })
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
    expect(await screen.findByText('Showing 200 of 340')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Clear' })).not.toBeInTheDocument()
  })

  it('shows a stored source name as its label in the row', async () => {
    const base = payload()
    vi.mocked(triageApi.get).mockResolvedValue({ data: { ...base, rows: [{ ...base.rows[0], source: 'microsoft_defender' }] } } as never)
    renderScreen()
    await screen.findByLabelText('Intake strip')
    expect(screen.getByText('Defender')).toHaveClass('tq-source')
    expect(screen.queryByText('microsoft_defender')).not.toBeInTheDocument()
  })

  it('lays the rows out in the board’s columns and words', async () => {
    vi.mocked(triageApi.get).mockResolvedValue({ data: payload() } as never)
    renderScreen()
    await screen.findByLabelText('Intake strip')
    const headers = screen.getAllByRole('columnheader').map((th) => th.textContent)
    expect(headers).toEqual([
      'Kind', 'Source', 'Alert, in the source’s words', 'Score', 'Arrived', 'Picked up in', 'What happened', 'Grouped into, and why',
    ].map((name) => expect.stringContaining(name)))
    // nothing sorts
    expect(document.querySelector('th.sortable')).toBeNull()
    expect(screen.getByText('Started a case')).toHaveClass('tq-pill', 'launched')
    expect(screen.getByText('Added to a case', { selector: '.tq-pill' })).toHaveClass('merged')
    // Arrived is UTC, 24-hour, with the date before today; durations follow DESIGN §2
    expect(screen.getAllByText('1 Oct 00:00')).toHaveLength(2)
    expect(screen.getByText('4.0 s')).toBeInTheDocument()
    expect(screen.getByText('2.0 s')).toBeInTheDocument()
    // the case door leads "Grouped into", then why
    const door = screen.getByRole('link', { name: 'case-9' })
    expect(door).toHaveAttribute('href', '/cases?case=case-9')
    expect(door.closest('.tq-why')).toHaveTextContent('case-9 (new) · incident-response')
    fireEvent.click(door)
    expect(openCase).toHaveBeenCalledWith('case-9')
    expect(screen.queryByText('Cases page')).not.toBeInTheDocument()
    // a row without a door has no link and no id
    expect(screen.queryByText('inv-ghost')).not.toBeInTheDocument()
    expect(screen.getAllByRole('link')).toHaveLength(1)
  })

  it('formats durations the way DESIGN says', async () => {
    const base = payload()
    const row = (id: number, pickup: number) => ({ ...base.rows[1], id, pickup_seconds: pickup })
    vi.mocked(triageApi.get).mockResolvedValue({
      data: { ...base, rows: [row(1, 1.24), row(2, 42), row(3, 252), row(4, 3600 * 7 + 300)] },
    } as never)
    renderScreen()
    await screen.findByLabelText('Intake strip')
    for (const text of ['1.2 s', '42 s', '4 min 12 s', '7 h 5 min']) {
      expect(screen.getByText(text)).toBeInTheDocument()
    }
  })

  it('reads Arrived as UTC, with only the time for today', async () => {
    const base = payload()
    vi.setSystemTime(new Date('2026-10-07T23:30:00Z'))
    vi.mocked(triageApi.get).mockResolvedValue({
      data: { ...base, rows: [{ ...base.rows[1], created_at: '2026-10-07T14:22:00' }] },
    } as never)
    try {
      renderScreen()
      expect(await screen.findByText('14:22')).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('toggles the breakdown from the Score button, and the Score ⓘ carries its tip', async () => {
    vi.mocked(triageApi.get).mockResolvedValue({ data: payload() } as never)
    renderScreen()
    await screen.findByLabelText('Intake strip')
    fireEvent.click(within(screen.getByRole('columnheader', { name: /Score/ })).getByRole('button'))
    expect(screen.getByRole('tooltip')).toHaveTextContent('Score tip')

    const [first, second] = screen.getAllByRole('button', { name: 'Show how this was ranked' })
    expect(first).toHaveAttribute('title', 'Show how this was ranked')
    expect(first).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(first)
    const panel = await screen.findByLabelText('Expanded row')
    expect(panel).toHaveTextContent('How the ranking was worked out')
    expect(first).toHaveAttribute('aria-expanded', 'true')
    // one click toggles once: the button does not also fire the row's click
    expect(screen.getAllByLabelText('Expanded row')).toHaveLength(1)
    fireEvent.click(second)
    expect(first).toHaveAttribute('aria-expanded', 'false')
    expect(second).toHaveAttribute('aria-expanded', 'true')
    fireEvent.click(second)
    expect(screen.queryByLabelText('Expanded row')).not.toBeInTheDocument()
  })

  it('expands a detection under its row, and Rescue sends nothing', async () => {
    vi.mocked(triageApi.get).mockResolvedValue({ data: payload() } as never)
    renderScreen('/triage?source=splunk')
    await screen.findByLabelText('Intake strip')
    expect(triageApi.get).toHaveBeenCalledWith({ source: 'splunk' })

    fireEvent.click(screen.getByText('Odd login'))
    const panel = await screen.findByLabelText('Expanded row')
    // drawn as its own full-width row directly under the clicked row
    expect(panel.closest('tr')?.previousElementSibling).toBe(screen.getByText('Odd login').closest('tr'))
    expect(panel.closest('td')).toHaveAttribute('colspan', '8')
    expect(panel).toHaveTextContent('Severity bandcritical')
    expect(panel).toHaveTextContent('Time to live4 h')
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
    // a second click on the row closes it
    fireEvent.click(screen.getByText('Odd login'))
    expect(screen.queryByLabelText('Expanded row')).not.toBeInTheDocument()
  })

  it('shows a detection by its title, with the description in the cell title, else the finding id', async () => {
    const base = payload()
    const [detection, ask] = base.rows
    const rows = [
      { ...detection, id: 10, title: 'Brute force', description: 'raw event text' },
      { ...detection, id: 11, title: '', description: null, finding_id: 'f-bare' },
      ask,
    ]
    vi.mocked(triageApi.get).mockResolvedValue({ data: { ...base, rows } } as never)
    renderScreen()
    await screen.findByLabelText('Intake strip')
    expect(screen.getByText('Brute force')).toHaveAttribute('title', 'raw event text')
    expect(screen.getByText('f-bare')).toHaveAttribute('title', 'f-bare')
    expect(screen.getByText('please look')).toBeInTheDocument()
  })

  it('hides Open in source without a link, and shows an Ask’s document', async () => {
    vi.mocked(triageApi.get).mockResolvedValue({ data: payload() } as never)
    renderScreen()
    await screen.findByLabelText('Intake strip')
    // no title: the detection shows its description
    expect(screen.getByText('Odd login')).toHaveAttribute('title', 'Odd login')
    fireEvent.click(screen.getByText('please look'))
    await screen.findByLabelText('Expanded row')
    expect(screen.queryByRole('link', { name: 'Open in source' })).not.toBeInTheDocument()
    expect(screen.queryByText(/records omitted/)).not.toBeInTheDocument()
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

  it('says nothing is in intake with no filter, and nothing matches with one', async () => {
    vi.mocked(triageApi.get).mockResolvedValue({ data: payload({ rows: [] }) } as never)
    renderScreen()
    expect(await screen.findByText('Nothing in intake.')).toBeInTheDocument()
    cleanup()
    renderScreen('/triage?kind=schedule')
    expect(await screen.findByText('Nothing in the queue matches these filters.')).toBeInTheDocument()
  })
})
