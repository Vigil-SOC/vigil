import { describe, expect, it } from 'vitest'
import { caseCitations } from './caseCitations'
import type { RunFold } from './caseFold'

const fold = {
  kind: 'hunt',
  hypotheses: [{ hypothesis_id: 'h-fb8e2227', statement: 'DUSKY LANTERN on FIN-WS-114' }],
  evidence: [
    { evidence_id: 'ev-0b17352e22bf', iteration: 3, source_system: 'CrowdStrike', summary: 'rundll32 loaded msupdate64.dll', is_gap: false, gap_detail: null },
    { evidence_id: 'ev-gap', iteration: 0, source_system: 'telemetry_search', summary: 'no tool answers telemetry_search', is_gap: true, gap_detail: 'No SIEM is connected' },
  ],
} as unknown as RunFold

describe('caseCitations (#2285)', () => {
  it('names a row by its step and source, a blind spot as one, an explanation by its statement and an alert by its title', () => {
    const cites = caseCitations(fold, [{ finding_id: 'dl-005', title: 'LSASS MiniDump', description: 'comsvcs.dll against lsass.exe', source_link: null, entities: [] }])
    expect(cites.map(({ id, kind, label, text }) => ({ id, kind, label, text }))).toEqual([
      { id: 'ev-0b17352e22bf', kind: 'evidence', label: 'step 3 · CrowdStrike', text: 'rundll32 loaded msupdate64.dll' },
      { id: 'ev-gap', kind: 'evidence', label: 'step 0 · blind spot', text: 'No SIEM is connected' },
      { id: 'h-fb8e2227', kind: 'explanation', label: 'explanation · DUSKY LANTERN on FIN-WS-114', text: 'DUSKY LANTERN on FIN-WS-114' },
      { id: 'dl-005', kind: 'alert', label: 'alert · LSASS MiniDump', text: 'comsvcs.dll against lsass.exe' },
    ])
    // an alert opens its own page in a new tab, so the case stays open
    expect(cites[3].href).toBe('/overview?alert=dl-005')
    expect(cites.slice(0, 3).every((c) => c.href === undefined)).toBe(true)
  })

  it('offers only alerts on a run that is not a hunt', () => {
    expect(caseCitations(null, [{ finding_id: 'f1', title: null, description: null, source_link: null, entities: [] }]).map((c) => c.label)).toEqual(['alert · f1'])
  })
})
