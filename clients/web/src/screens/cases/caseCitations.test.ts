import { describe, expect, it } from 'vitest'
import { caseCitations, evidenceInNote } from './caseCitations'
import type { RunFold } from './caseFold'

const fold = {
  kind: 'hunt',
  hypotheses: [{ hypothesis_id: 'h-fb8e2227', statement: 'DUSKY LANTERN on FIN-WS-114' }],
  evidence: [
    // #2287: the system its rows came from, not the hunt's domain word.
    { evidence_id: 'ev-0b17352e22bf', iteration: 3, source_system: 'process_lineage', origins: ['crowdstrike'], summary: 'rundll32 loaded msupdate64.dll', is_gap: false, gap_detail: null },
    { evidence_id: 'ev-gap', iteration: 0, source_system: 'deployment', origins: [], summary: 'No SIEM is connected, so the hunt could not search raw telemetry', is_gap: true, gap_detail: null },
    // No system known: the domain is all a chip can name.
    { evidence_id: 'ev-dns', iteration: 4, source_system: 'dns', origins: [], summary: 'one lookup', is_gap: false, gap_detail: null },
  ],
} as unknown as RunFold

describe('caseCitations (#2285)', () => {
  it('names a row by its step and source, a blind spot as one, an explanation by its statement and an alert by its title', () => {
    const cites = caseCitations(fold, [{ finding_id: 'dl-005', title: 'LSASS MiniDump', description: 'comsvcs.dll against lsass.exe', source_link: null, entities: [] }])
    expect(cites.map(({ id, kind, label, text }) => ({ id, kind, label, text }))).toEqual([
      { id: 'ev-0b17352e22bf', kind: 'evidence', label: 'step 3 · CrowdStrike', text: 'rundll32 loaded msupdate64.dll' },
      { id: 'ev-gap', kind: 'evidence', label: 'blind spot', text: 'No SIEM is connected, so the hunt could not search raw telemetry' },
      { id: 'ev-dns', kind: 'evidence', label: 'step 4 · dns', text: 'one lookup' },
      { id: 'h-fb8e2227', kind: 'explanation', label: 'explanation · DUSKY LANTERN on FIN-WS-114', text: 'DUSKY LANTERN on FIN-WS-114' },
      { id: 'dl-005', kind: 'alert', label: 'alert · LSASS MiniDump', text: 'comsvcs.dll against lsass.exe' },
    ])
    // an alert opens its own page in a new tab, so the case stays open
    expect(cites[4].href).toBe('/overview?alert=dl-005')
    expect(cites.slice(0, 4).every((c) => c.href === undefined)).toBe(true)
  })

  it('tells apart rows that would share a label by their own text, and leaves a unique label as it is (#2565)', () => {
    const row = (evidence_id: string, summary: string) =>
      ({ evidence_id, iteration: 26, source_system: 'alerts', origins: ['vigil'], summary, is_gap: false, gap_detail: null })
    const twins = {
      kind: 'hunt',
      hypotheses: [],
      evidence: [
        row('ev-a', 'Email with ISO attachment to j.doe'),
        row('ev-b', 'DNS query for newly registered domain'),
        row('ev-c', 'DNS query for newly registered domain'),
        { ...row('ev-d', 'one lookup'), iteration: 4, source_system: 'dns', origins: [] },
        { ...row('ev-x', 'explorer.exe launched a .lnk file'), iteration: 3, origins: ['crowdstrike'] },
        { ...row('ev-e', 'rundll32.exe spawned by explorer.exe loaded msupdate64.dll'), iteration: 3, origins: ['crowdstrike'] },
        { ...row('ev-f', 'rundll32.exe invoked comsvcs.dll MiniDump'), iteration: 3, origins: ['crowdstrike'] },
      ],
    } as unknown as RunFold
    const labels = caseCitations(twins, []).map((c) => c.label)
    expect(new Set(labels).size).toBe(labels.length)
    expect(labels[3]).toBe('step 4 · dns')
    expect(labels[0]).toMatch(/^step 26 · .+ · Email with ISO…$/)
    expect(labels[2]).toBe(`${labels[1]} (2)`)
    // where two texts start alike, the words where they part
    expect(labels.slice(4)).toEqual(['step 3 · CrowdStrike · explorer.exe…', 'step 3 · CrowdStrike · spawned by…', 'step 3 · CrowdStrike · invoked…'])
    expect(labels.every((label) => label.length <= 48)).toBe(true)
  })

  it('offers only alerts on a run that is not a hunt', () => {
    expect(caseCitations(null, [{ finding_id: 'f1', title: null, description: null, source_link: null, entities: [] }]).map((c) => c.label)).toEqual(['alert · f1'])
  })
})

describe('evidenceInNote (#2459)', () => {
  it('takes a bracketed or parenthesised id list out, and returns each id once, in order', () => {
    expect(evidenceInNote('abandoned: no change record [ev-b680, ev-6e07, ev-b680]')).toEqual({ text: 'abandoned: no change record', ids: ['ev-b680', 'ev-6e07'] })
    expect(evidenceInNote('weakened (ev-1a; ev-2b) by the proxy rows.')).toEqual({ text: 'weakened by the proxy rows.', ids: ['ev-1a', 'ev-2b'] })
  })

  it('takes a bare id out of the prose, and leaves a note without ids as written', () => {
    expect(evidenceInNote('the dump ev-77 came first.')).toEqual({ text: 'the dump came first.', ids: ['ev-77'] })
    expect(evidenceInNote('waiting on logs')).toEqual({ text: 'waiting on logs', ids: [] })
  })
})
