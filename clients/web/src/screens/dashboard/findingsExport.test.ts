import { describe, expect, it } from 'vitest'
import type { Finding } from '../../data/data'
import { buildFindingsCsv } from './findingsExport'

const finding: Finding = {
  id: 'finding-1',
  sev: 'High',
  tech: 'T1059.001',
  conf: 93,
  tactic: 'Execution',
  src: 'sensor,west',
  host: 'host-1',
  user: 'analyst "one"',
  time: 'Jun 14, 17:30',
  ts: Date.parse('2026-06-14T17:30:00Z'),
  score: 0.91,
  status: 'working',
  caseId: '0042',
  extra: { device_id: 'device-7', analyst_note: '=WEBSERVICE("https://example.invalid")' },
}

describe('buildFindingsCsv', () => {
  it('exports stable fields, source-specific fields, and escaped cells', () => {
    const csv = buildFindingsCsv([finding])
    expect(csv).toContain('finding_id,severity,mitre_technique')
    expect(csv).toContain(',device_id\n')
    expect(csv).toContain('"sensor,west"')
    expect(csv).toContain('"analyst ""one"""')
    expect(csv).toContain('2026-06-14T17:30:00.000Z')
    expect(csv).toContain("'=WEBSERVICE")
    expect(csv).toContain(',0.91,Case 0042,')
    expect(csv.endsWith('\n')).toBe(true)
  })
})
