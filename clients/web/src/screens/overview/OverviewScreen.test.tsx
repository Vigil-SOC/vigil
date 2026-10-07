import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import OverviewScreen from './OverviewScreen'
import api, { configApi, findingsApi, overviewApi, type OverviewAgent, type OverviewFeedItem, type OverviewPayload } from '../../services/api'

vi.mock('../../services/api', () => ({
  default: { post: vi.fn() },
  overviewApi: { get: vi.fn(), alert: vi.fn() },
  findingsApi: {
    markNoise: vi.fn(),
    clearNoise: vi.fn(),
    launchIntake: vi.fn(),
  },
  configApi: {
    getIntegrations: vi.fn(() => Promise.resolve({
      data: {
        enabled_integrations: ['jira'],
        integrations: { jira: { url: 'https://jira.example', username: 'ada', project_key: 'SOC' } },
        secrets_set: { jira: { api_token: true } },
      },
    })),
  },
}))

const SOURCE = 'Findings created today (UTC) with this data source.'
const OUTCOME = 'Alerts that arrived today (UTC) and reached this state. Each alert is counted once.'

const measured = (state: string, label: string, count: number, info: string | null = null) => ({
  state,
  label,
  count,
  source_text: OUTCOME,
  info,
  unmeasured_text: null,
})
const unmeasured = (state: string, label: string) => ({
  state,
  label,
  count: null,
  source_text: `${label} is not measured.`,
  info: `${label} is not measured.`,
  unmeasured_text: 'Not measured yet',
})
const OUTCOMES = [
  measured('resolved_auto', 'Resolved automatically', 0),
  measured('resolved_person', 'Closed by a person', 0),
  measured('working', 'Still working', 0),
  measured('needs_you', 'Needs you', 0, 'This count is alerts, not decisions.'),
  measured('waiting', 'Not in a case', 2),
  unmeasured('ticketed', 'Ticket created'),
  unmeasured('dropped', 'Dropped as noise'),
  unmeasured('paused', 'Paused'),
  unmeasured('stuck', 'Stuck'),
  unmeasured('incidents', 'Incidents'),
]

const agent = (over: Partial<OverviewAgent> = {}): OverviewAgent => ({
  workflow_id: 'incident-response',
  name: 'Incident Response',
  running: 1,
  sample_size: 0,
  rate: null,
  level: null,
  current_step: 'running',
  ...over,
})

function payload(overrides: Partial<OverviewPayload> = {}): OverviewPayload {
  return {
    day: '2026-10-01',
    empty: false,
    arrivals: [{ data_source: 'splunk', count: 2, source_text: SOURCE }],
    engine: { source_text: 'No count. The outcome counts partition today\'s arrivals.' },
    outcomes: OUTCOMES,
    running_source: 'Runs still in history whose status is running or paused.',
    step_source: 'The open phase on the newest live run, or that run\'s status when it has no phase row.',
    rate_info: 'A run that stopped at its budget counts as completed, because that is how the row is stored.',
    good_at: 0.95,
    fair_at: 0.85,
    agents: [agent()],
    feed: [{
      finding_id: 'f-1',
      severity: 'high',
      data_source: 'splunk',
      status: 'new',
      terminal_state: 'waiting',
      terminal_label: 'Not in a case',
      description: 'Odd login',
      created_at: '2026-10-01T00:01:00',
      evidence_links: [{ ref: 'https://example.test/alert/1' }],
      source_link: 'https://example.test/alert/1',
      case_id: null,
      noise_marked: false,
      source_evidence: {
        version: 1,
        telemetry_kind: 'dns',
        schema_id: 'dns.v1',
        status: 'not_in_artifact',
        provenance: 'embedded',
        payload_included: false,
      },
    }],
    ...overrides,
  }
}

function Where() {
  return <output data-testid="where">{useLocation().search}</output>
}

function renderScreen(url = '/overview') {
  const goSettings = vi.fn()
  const go = vi.fn()
  const setWallMode = vi.fn()
  const openCase = vi.fn()
  const { unmount } = render(
    <MemoryRouter initialEntries={[url]}>
      <OverviewScreen openChat={vi.fn()} go={go} goSettings={goSettings} openCase={openCase} setViewFull={vi.fn()} setWallMode={setWallMode} />
      <Where />
    </MemoryRouter>,
  )
  return { setWallMode, openCase, go, unmount }
}

