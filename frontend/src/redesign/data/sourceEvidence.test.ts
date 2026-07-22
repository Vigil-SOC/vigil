import { describe, expect, it } from 'vitest'
import { parseSourceEvidence } from './sourceEvidence'

describe('parseSourceEvidence', () => {
  it('parses a bounded NetFlow envelope', () => {
    const result = parseSourceEvidence({
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
        destination_ip: '198.51.100.2',
      }],
    })

    expect(result).toMatchObject({
      telemetryKind: 'netflow',
      status: 'available',
      provenance: 'joined',
      totalRecords: 150,
      truncated: true,
    })
    expect(result?.records).toHaveLength(1)
  })

  it('preserves explicit unavailable states and hides an absent contract', () => {
    expect(parseSourceEvidence(undefined)).toBeUndefined()
    expect(parseSourceEvidence({
      version: 1,
      telemetry_kind: 'dns',
      schema_id: 'dns.v1',
      status: 'not_in_artifact',
      provenance: 'embedded',
    })).toMatchObject({ telemetryKind: 'dns', status: 'not_in_artifact' })
  })

  it('fails malformed or mismatched records closed', () => {
    expect(parseSourceEvidence('{bad-json')).toMatchObject({ status: 'invalid' })
    expect(parseSourceEvidence({
      version: 1,
      telemetry_kind: 'dns',
      schema_id: 'dns.v1',
      status: 'available',
      provenance: 'embedded',
      records: [{ response_code: 'NOERROR' }],
    })).toMatchObject({ status: 'invalid' })
  })
})
