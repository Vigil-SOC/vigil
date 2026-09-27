import { fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import FindingPopup from './FindingPopup'
import { exclusionsApi, findingsApi } from '../../services/api'

vi.mock('../../services/api', () => ({
  exclusionsApi: { create: vi.fn() },
  findingsApi: {
    getById: vi.fn(),
    getEnrichment: vi.fn(),
    update: vi.fn(),
    delete: vi.fn(),
  },
}))

const baseFinding = {
  finding_id: 'f-source-1',
  severity: 'high',
  data_source: 'loglm',
  timestamp: '2026-07-21T12:00:00Z',
  anomaly_score: 0.92,
  status: 'new',
  mitre_predictions: {},
  entity_context: {},
}

function openFinding(entityContext: Record<string, unknown>) {
  vi.mocked(findingsApi.getById).mockResolvedValueOnce({
    data: { ...baseFinding, entity_context: entityContext },
  } as never)
  return render(<FindingPopup id="f-source-1" onClose={vi.fn()} />)
}

describe('FindingPopup missing source fields', () => {
  beforeEach(() => vi.clearAllMocks())

  it('renders Unrated, Not provided, and Source time unavailable when those fields are missing', async () => {
    vi.mocked(findingsApi.getById).mockResolvedValueOnce({
      data: {
        finding_id: 'f-null',
        severity: null,
        timestamp: null,
        anomaly_score: null,
        status: 'new',
        mitre_predictions: {},
        entity_context: {},
      },
    } as never)

    render(<FindingPopup id="f-null" onClose={vi.fn()} />)

    expect(await screen.findByText('Unrated')).toBeInTheDocument()
    expect(screen.getByText('Not provided')).toBeInTheDocument()
    expect(screen.getByText('Source time unavailable')).toBeInTheDocument()
  })
})

describe('FindingPopup source evidence', () => {
  beforeEach(() => vi.clearAllMocks())

  it('hides the section when the finding has no declared evidence contract', async () => {
    openFinding({})
    await screen.findByText('f-source-1')
    expect(screen.queryByText('Source evidence')).not.toBeInTheDocument()
  })

  it('shows a truthful message when evidence was not in the artifact', async () => {
    openFinding({
      source_evidence: {
        version: 1,
        telemetry_kind: 'dns',
        schema_id: 'dns.v1',
        status: 'not_in_artifact',
        provenance: 'embedded',
      },
    })

    expect(await screen.findByText('Source evidence was not included in the ingested artifact.')).toBeInTheDocument()
    expect(screen.getByText('DNS:')).toBeInTheDocument()
  })

  it('renders NetFlow evidence collapsed with an accessible scroll table', async () => {
    openFinding({
      source_evidence: {
        version: 1,
        telemetry_kind: 'netflow',
        schema_id: 'netflow.v1',
        status: 'available',
        provenance: 'joined',
        total_records: 150,
        truncated: true,
        records: [{
          timestamp: '2026-07-21T12:00:00Z',
          source_ip: '10.0.0.1',
          source_port: 51515,
          destination_ip: '198.51.100.2',
          destination_port: 443,
          protocol: 6,
          forward_packets: 8,
          backward_packets: 5,
          forward_bytes: 2048,
          backward_bytes: 1024,
          duration_ms: 512,
        }],
      },
    })

    const summaryText = await screen.findByText('Source evidence')
    const disclosure = summaryText.closest('details') as HTMLDetailsElement
    expect(disclosure.open).toBe(false)
    expect(within(disclosure).getByText('1 of 150 records')).toBeInTheDocument()

    fireEvent.click(summaryText.closest('summary')!)
    expect(disclosure.open).toBe(true)
    const region = within(disclosure).getByRole('region', { name: 'NetFlow source evidence table' })
    expect(region).toHaveAttribute('tabindex', '0')
    expect(within(region).getByRole('columnheader', { name: 'Source' })).toBeInTheDocument()
    expect(within(region).getByText('10.0.0.1:51515')).toBeInTheDocument()
    expect(within(region).getByText('198.51.100.2:443')).toBeInTheDocument()
  })

  it('renders DNS records with the DNS-specific columns', async () => {
    openFinding({
      source_evidence: {
        version: 1,
        telemetry_kind: 'dns',
        schema_id: 'dns.v1',
        status: 'available',
        provenance: 'embedded',
        total_records: 1,
        truncated: false,
        records: [{
          timestamp: '2026-07-21T12:00:00Z',
          client_ip: '10.0.0.8',
          server_ip: '10.0.0.53',
          query: 'example.test',
          query_type: 'A',
          answer: '198.51.100.7',
          response_code: 'NOERROR',
          ttl: 300,
        }],
      },
    })

    const region = await screen.findByRole('region', { name: 'DNS source evidence table' })
    expect(within(region).getByRole('columnheader', { name: 'Query' })).toBeInTheDocument()
    expect(within(region).getByText('example.test')).toBeInTheDocument()
    expect(within(region).getByText('NOERROR')).toBeInTheDocument()
  })

  it('keeps generic records and raw source text available without a NetFlow assumption', async () => {
    openFinding({
      source_evidence: {
        version: 1,
        telemetry_kind: 'generic_log',
        schema_id: 'generic-log.v1',
        status: 'available',
        provenance: 'embedded',
        total_records: 1,
        truncated: false,
        records: [{ timestamp: '2026-07-21T12:00:00Z', event_type: 'process_start', pid: 42 }],
        raw_text: 'process_start pid=42',
      },
    })

    expect(await screen.findByText('Log events')).toBeInTheDocument()
    expect(screen.getByText('2026-07-21T12:00:00Z · process_start')).toBeInTheDocument()
    expect(screen.getByText('process_start pid=42')).toBeInTheDocument()
  })
})

describe('FindingPopup IP exclusions', () => {
  beforeEach(() => vi.clearAllMocks())

  it('marks excluded addresses and excludes another in place with a reason', async () => {
    vi.mocked(findingsApi.getById).mockResolvedValueOnce({
      data: {
        ...baseFinding,
        entity_context: { src_ip: '203.0.113.9', dest_ips: ['10.0.0.5', 'not-an-ip'] },
        excluded_ips: ['203.0.113.9'],
      },
    } as never)
    vi.mocked(exclusionsApi.create).mockResolvedValueOnce({ data: { ip: '10.0.0.5' } } as never)
    const onChanged = vi.fn()
    render(<FindingPopup id="f-source-1" onClose={vi.fn()} onChanged={onChanged} />)

    const scanner = (await screen.findByText('203.0.113.9')).closest('li') as HTMLElement
    expect(within(scanner).getByText('excluded')).toBeInTheDocument()
    expect(within(scanner).queryByRole('button', { name: 'Exclude' })).not.toBeInTheDocument()
    expect(screen.queryByText('not-an-ip')).not.toBeInTheDocument()

    const host = screen.getByText('10.0.0.5').closest('li') as HTMLElement
    fireEvent.click(within(host).getByRole('button', { name: 'Exclude' }))
    const submit = within(host).getByRole('button', { name: 'Exclude' })
    expect(submit).toBeDisabled()

    fireEvent.change(within(host).getByLabelText('Reason for excluding 10.0.0.5'), {
      target: { value: 'internal scanner' },
    })
    fireEvent.click(submit)

    expect(exclusionsApi.create).toHaveBeenCalledWith({
      ip: '10.0.0.5',
      reason: 'internal scanner',
      origin: 'finding',
      origin_ref: 'f-source-1',
    })
    expect(await within(host).findByText('excluded')).toBeInTheDocument()
    expect(onChanged).toHaveBeenCalled()
  })

  it('shows the server refusal instead of marking the address', async () => {
    vi.mocked(findingsApi.getById).mockResolvedValueOnce({
      data: { ...baseFinding, entity_context: { src_ip: '198.51.100.1' } },
    } as never)
    vi.mocked(exclusionsApi.create).mockRejectedValueOnce({
      response: { data: { detail: '198.51.100.1 is already excluded' } },
    })
    render(<FindingPopup id="f-source-1" onClose={vi.fn()} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Exclude' }))
    fireEvent.change(screen.getByLabelText('Reason for excluding 198.51.100.1'), { target: { value: 'dup' } })
    fireEvent.click(screen.getByRole('button', { name: 'Exclude' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('198.51.100.1 is already excluded')
    expect(screen.queryByText('excluded')).not.toBeInTheDocument()
  })
})
