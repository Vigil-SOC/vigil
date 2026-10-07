import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import OverviewScreen from './OverviewScreen'
import api, { configApi, findingsApi, overviewApi, type OverviewPayload } from '../../services/api'

vi.mock('../../services/api', () => ({
  default: { post: vi.fn() },
  overviewApi: { get: vi.fn() },
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

function payload(overrides: Partial<OverviewPayload> = {}): OverviewPayload {
  return {
    day: '2026-10-01',
    empty: false,
    arrivals: [{ data_source: 'splunk', count: 2, source_text: SOURCE }],
    engine: { source_text: 'No count. The outcome counts partition today\'s arrivals.' },
    outcomes: [
      { state: 'waiting', label: 'Not in a case', count: 2, source_text: OUTCOME, info: null, unmeasured_text: null },
      {
        state: 'needs_you',
        label: 'Needs you',
        count: 0,
        source_text: OUTCOME,
        info: 'This count is alerts, not decisions.',
        unmeasured_text: null,
      },
      {
        state: 'dropped',
        label: 'Dropped as noise',
        count: null,
        source_text: 'There is no score floor, and a noise mark is not a disposition.',
        info: 'There is no score floor, and a noise mark is not a disposition.',
        unmeasured_text: 'Not measured yet',
      },
    ],
    running_source: 'Runs still in history whose status is running or paused.',
    step_source: 'The open phase on the newest live run, or that run\'s status when it has no phase row.',
    rate_info: 'A run that stopped at its budget counts as completed, because that is how the row is stored.',
    good_at: 0.95,
    fair_at: 0.85,
    agents: [{
      workflow_id: 'incident-response',
      name: 'Incident Response',
      running: 1,
      sample_size: 0,
      rate: null,
      level: null,
      current_step: 'running',
    }],
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

function renderScreen() {
  const goSettings = vi.fn()
  const setWallMode = vi.fn()
  render(
    <MemoryRouter>
      <OverviewScreen openChat={vi.fn()} go={vi.fn()} goSettings={goSettings} openCase={vi.fn()} setViewFull={vi.fn()} setWallMode={setWallMode} />
    </MemoryRouter>,
  )
  return { goSettings, setWallMode }
}

describe('OverviewScreen', () => {
  it('prompts for Settings when nothing is enabled and nothing arrived', async () => {
    vi.mocked(overviewApi.get).mockResolvedValue({ data: payload({ empty: true, arrivals: [], outcomes: [] }) } as never)
    const { goSettings } = renderScreen()
    expect(await screen.findByText('No sources enabled')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Settings' }))
    expect(goSettings).toHaveBeenCalledWith('federation')
    expect(screen.queryByLabelText("Today's flow")).not.toBeInTheDocument()
  })

  it('shows arrival counts, an engineless number, and the feed', async () => {
    vi.mocked(overviewApi.get).mockResolvedValue({ data: payload() } as never)
    const { setWallMode } = renderScreen()
    const flow = await screen.findByLabelText("Today's flow")
    const source = within(flow).getByLabelText('splunk')
    expect(within(source).getByRole('link', { name: '2' })).toHaveAttribute('href', '/triage?source=splunk')
    expect(within(flow).getByLabelText('Not in a case').querySelector('a')).toBeNull()
    const engine = within(flow).getByLabelText('Engine')
    expect(engine.textContent).not.toMatch(/\d/)
    fireEvent.click(screen.getByRole('button', { name: 'Wall' }))
    expect(setWallMode).toHaveBeenCalledWith(true)
    fireEvent.click(await screen.findByText('f-1'))
    expect(await screen.findByRole('link', { name: 'Open in source' })).toHaveAttribute('href', 'https://example.test/alert/1')
    expect(screen.queryByRole('link', { name: 'https://example.test/alert/1' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create ticket' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'ServiceNow' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'ServiceNow' }).parentElement).toHaveAttribute('title', 'Coming in a later release')
    expect(screen.getByText(/records omitted from this list/)).toBeInTheDocument()
  })

  it('renders an unmeasured outcome as words rather than zero', async () => {
    vi.mocked(overviewApi.get).mockResolvedValue({ data: payload() } as never)
    renderScreen()
    const node = await screen.findByLabelText('Dropped as noise')
    expect(within(node).getByText('Not measured yet')).toBeInTheDocument()
    expect(within(node).queryByText('0')).not.toBeInTheDocument()
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
    fireEvent.click(screen.getByRole('button', { name: 'Launch' }))
    fireEvent.click(screen.getByRole('button', { name: 'Launch' }))
    expect(await screen.findByText('This finding is already queued.')).toBeInTheDocument()
    await waitFor(() => expect(configApi.getIntegrations).toHaveBeenCalled())
    fireEvent.click(screen.getByRole('button', { name: 'Create ticket' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('JIRA not configured')
    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/cases/case-1/export/jira', { project_key: 'SOC' }))
    expect(screen.getByRole('button', { name: 'ServiceNow' })).toBeDisabled()
  })
})
