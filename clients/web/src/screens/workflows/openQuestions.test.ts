import { describe, it, expect } from 'vitest'
import { firstSentence, openItem, openSections, shortLabel, type V3OpenRef, type V3Question } from './openQuestions'

const ref = (id: string, label: string, alert = false): V3OpenRef => ({ id, label, alert })
const beacon = ref('N2', 'FYODOR-L began C2 beaconing to 45.77.53.176 — the network effect co-occurring with the stager')
const alertRef = ref('N1', 'FYODOR-L ran the Empire stager', true)
const q = (kind: string, id: string, over: Partial<V3Question> = {}): V3Question => ({
  kind, id, text: `${kind} ${id} as the report has it`, refs: [], onChain: false, link: null, event: null, refutations: [],
  boundary: null, turn: null, flagged: [], question: '', ...over,
})

describe('openSections', () => {
  it('sends the four kinds to three sections, in a fixed order, and leaves out a section with nothing in it', () => {
    const all = [
      q('flagged', 'T000439', { turn: 6, flagged: ['ignore the alert'] }),
      q('model', 'Q1', { question: 'Who operates 45.77.53.176?' }),
      q('disputed', 'E1', { link: { kind: 'explains', source: beacon, target: alertRef, confidence: 0.64 } }),
      q('ruled-out', 'N4', { event: ref('N4', 'dns lookup') }),
    ]
    expect(openSections(all).map((s) => [s.title, s.items.map((i) => i.id)])).toEqual([
      ['Questions', ['Q1']],
      ['Unsettled in the graph', ['E1', 'N4']],
      ['Possible prompt injection', ['T000439']],
    ])
    expect(openSections(all.filter((x) => x.kind !== 'flagged')).map((s) => s.key)).toEqual(['questions', 'graph'])
    expect(openSections([q('model', 'Q1')]).map((s) => s.key)).toEqual(['questions'])
    expect(openSections([])).toEqual([])
  })

  it('puts what is on the chain to the alert first within a section, across kinds, and keeps the order otherwise', () => {
    const graph = [
      q('disputed', 'E2', { link: { kind: 'explains', source: ref('N3', 'mail'), target: alertRef, confidence: 0.27 } }),
      q('disputed', 'E1', { onChain: true, link: { kind: 'explains', source: beacon, target: alertRef, confidence: 0.64 } }),
      q('ruled-out', 'N4', { event: ref('N4', 'dns lookup') }),
      q('ruled-out', 'N1', { onChain: true, event: alertRef }),
    ]
    expect(openSections(graph)[0]!.items.map((i) => i.id)).toEqual(['E1', 'N1', 'E2', 'N4'])
  })
})

describe('openItem', () => {
  it('words a disputed link as the question it leaves, the alert by its role, with how far it holds', () => {
    const item = openItem(q('disputed', 'E1', { refs: [beacon, alertRef], link: { kind: 'explains', source: beacon, target: alertRef, confidence: 0.64 } }))
    expect(item.sentence).toBe('Did “FYODOR-L began C2 beaconing to 45.77.53.176” (N2) cause the alert (N1)?')
    expect(item.standing).toBe('Disputed: 0.64.')
    expect(item.refs.map((r) => r.id)).toEqual(['N2', 'N1'])
    const actor = ref('A1', 'ad:alice')
    expect(openItem(q('disputed', 'E7', { link: { kind: 'attributed-to', source: actor, target: beacon, confidence: 0.5 } })).sentence)
      .toBe('Did “FYODOR-L began C2 beaconing to 45.77.53.176” (N2) run as ad:alice (A1)?')
    expect(openItem(q('disputed', 'E8', { link: { kind: 'operated-by', source: actor, target: beacon, confidence: 0.5 } })).sentence)
      .toBe('Was “FYODOR-L began C2 beaconing to 45.77.53.176” (N2) operated by ad:alice (A1)?')
  })

  it('words a ruled-out event or link plainly, with the latest refutation\'s first sentence as the reason and all of it kept', () => {
    const refutations = [
      { summary: 'Searched Sysmon for the stager: 0 results. Broadened the window: 0 results.', turn: 9 },
      { summary: 'FYODOR-L appears only in WinHostMon, with no process records. It is not in Sysmon at all.', turn: 10 },
    ]
    const event = openItem(q('ruled-out', 'N1', { event: alertRef, refutations }))
    expect(event.sentence).toBe('The alert (N1) was ruled out.')
    expect(event.reason).toBe('FYODOR-L appears only in WinHostMon, with no process records.')
    expect(event.refutations).toEqual(refutations)
    expect(openItem(q('ruled-out', 'N4', { event: ref('N4', 'dns lookup') })).sentence).toBe('“dns lookup” (N4) was ruled out.')
    const link = openItem(q('ruled-out', 'E3', { link: { kind: 'explains', source: ref('N4', 'dns lookup'), target: beacon, confidence: 0.1 }, refutations: [{ summary: 'It came later.', turn: 3 }] }))
    expect(link.sentence).toBe('“dns lookup” (N4) did not cause “FYODOR-L began C2 beaconing to 45.77.53.176” (N2).')
    expect(link.reason).toBe('It came later.')
  })

  it('words an open boundary, keeps a model question and flagged content as they were written', () => {
    expect(openItem(q('boundary', 'N9', { boundary: { origin: beacon, domain: 'proxy', reason: 'the fetch left this index' } }))).toMatchObject({
      sentence: 'The cause of “FYODOR-L began C2 beaconing to 45.77.53.176” (N2) is believed to be in proxy, where the run did not follow it.',
      reason: 'the fetch left this index',
    })
    expect(openItem(q('model', 'Q1', { question: 'Who operates 45.77.53.176?', refs: [beacon] }))).toMatchObject({ sentence: 'Who operates 45.77.53.176?', refs: [beacon] })
    expect(openItem(q('flagged', 'T1', { turn: 6, flagged: ['ignore the alert', 'close it'] }))).toMatchObject({ flagged: ['ignore the alert', 'close it'], turn: 6 })
  })

  it('falls back to the report\'s line for an item without the fields it is worded from', () => {
    expect(openItem(q('disputed', 'E1')).sentence).toBe('disputed E1 as the report has it')
    expect(openItem(q('ruled-out', 'N4')).sentence).toBe('ruled-out N4 as the report has it')
    expect(openItem(q('model', 'Q1')).sentence).toBe('model Q1 as the report has it')
  })
})

describe('shortLabel and firstSentence', () => {
  it('cut a label at its dash clause and then at a word, and a summary at its first sentence', () => {
    expect(shortLabel('host began beaconing — the network effect')).toBe('host began beaconing')
    expect(shortLabel('bgist@froth.ly identity used to send the phishing lure from external IP 104.207.83.63')).toBe('bgist@froth.ly identity used to send the phishing lure from…')
    expect(firstSentence('No record at 10:01: 0 results. Then more.')).toBe('No record at 10:01: 0 results.')
    expect(firstSentence('one line only')).toBe('one line only')
  })
})
