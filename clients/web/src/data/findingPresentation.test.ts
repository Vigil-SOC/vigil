import { describe, expect, it } from 'vitest'
import { findingEndpoints, findingTitle, sourceTime, sourceTimestamp } from './findingPresentation'
import { mapApiFinding } from './mappers'

describe('finding presentation', () => {
  it('maps explicit endpoints before flattening metadata and rejects ambiguous or invalid roles', () => {
    expect(findingEndpoints({ source_ip: '192.0.2.2', src_ips: ['192.0.2.2'], dest_ips: ['198.51.100.10'] }))
      .toEqual({ sourceIp: '192.0.2.2', destinationIp: '198.51.100.10' })
    for (const source of [['192.0.2.2', '192.0.2.20'], ['192.0.2.2', 'invalid'], '192.0.2.2/32', '2001:db8::1', '192.000.2.2']) {
      expect(findingEndpoints({ source_ips: source }).sourceIp).toBeUndefined()
    }
    expect(findingEndpoints({ ips: ['192.0.2.2'], nodes: [{ source_ip: '192.0.2.2' }] })).toEqual({ sourceIp: undefined, destinationIp: undefined })
  })
  it('keeps source titles and missing scores instead of inventing detection claims', () => {
    const finding = mapApiFinding({ finding_id: 'synthetic-1', title: 'Unusual transfer', entity_context: { source_ip: '192.0.2.2', destination_ip: '198.51.100.10' } })
    expect(findingTitle(finding)).toBe('Unusual transfer')
    expect(finding.score).toBeNull()
    expect(finding.sev).toBe('Unrated')
    expect(finding.sourceIp).toBe('192.0.2.2')
    expect(findingTitle({ src: 'flow', tech: '—' })).toBe('Network flow finding')
  })
  it('interprets naive timestamps as UTC and retains explicit offsets', () => {
    expect(sourceTimestamp('2026-01-02T12:00:00')).toBe(sourceTimestamp('2026-01-02T12:00:00Z'))
    expect(sourceTimestamp('2026-01-02T07:00:00-05:00')).toBe(sourceTimestamp('2026-01-02T12:00:00Z'))
    expect(sourceTime('2026-01-02T12:00:00')).toContain('12:00:00 UTC')
    expect(sourceTime(null)).toBe('Source time unavailable')
    expect(sourceTimestamp('invalid')).toBeUndefined()
  })
})
