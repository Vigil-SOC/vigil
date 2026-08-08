import { fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import FindingPopup from './FindingPopup'
import { findingsApi } from '../../../services/api'

vi.mock('../../../services/api', () => ({
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
