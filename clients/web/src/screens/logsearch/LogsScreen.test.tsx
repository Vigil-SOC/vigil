import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import LogsScreen from './LogsScreen'
import { formatQuery, parseQuery, termLabel, toCsv } from './logsQuery'
import { logsApi, type LogRow, type LogSearchResult } from '../../services/api'

vi.mock('../../services/api', () => ({
  logsApi: { search: vi.fn(), sources: vi.fn() },
}))

const NOW = Date.UTC(2026, 9, 27, 12, 0)

function event(overrides: Partial<LogRow> = {}): LogRow {
  return {
    id: 'ev-1',
    time: '2026-10-27T11:58:00Z',
    ts: (NOW - 2 * 60_000) / 1000,
    source: 'sysmon',
    class: 'Process Activity',
    severity: 'high',
    srcIp: '10.0.30.40',
    srcHost: 'ws-finance-01',
    dstIp: null,
    dstHost: null,
    dstPort: null,
    user: 'j.doe',
    action: 'process_start',
    summary: 'WINWORD.EXE started powershell.exe',
    loglm: null,
    mitre: 'T1059.001',
    incident: null,
    tags: [],
    raw: { CommandLine: 'powershell -nop -enc AAAA' },
    ...overrides,
  }
}

function result(events: LogRow[], overrides: Partial<LogSearchResult> = {}): LogSearchResult {
  return {
    total: events.length,
    events,
    bySource: events.reduce<Record<string, number>>((a, e) => ({ ...a, [e.source]: (a[e.source] ?? 0) + 1 }), {}),
    histogram: { counts: [0, 1, events.length - 1], incident: [0, 0, 0] },
    start: NOW - 86_400_000,
    end: NOW,
    newest: NOW - 60_000,
    ...overrides,
  }
}

const EVENTS = [
  event(),
  event({ id: 'ev-2', source: 'zeek', class: 'Network Activity', severity: 'medium', summary: 'ws-finance-01 -> 10.0.10.20 7000/tcp', dstIp: '10.0.10.20', dstPort: 7000, loglm: 0.93, incident: 'CASE-7', user: null, mitre: null }),
]

function Where() {
  const loc = useLocation()
  return <output data-testid="where">{loc.search}</output>
}

function renderAt(path = '/logs') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/logs" element={<><LogsScreen /><Where /></>} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.mocked(logsApi.search).mockReset().mockResolvedValue(result(EVENTS))
  vi.mocked(logsApi.sources).mockReset().mockResolvedValue([
    { source: 'sysmon', events: 1200, first: '2026-10-20T00:00:00Z', last: '2026-10-27T11:58:00Z' },
    { source: 'zeek', events: 800, first: '2026-10-20T00:00:00Z', last: '2026-10-27T11:59:00Z' },
  ])
})

describe('the search language', () => {
  it('reads terms the way the server does and writes them back', () => {
    const q = 'powershell "failed logon" host:web-01 -source:entra loglm>0.7 team:blue'
    const terms = parseQuery(q)
    expect(terms.map(termLabel)).toEqual([
      '“powershell”',
      '“failed logon”',
      'host is web-01',
      'source is not entra',
      'unusual > 0.7',
      '“team:blue”',
    ])
    expect(formatQuery(terms)).toBe(q)
    expect(termLabel(parseQuery('incident:*')[0])).toBe('tied to a case')
  })

  it('exports rows as CSV, quoting what needs it', () => {
    const csv = toCsv([event({ summary: 'a, "b"' })]).split('\n')
    expect(csv[0].startsWith('time,source,class')).toBe(true)
    expect(csv[1]).toContain('"a, ""b"""')
  })
})

describe('LogsScreen', () => {
  it('shows the matching events, newest first, with each source and its count', async () => {
    renderAt()
    expect(await screen.findByText('WINWORD.EXE started powershell.exe')).toBeInTheDocument()
    expect(screen.getByText('2 events', { exact: false })).toBeInTheDocument()
    expect(screen.getByText('2,000 stored', { exact: false })).toBeInTheDocument()
    const chip = screen.getByRole('button', { name: /^Zeek\s*1$/ })
    expect(chip).toHaveAttribute('aria-pressed', 'true')
    const args = vi.mocked(logsApi.search).mock.calls[0][0]
    expect(args.q).toBe('')
    expect(args.sources).toBeNull()
    expect(args.end - args.start).toBe(86_400_000)
  })

  it('searches what is typed and keeps it in the address', async () => {
    renderAt()
    await screen.findByText('WINWORD.EXE started powershell.exe')
    fireEvent.change(screen.getByLabelText('Search events'), { target: { value: 'user:j.doe -source:entra' } })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))
    await waitFor(() => expect(vi.mocked(logsApi.search).mock.lastCall?.[0].q).toBe('user:j.doe -source:entra'))
    expect(screen.getByTestId('where').textContent).toBe('?q=user%3Aj.doe+-source%3Aentra')
    const filters = screen.getByLabelText('Search filters')
    fireEvent.click(within(filters).getByRole('button', { name: 'Remove source is not entra' }))
    await waitFor(() => expect(vi.mocked(logsApi.search).mock.lastCall?.[0].q).toBe('user:j.doe'))
  })

  it('opens an event to its schema and original record, and pivots from a value', async () => {
    renderAt()
    fireEvent.click(await screen.findByText('WINWORD.EXE started powershell.exe'))
    expect(screen.getByText(/"CommandLine": "powershell -nop -enc AAAA"/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Leave out mitre:T1059.001' }))
    await waitFor(() => expect(vi.mocked(logsApi.search).mock.lastCall?.[0].q).toBe('-mitre:T1059.001'))
  })

  it('hides a source by sending the ones still shown', async () => {
    renderAt()
    await screen.findByText('WINWORD.EXE started powershell.exe')
    fireEvent.click(screen.getByRole('button', { name: /^Zeek\s*1$/ }))
    await waitFor(() => expect(vi.mocked(logsApi.search).mock.lastCall?.[0].sources).toEqual(['sysmon']))
  })

  it('reads ?q= and ?range= from a link', async () => {
    renderAt('/logs?q=host%3Aws-finance-01&range=7d')
    await screen.findByText('WINWORD.EXE started powershell.exe')
    const args = vi.mocked(logsApi.search).mock.calls[0][0]
    expect(args.q).toBe('host:ws-finance-01')
    expect(args.end - args.start).toBe(7 * 86_400_000)
    expect(screen.getByLabelText('Time range')).toHaveValue('7d')
  })

  it('says how events get in when it holds none', async () => {
    vi.mocked(logsApi.search).mockResolvedValue(result([]))
    vi.mocked(logsApi.sources).mockResolvedValue([])
    renderAt()
    expect(await screen.findByText('No events yet')).toBeInTheDocument()
    expect(screen.getByText(/\/api\/logs\/events/)).toBeInTheDocument()
  })

  it('shows what the server could not read in a search', async () => {
    renderAt()
    await screen.findByText('WINWORD.EXE started powershell.exe')
    vi.mocked(logsApi.search).mockRejectedValue({ response: { status: 422, data: { detail: 'start must be before end' } } })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('start must be before end')
  })

  it('offers a retry when the log store does not answer', async () => {
    vi.mocked(logsApi.search).mockRejectedValue(new Error('Network Error'))
    renderAt()
    expect(await screen.findByText("Couldn't reach the log store")).toBeInTheDocument()
    vi.mocked(logsApi.search).mockResolvedValue(result(EVENTS))
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByText('WINWORD.EXE started powershell.exe')).toBeInTheDocument()
  })
})