const where = () => screen.getByTestId('where').textContent

describe('OverviewScreen', () => {
  it('empty: four connect slots, five outcome slots, no counts, and every card kept', async () => {
    vi.mocked(overviewApi.get).mockResolvedValue({ data: payload({ empty: true, arrivals: [], feed: [], agents: [agent({ running: 0, current_step: null })] }) } as never)
    renderScreen()
    const flow = await screen.findByRole('region', { name: 'How alerts flow through Vigil' })
    expect(within(flow).getByText(/Nothing is connected yet\. Connect a source on the left/)).toBeInTheDocument()
    expect(within(flow).getByText('Waiting for data')).toBeInTheDocument()
    for (const name of ['Connect a SIEM', 'Connect an EDR', 'Connect identity', 'Connect the LogLM pipeline']) {
      expect(within(flow).getByRole('link', { name: new RegExp(name) })).toHaveAttribute('href', '/settings?section=data')
    }
    for (const label of ['Resolved automatically', 'Closed by a person', 'Still working', 'Needs you', 'Not in a case']) {
      expect(within(flow).getByText(label)).toBeInTheDocument()
    }
    expect(flow.textContent).not.toMatch(/\d/)
    expect(within(flow).queryByText(/Not measured yet/)).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Agents running now' })).toBeInTheDocument()
    expect(screen.getByText('No agents running yet')).toBeInTheDocument()
    expect(screen.getAllByRole('link', { name: 'Connect data' })).toHaveLength(2)
    expect(screen.getByText(/No alerts yet · Connect a SIEM, an EDR or the LogLM pipeline/)).toBeInTheDocument()
    for (const link of screen.getAllByRole('link', { name: 'Connect data' })) expect(link).toHaveAttribute('href', '/settings?section=data')
    expect(screen.getByText(/Nothing is connected yet, so each part below shows where to connect/)).toBeInTheDocument()
  })

  it('with data: its own heading and legend, a source node per arrival, no connect copy', async () => {
    vi.mocked(overviewApi.get).mockResolvedValue({ data: payload({ feed: [] }) } as never)
    renderScreen()
    const flow = await screen.findByRole('region', { name: 'How alerts flow through Vigil' })
    expect(screen.getByRole('heading', { name: 'Overview', level: 1 })).toBeInTheDocument()
    expect(screen.getByText(/Today, UTC 2026-10-01\./)).toBeInTheDocument()
    expect(screen.getByTitle('Health: Good 95% and up, Fair 85 to 95%, Poor under 85%.')).toBeInTheDocument()
    const source = within(flow).getByRole('link', { name: /Splunk/ })
    expect(source).toHaveAttribute('href', '/triage?source=splunk')
    expect(source).toHaveTextContent('2 alerts')
    expect(source).toHaveAttribute('title', 'Splunk: 2 alerts today. Opens the Triage queue for this source.')
    expect(screen.getByText('No alerts.')).toBeInTheDocument()
    expect(screen.queryByText(/Nothing is connected yet/)).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Connect data' })).not.toBeInTheDocument()
  })

  it('agent cards: ordered by running then name, with step, pill, rate and border by level', async () => {
    vi.mocked(overviewApi.get).mockResolvedValue({
      data: payload({
        agents: [
          agent({ workflow_id: 'b', name: 'Beta', running: 0, current_step: null, sample_size: 0 }),
          agent({ workflow_id: 'a', name: 'Alpha', running: 0, current_step: null, sample_size: 412, rate: 0.979, level: 'good' }),
          agent({ workflow_id: 'c', name: 'Gamma', running: 3, current_step: 'Enrich', sample_size: 20, rate: 0.9, level: 'fair' }),
          agent({ workflow_id: 'd', name: 'Delta', running: 3, current_step: null, sample_size: 20, rate: 0.5, level: 'poor' }),
        ],
        good_at: 0.9,
        fair_at: 0.7,
      }),
    } as never)
    renderScreen()
    const cards = within(await screen.findByRole('list')).getAllByRole('listitem')
    expect(cards.map((c) => c.querySelector('b')?.textContent)).toEqual(['Delta', 'Gamma', 'Alpha', 'Beta'])
    const [delta, gamma, alpha, beta] = cards
    expect(within(delta).getByText('3 running')).toBeInTheDocument()
    expect(within(gamma).getByText('3 running · Enrich')).toBeInTheDocument()
    expect(delta).toHaveClass('poor')
    expect(gamma).toHaveClass('fair')
    expect(alpha).not.toHaveClass('fair', 'poor')
    expect(within(delta).getByText('Poor')).toBeInTheDocument()
    expect(within(gamma).getByText('Fair')).toBeInTheDocument()
    expect(within(alpha).getByText('Good')).toBeInTheDocument()
    expect(within(alpha).getByTitle('97.9% of 412 runs completed, last 30 days')).toHaveTextContent('97.9%')
    expect(within(alpha).getByText('0 running')).toBeInTheDocument()
    expect(within(beta).getByTitle('No finished runs in the last 30 days')).toHaveTextContent('—')
    expect(within(beta).queryByText(/Good|Fair|Poor/)).not.toBeInTheDocument()
    expect(screen.getByText(/Good means it finishes 90% or more of its runs; Fair 70 to 90%; Poor under 70%\./)).toBeInTheDocument()
  })

  it('agent cards: goes to workflows, and a connected install with no runs gets the panel without Connect data', async () => {
    vi.mocked(overviewApi.get).mockResolvedValue({ data: payload({ agents: [agent({ running: 0, current_step: null })] }) } as never)
    const { go } = renderScreen()
    expect(await screen.findByText('No agents running yet')).toBeInTheDocument()
    expect(screen.queryByRole('list')).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Connect data' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Agents & workflows →' }))
    expect(go).toHaveBeenCalledWith('workflows')
  })

  it('shows five outcome nodes with counts, names the unmeasured ones, and opens the feed', async () => {
    vi.mocked(overviewApi.get).mockResolvedValue({ data: payload() } as never)
    const { setWallMode } = renderScreen()
    const flow = await screen.findByRole('region', { name: 'How alerts flow through Vigil' })
    const counts = { 'Resolved automatically': '0', 'Closed by a person': '0', 'Still working': '0', 'Needs you': '0', 'Not in a case': '2' }
    for (const [label, count] of Object.entries(counts)) {
      const node = within(flow).getByLabelText(label)
      expect(node).toHaveTextContent(count)
      expect(node.querySelector('a')).toBeNull()
    }
    expect(within(flow).getByRole('button', { name: 'This count is alerts, not decisions.' })).toBeInTheDocument()
    expect(within(flow).queryByLabelText('Dropped as noise')).not.toBeInTheDocument()
    const line = within(flow).getByText(/Not measured yet:/)
    expect(line).toHaveTextContent('Not measured yet: Ticket created · Dropped as noise · Paused · Stuck · Incidents')
    expect(within(line).getByText('Paused')).toHaveAttribute('title', 'Paused is not measured.')
    fireEvent.click(screen.getByRole('button', { name: 'Full screen' }))
    expect(setWallMode).toHaveBeenCalledWith(true)
    fireEvent.click(await screen.findByText('f-1'))
    expect(await screen.findByRole('link', { name: 'Open in source' })).toHaveAttribute('href', 'https://example.test/alert/1')
    expect(screen.queryByRole('link', { name: 'https://example.test/alert/1' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create ticket' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'ServiceNow' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'ServiceNow' }).parentElement).toHaveAttribute('title', 'Coming in a later release')
    expect(screen.getByText(/records omitted from this list/)).toBeInTheDocument()
  })

  it('totals every arrival, and folds sources past the sixth into "+N more sources"', async () => {
    const names = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h']
    const arrivals = names.map((data_source, i) => ({ data_source, count: 10 - i, source_text: SOURCE }))
    vi.mocked(overviewApi.get).mockResolvedValue({ data: payload({ arrivals, feed: [] }) } as never)
    renderScreen()
    const flow = await screen.findByRole('region', { name: 'How alerts flow through Vigil' })
    expect(within(flow).getByText('52')).toBeInTheDocument() // 10+9+8+7+6+5+4+3, folded ones included
    expect(within(flow).getAllByRole('link', { name: /alerts/ })).toHaveLength(6)
    const more = within(flow).getByText('+2 more sources').closest('.ov-src')!
    expect(more).toHaveTextContent('7 alerts')
    expect(more.tagName).not.toBe('A')
    expect(within(flow).queryByText('g')).not.toBeInTheDocument()
  })

  it('marks then clears, says a second launch is already queued, and shows a Jira error', async () => {
    const item = {
      ...payload().feed[0],
      source_link: null,
      case_id: 'case-1',
      evidence_links: [{ ref: 'https://example.test/raw' }],
    }
    vi.mocked(overviewApi.get).mockResolvedValue({ data: payload({ feed: [item] }) } as never)
    vi.mocked(findingsApi.markNoise).mockResolvedValue({ data: {} } as never)
    vi.mocked(findingsApi.clearNoise).mockResolvedValue({ data: {} } as never)
    vi.mocked(findingsApi.launchIntake)
      .mockResolvedValueOnce({ data: { queued: true, already_queued: false, trigger_id: 1 } } as never)
      .mockResolvedValueOnce({ data: { queued: false, already_queued: true, trigger_id: null } } as never)
    vi.mocked(api.post).mockRejectedValue({ response: { data: { detail: 'JIRA not configured' } } })
    renderScreen()
    fireEvent.click(await screen.findByText('f-1'))
    expect(screen.queryByRole('link', { name: 'Open in source' })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'https://example.test/raw' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'The mark is stored and does not change scoring.' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Mark as noise' }))
    expect(await screen.findByRole('button', { name: 'Clear noise' })).toBeInTheDocument()
    expect(findingsApi.markNoise).toHaveBeenCalledWith('f-1')
    fireEvent.click(screen.getByRole('button', { name: 'Clear noise' }))
    expect(await screen.findByRole('button', { name: 'Mark as noise' })).toBeInTheDocument()
    expect(findingsApi.clearNoise).toHaveBeenCalledWith('f-1')
    fireEvent.click(screen.getByRole('button', { name: 'Send to triage' }))
    expect(await screen.findByRole('link', { name: 'Waiting in the Triage queue' })).toHaveAttribute('href', '/triage')
    fireEvent.click(screen.getByRole('button', { name: 'Send to triage' }))
    expect(await screen.findByRole('link', { name: 'Already waiting in the Triage queue.' })).toHaveAttribute('href', '/triage')
    await waitFor(() => expect(configApi.getIntegrations).toHaveBeenCalled())
    fireEvent.click(screen.getByRole('button', { name: 'Create ticket' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('JIRA not configured')
    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/cases/case-1/export/jira', { project_key: 'SOC' }))
    expect(screen.getByRole('button', { name: 'ServiceNow' })).toBeDisabled()
  })

  it('full screen hides the page heading and Agents running now, puts Exit in the diagram heading, and Escape leaves it', async () => {
    vi.mocked(overviewApi.get).mockResolvedValue({ data: payload() } as never)
    const { setWallMode } = renderScreen()
    const flow = await screen.findByRole('region', { name: 'How alerts flow through Vigil' })
    expect(screen.getByRole('heading', { name: 'Agents running now' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Overview', level: 1 })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Full screen' }))
    expect(setWallMode).toHaveBeenLastCalledWith(true)
    expect(screen.getAllByRole('button', { name: 'Exit full screen' })).toHaveLength(1)
    expect(within(flow).getByRole('button', { name: 'Exit full screen' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.queryByRole('heading', { name: 'Overview', level: 1 })).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Agents running now' })).not.toBeInTheDocument()
    expect(screen.getByText('f-1')).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(setWallMode).toHaveBeenLastCalledWith(false)
    expect(screen.getByRole('button', { name: 'Full screen' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Agents running now' })).toBeInTheDocument()
  })

  it('Escape with an alert open closes only the popup, not full screen', async () => {
    vi.mocked(overviewApi.get).mockResolvedValue({ data: payload() } as never)
    const { setWallMode } = renderScreen()
    await screen.findByRole('region', { name: 'How alerts flow through Vigil' })
    fireEvent.click(screen.getByRole('button', { name: 'Full screen' }))
    fireEvent.click(screen.getByText('f-1'))
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(screen.getByRole('button', { name: 'Exit full screen' })).toBeInTheDocument()
    expect(setWallMode).not.toHaveBeenCalledWith(false)
  })

  it('renders the heading and full screen button while loading, on error and when empty, with no Agents while loading or on error', async () => {
    vi.mocked(overviewApi.get).mockReturnValueOnce(new Promise(() => {}) as never)
    const first = renderScreen()
    expect(screen.getByRole('button', { name: 'Full screen' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Agents running now' })).not.toBeInTheDocument()
    first.unmount()
    vi.mocked(overviewApi.get).mockRejectedValueOnce(new Error('boom'))
    const second = renderScreen()
    expect(await screen.findByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Full screen' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Agents running now' })).not.toBeInTheDocument()
    second.unmount()
    vi.mocked(overviewApi.get).mockResolvedValue({ data: payload({ empty: true, arrivals: [], feed: [] }) } as never)
    renderScreen()
    expect(await screen.findAllByText(/Nothing is connected yet/)).toHaveLength(2)
    expect(screen.getByRole('button', { name: 'Full screen' })).toBeInTheDocument()
  })

  it('opens an alert from ?alert= with the feed row, and closing removes only that param', async () => {
    vi.mocked(overviewApi.get).mockResolvedValue({ data: payload() } as never)
    vi.mocked(overviewApi.alert).mockClear()
    renderScreen('/overview?alert=f-1&keep=1')
    expect(await screen.findByRole('button', { name: 'Mark as noise' })).toBeInTheDocument()
    expect(screen.getByText('Odd login', { selector: 'p' })).toBeInTheDocument()
    expect(overviewApi.alert).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: /close/i }))
    await waitFor(() => expect(where()).toBe('?keep=1'))
    fireEvent.click(await screen.findByText('f-1'))
    expect(where()).toBe('?keep=1&alert=f-1')
  })

  it('reads an alert outside the feed, starting from its noise mark', async () => {
    const old: OverviewFeedItem = { ...payload().feed[0], finding_id: 'f-old', description: 'Old one', noise_marked: true }
    vi.mocked(overviewApi.get).mockResolvedValue({ data: payload() } as never)
    vi.mocked(overviewApi.alert).mockResolvedValue({ data: old } as never)
    renderScreen('/overview?alert=f-old')
    expect(screen.getByText('Loading alert…')).toBeInTheDocument()
    expect(await screen.findByText('Old one', { selector: 'p' })).toBeInTheDocument()
    expect(overviewApi.alert).toHaveBeenCalledWith('f-old')
    expect(screen.getByRole('button', { name: 'Clear noise' })).toBeInTheDocument()
  })

  it('says an unknown alert is not found, and a failed read can be retried', async () => {
    vi.mocked(overviewApi.get).mockResolvedValue({ data: payload() } as never)
    vi.mocked(overviewApi.alert).mockRejectedValueOnce({ response: { status: 404 } })
    const { unmount } = renderScreen('/overview?alert=nope')
    expect(await screen.findByText('Alert nope not found.')).toBeInTheDocument()
    unmount()
    vi.mocked(overviewApi.alert)
      .mockRejectedValueOnce({ response: { status: 500 } })
      .mockResolvedValueOnce({ data: { ...payload().feed[0], finding_id: 'f-9', description: 'Back' } } as never)
    renderScreen('/overview?alert=f-9')
    expect(await screen.findByRole('alert')).toHaveTextContent('Couldn’t load alert f-9.')
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('Back', { selector: 'p' })).toBeInTheDocument()
  })

  it('opens the case from the popup and from the Case column, without stacking overlays', async () => {
    const item = { ...payload().feed[0], case_id: 'case-7' }
    const other = { ...payload().feed[0], finding_id: 'f-2', case_id: null }
    vi.mocked(overviewApi.get).mockResolvedValue({ data: payload({ feed: [item, other] }) } as never)
    const { openCase } = renderScreen()
    const column = await screen.findByRole('button', { name: 'Case case-7' })
    expect(screen.getAllByRole('button', { name: /^Case / })).toHaveLength(1)
    fireEvent.click(column)
    expect(openCase).toHaveBeenCalledWith('case-7')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(where()).toBe('')

    fireEvent.click(screen.getByText('f-2'))
    expect(screen.queryByRole('button', { name: 'Open case' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /close/i }))
    fireEvent.click(screen.getByText('f-1'))
    fireEvent.click(await screen.findByRole('button', { name: 'Open case' }))
    expect(openCase).toHaveBeenLastCalledWith('case-7')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(where()).toBe('')
  })
})
