/* A probe's newest row decides whether it is awaiting a score; the newest
   scored row is the result shown. An unscored row must never read as a miss. */
import { describe, expect, it } from 'vitest'
import type { ApiFinding } from '../../data/mappers'
import { summarizeProbes } from './useHealth'

const NOW = Date.parse('2026-09-23T12:00:00Z')
const expected = { severity: ['high'], recommended_action: ['isolate'] }

const probeRow = (name: string, timestamp: string, outcome?: 'hit' | 'miss' | 'silent'): ApiFinding => ({
  finding_id: `probe:${name}:${timestamp}`,
  data_source: 'probe',
  timestamp,
  entity_context: {
    probe: {
      name,
      expected,
      ...(outcome && {
        score: {
          outcome,
          verdict: outcome === 'silent' ? null : { severity: 'high', recommended_action: 'isolate', confidence: 0.9 },
          time_to_verdict_s: outcome === 'silent' ? null : 42,
          scored_at: timestamp,
        },
      }),
    },
  },
})

describe('summarizeProbes', () => {
  it('groups by probe name and shows the newest scored row', () => {
    const { probes } = summarizeProbes([
      probeRow('c2', '2026-09-21T10:00:00Z', 'hit'),
      probeRow('c2', '2026-09-22T10:00:00Z', 'miss'),
      probeRow('patch', '2026-09-22T10:00:00Z', 'silent'),
    ], NOW)
    expect(probes.map((p) => [p.name, p.awaiting, p.latest?.outcome])).toEqual([
      ['c2', false, 'miss'],
      ['patch', false, 'silent'],
    ])
  })

  it('marks a probe awaiting when its newest row is unscored, keeping the older score as previous', () => {
    const { probes } = summarizeProbes([
      probeRow('c2', '2026-09-23T11:30:00Z'),
      probeRow('c2', '2026-09-22T11:30:00Z', 'hit'),
      probeRow('fresh', '2026-09-23T11:45:00Z'),
    ], NOW)
    expect(probes.find((p) => p.name === 'c2')).toMatchObject({ awaiting: true, latest: { outcome: 'hit' } })
    expect(probes.find((p) => p.name === 'fresh')).toMatchObject({ awaiting: true, latest: null })
  })

  it('tallies only scored rows from the last week', () => {
    const { tally } = summarizeProbes([
      probeRow('a', '2026-09-23T11:30:00Z'),
      probeRow('a', '2026-09-22T10:00:00Z', 'hit'),
      probeRow('a', '2026-09-21T10:00:00Z', 'hit'),
      probeRow('b', '2026-09-22T10:00:00Z', 'miss'),
      probeRow('c', '2026-09-22T10:00:00Z', 'silent'),
      probeRow('a', '2026-09-10T10:00:00Z', 'miss'),
    ], NOW)
    expect(tally).toEqual({ hit: 2, miss: 1, silent: 1 })
  })

  it('ignores rows without a probe block', () => {
    const stray: ApiFinding = { finding_id: 'x', data_source: 'probe', timestamp: '2026-09-23T10:00:00Z', entity_context: { hostnames: ['h'] } }
    expect(summarizeProbes([stray, { finding_id: 'y' }], NOW)).toEqual({ probes: [], tally: { hit: 0, miss: 0, silent: 0 } })
  })
})
