import { describe, expect, it } from 'vitest'
import {
  MISSING_FINDING_SCORE,
  MISSING_FINDING_SEVERITY,
  MISSING_FINDING_TIME,
} from './data'
import { formatFindingScore, mapApiCase, mapApiFinding, mapApiSkill, rankTechniques } from './mappers'
import type { ApiCase } from './mappers'

const caseStub = (priority?: string | null): ApiCase =>
  ({
    case_id: 'c-1',
    priority,
    activities: [],
    mitre_techniques: [],
    notes: [],
    resolution_steps: [],
    tags: [],
    timeline: [],
  })

describe('mapApiCase priority', () => {
  it('keeps unknown instead of falling through to medium', () => {
    expect(mapApiCase(caseStub('unknown')).prio).toBe('unknown')
  })

  it('still maps a rated priority', () => {
    expect(mapApiCase(caseStub('high')).prio).toBe('high')
  })
})

describe('mapApiSkill', () => {
  it('keys the loaded skill by name and surfaces its source path', () => {
    expect(mapApiSkill({
      name: 'triage',
      description: 'Triage a finding.',
      source_path: 'skills/triage',
      bundled: true,
      file_count: 3,
    })).toEqual({
      id: 'triage',
      name: 'triage',
      desc: 'Triage a finding.',
      source: 'skills/triage',
      bundled: true,
      fileCount: 3,
      reads7d: 0,
      agents7d: 0,
    })
  })

  it('carries the 7-day usage counts, defaulting an older API to zero', () => {
    const used = mapApiSkill({
      name: 'triage',
      description: 'Triage a finding.',
      source_path: 'skills/triage',
      bundled: true,
      file_count: 3,
      reads_7d: 5,
      agents_7d: 2,
    })
    expect(used.reads7d).toBe(5)
    expect(used.agents7d).toBe(2)
  })
})

describe('mapApiFinding missing source fields', () => {
  it('round-trips null score, severity, and timestamp to the console labels', () => {
    const finding = mapApiFinding({
      finding_id: 'f-null',
      severity: null,
      timestamp: null,
      anomaly_score: null,
      mitre_predictions: { 'Command and Control': 0.9 },
    })

    expect(finding.sev).toBe(MISSING_FINDING_SEVERITY)
    expect(finding.score).toBeNull()
    expect(formatFindingScore(finding.score)).toBe(MISSING_FINDING_SCORE)
    expect(finding.time).toBe(MISSING_FINDING_TIME)
    expect(finding.ts).toBeUndefined()
  })

  it('does not coerce an omitted score to zero or a missing severity to Medium', () => {
    const finding = mapApiFinding({ finding_id: 'f-omitted' })

    expect(finding.sev).toBe('Unrated')
    expect(finding.score).toBeNull()
    expect(finding.time).toBe('Source time unavailable')
  })

  it('still maps explicit medium / zero / a real timestamp', () => {
    const finding = mapApiFinding({
      finding_id: 'f-present',
      severity: 'medium',
      timestamp: '2026-07-21T12:00:00Z',
      anomaly_score: 0,
    })

    expect(finding.sev).toBe('Medium')
    expect(finding.score).toBe(0)
    expect(formatFindingScore(finding.score)).toBe('0.00')
    expect(finding.time).not.toBe(MISSING_FINDING_TIME)
    expect(finding.ts).toBe(Date.parse('2026-07-21T12:00:00Z'))
  })
})

describe('technique confidence', () => {
  it('ranks scored techniques first and never scores an unknown', () => {
    const preds = { T1071: null, T1003: 0, T1059: 0.4, T1105: 0.4, T1021: true } as unknown as Record<string, number | null>
    expect(rankTechniques(preds)).toEqual([
      ['T1059', 0.4], ['T1105', 0.4], ['T1003', 0], ['T1071', null], ['T1021', null],
    ])
    expect(mapApiFinding({ finding_id: 'f', mitre_predictions: preds })).toMatchObject({ tech: 'T1059', conf: 40 })
  })

  it('reads an unscored or absent top technique as null, and 0 as 0', () => {
    expect(mapApiFinding({ finding_id: 'f', mitre_predictions: { T1071: null } })).toMatchObject({ tech: 'T1071', conf: null })
    expect(mapApiFinding({ finding_id: 'f' })).toMatchObject({ tech: '—', conf: null })
    expect(mapApiFinding({ finding_id: 'f', mitre_predictions: { T1003: 0 } })).toMatchObject({ tech: 'T1003', conf: 0 })
  })
})
