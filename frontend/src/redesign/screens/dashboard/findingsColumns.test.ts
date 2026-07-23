import { describe, it, expect } from 'vitest'
import { mapApiFinding } from '../../data/mappers'
import { extraFindingColumns, baseFindingColumns } from './findingsColumns'

// CrowdStrike emits device_id and no dest_ips; Splunk emits dest_ips. The
// dashboard must surface both without knowing either in advance.
const crowdstrike = mapApiFinding({
  finding_id: 'CS-1', severity: 'high', data_source: 'crowdstrike',
  entity_context: { hostnames: ['h1'], usernames: ['u1'], src_ips: ['1.1.1.1'], device_id: 'abc123' },
} as never)
const splunk = mapApiFinding({
  finding_id: 'SP-1', severity: 'low', data_source: 'splunk',
  entity_context: { hostnames: ['h2'], usernames: ['u2'], dest_ips: ['2.2.2.2', '3.3.3.3'] },
} as never)

describe('adaptive findings columns', () => {
  it('keeps source-specific entity keys instead of dropping them', () => {
    expect(crowdstrike.extra).toEqual({ src_ips: '1.1.1.1', device_id: 'abc123' })
    expect(splunk.extra).toEqual({ dest_ips: '2.2.2.2, 3.3.3.3' })
  })

  it('does not duplicate keys already shown as fixed columns', () => {
    expect(crowdstrike.extra).not.toHaveProperty('hostnames')
    expect(crowdstrike.extra).not.toHaveProperty('usernames')
  })

  it('derives a column per distinct extra key across the union of rows', () => {
    const cols = extraFindingColumns([crowdstrike, splunk])
    expect(cols.map((c) => c.key)).toEqual(['extra:dest_ips', 'extra:device_id', 'extra:src_ips'])
    expect(cols.map((c) => c.label)).toEqual(['Dest Ips', 'Device Id', 'Src Ips'])
    expect(cols.every((c) => c.visible === false)).toBe(true)
  })

  it('renders an em-dash, not undefined, when a row lacks the key', () => {
    const cols = extraFindingColumns([crowdstrike, splunk])
    const deviceCol = cols.find((c) => c.key === 'extra:device_id')!
    expect(deviceCol.render(splunk)).toMatchObject({ props: { children: '—' } })
  })

  it('preserves the default 11-column view', () => {
    const base = baseFindingColumns(() => {}, () => {})
    expect(base).toHaveLength(11)
    expect(base.filter((c) => c.sortVal).map((c) => c.key)).toEqual(['sev', 'time', 'score', 'status'])
    // same five fields the old hardcoded search covered, in column order
    expect(base.filter((c) => c.searchVal).map((c) => c.key)).toEqual(['id', 'tech', 'src', 'host', 'user'])
  })
})
