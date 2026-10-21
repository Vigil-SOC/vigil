import { describe, it, expect } from 'vitest'
import { decisionRows, outcomeOf, type V3Move } from './decisionRows'

const move = (n: number, over: Partial<V3Move> = {}): V3Move => ({
  n, move: 'expand', targetId: 'N1', target: '', reason: '', queries: [], created: [], changed: [], prediction: null, theories: null, concluded: false, ...over,
})

describe('outcomeOf', () => {
  it('reads the line from what the turn changed in the graph', () => {
    const m = move(7, {
      created: [{ id: 'N3', kind: 'cause', label: 'x' }, { id: 'E4', kind: 'link', label: '' }],
      changed: [
        { id: 'N2', status: 'confirmed', confidence: 0.9, from: 0.6, fromStatus: 'suspected' },
        { id: 'N5', status: 'suspected', confidence: 0.52, from: 0.5, fromStatus: 'suspected' },
      ],
    })
    expect(outcomeOf(m)).toBe('+1 cause N3 · +1 link · N2 confirmed 0.90')
    expect(outcomeOf(move(8, { prediction: { id: 'P3', status: 'found' } }))).toBe('prediction P3 found')
    expect(outcomeOf(move(9, { prediction: { id: 'P3', status: 'absent' } }))).toBe('prediction P3 not found')
    expect(outcomeOf(move(10, { theories: 2 }))).toBe('2 theories')
    expect(outcomeOf(move(11))).toBe('nothing new')
  })

  it('shows a choose turn\'s reason as its outcome and drops it for every other move', () => {
    expect(outcomeOf(move(3, { move: 'choose', reason: 'branch N1 is not available' }))).toBe('branch N1 is not available')
    expect(outcomeOf(move(4, { reason: 'the model\'s own reasoning' }))).toBe('nothing new')
  })
})

describe('decisionRows', () => {
  it('collapses consecutive identical turns, only when move, target and outcome all match', () => {
    const refused = (n: number, reason = 'refused A') => move(n, { move: 'choose', reason })
    const [round] = decisionRows([move(1), refused(2), refused(3), refused(4), refused(5, 'refused B'), refused(6), move(7, { targetId: 'N2' })])
    expect(round!.rows.map((r) => [r.move, r.turns.map((t) => t.n)])).toEqual([
      ['expand', [1]], ['choose', [2, 3, 4]], ['choose', [5]], ['choose', [6]], ['expand', [7]],
    ])
  })

  it('counts a collapsed row\'s searches and groups rounds at each theorize, in turn order', () => {
    const q = { ref: 'q', purpose: '', query: 'index=x', rows: 0, error: null }
    const rounds = decisionRows([move(4, { move: 'theorize' }), move(1, { queries: [q] }), move(2, { queries: [q, q] }), move(3), move(5), move(6, { move: 'theorize' })])
    expect(rounds.map((r) => r.label)).toEqual(['Opening', 'Round 1', 'Round 2'])
    expect(rounds[0]!.rows).toHaveLength(1)
    expect(rounds[0]!.rows[0]).toMatchObject({ searches: 3, outcome: 'nothing new' })
    expect(rounds[1]!.rows.map((r) => r.move)).toEqual(['theorize', 'expand'])
  })
})
