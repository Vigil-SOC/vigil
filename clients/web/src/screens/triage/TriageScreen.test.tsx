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
    unmeasured_text: 'Not measured yet',
    ...overrides,
  }
}

function renderScreen(path = '/triage') {
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/triage" element={<TriageScreen openChat={vi.fn()} go={vi.fn()} goSettings={vi.fn()} setViewFull={vi.fn()} />} />
        <Route path="/cases" element={<p>Cases page</p>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('TriageScreen', () => {
  it('shows the strip, expands a detection, and Rescue sends nothing', async () => {
    vi.mocked(triageApi.get).mockResolvedValue({ data: payload() } as never)
    renderScreen('/triage?source=splunk')
    const strip = await screen.findByLabelText('Intake strip')
    expect(within(strip).getByLabelText('Waiting in line')).toHaveTextContent('3')
    expect(within(strip).getByLabelText('Trust floor')).toHaveTextContent('Not measured yet')
    const picked = within(strip).getByLabelText('Picked up automatically')
    expect(picked).not.toHaveTextContent('%')
    expect(within(strip).getByLabelText('splunk')).toHaveTextContent('Quiet')
    expect(within(strip).getByLabelText('never')).toHaveTextContent('Quiet')
    expect(triageApi.get).toHaveBeenCalledWith({ source: 'splunk' })

    fireEvent.click(screen.getByText('Alert'))
    expect(await screen.findByLabelText('Expanded row')).toHaveTextContent('Severity critical')
    expect(screen.getByRole('link', { name: 'https://console.example/alert/9' })).toHaveAttribute('href', 'https://console.example/alert/9')
    expect(screen.getByText(/records omitted from this list/)).toBeInTheDocument()
    const calls = vi.mocked(triageApi.get).mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Rescue' }))
    expect(screen.getByRole('button', { name: 'Rescue' })).toBeDisabled()
    expect(vi.mocked(triageApi.get).mock.calls.length).toBe(calls)

    expect(screen.getByRole('link', { name: 'Started a case' })).toHaveAttribute('href', '/cases?case=case-9')
    const ghost = screen.getByText('inv-ghost')
    expect(ghost.closest('a')).toBeNull()
  })
})
