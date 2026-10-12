import { describe, expect, it } from 'vitest'
import { actionWords, huntHypothesis, readFold, runSentence, stanceTotals, type RunFold } from './caseFold'

describe('actionWords', () => {
  it('has plain words for every hunt action', () => {
    for (const action of ['INVESTIGATE', 'EXPAND', 'PIVOT', 'DEEPEN', 'ABANDON', 'VALIDATE', 'CHECKPOINT', 'CONCLUDE', 'HANDOFF_IR', 'STALLED']) {
      expect(actionWords(action)).not.toMatch(/^[A-Z_]+$/)
    }
    expect(actionWords('HANDOFF_IR')).toBe('Start incident response on a proven explanation')
  })

  it('reads an unknown token as a sentence, never the raw enum', () => {
    expect(actionWords('SOME_NEW_MOVE')).toBe('Some new move')
    expect(actionWords('')).toBe('')
  })
})

describe('runSentence', () => {
  it('says where a hunt stands, never the lead’s directive', () => {
    const fold = readFold({
      status: 'running',
      hunt: { iteration: 6, moves: [{ query_intent: 'Re-read the raw CloudTrail', iteration: 6 }], hypotheses: [{ hypothesis_id: 'h1' }, { hypothesis_id: 'h2' }], evidence_count: 1 },
    })
    expect(runSentence(fold)).toBe('Step 6 · 2 explanations · 1 evidence row so far')
  })

  it('says nothing before a run has been read', () => {
    expect(runSentence(null)).toBe('')
  })
})

describe('stanceTotals', () => {
  const hunt = (hypotheses: object[], evidenceCount = 157): Extract<RunFold, { kind: 'hunt' }> => {
    const fold = readFold({ status: 'running', hunt: { iteration: 1, hypotheses, evidence: [], evidence_count: evidenceCount } })
    if (fold?.kind !== 'hunt') throw new Error('not a hunt')
    return fold
  }
  const base = { hypothesis_id: 'hb', statement: 'legitimate', status: 'active', supports: 0, weakens: 24, neither: 133, provenance: 'base_rate' }

  it('reads the hunt’s own explanation over every row, whatever the base rate ranks', () => {
    const fold = hunt([base, { hypothesis_id: 'h1', statement: 'owned', status: 'inconclusive', supports: 95, weakens: 3, neither: 59, provenance: 'hunt_spec' }])
    expect(huntHypothesis(fold)?.hypothesis_id).toBe('h1')
    expect(stanceTotals(fold)).toEqual({ supports: 95, weakens: 3, neither: 59 })
  })

  it('prefers the caller’s hypothesis, and never defaults to the base rate', () => {
    const own = { hypothesis_id: 'h2', statement: 'mine', status: 'active', supports: 1, weakens: 0, neither: 2, provenance: 'operator' }
    expect(huntHypothesis(hunt([base, { ...own, hypothesis_id: 'h1', provenance: 'hunt_spec' }, own]))?.hypothesis_id).toBe('h2')
    expect(huntHypothesis(hunt([base]))).toBeNull()
    expect(stanceTotals(hunt([base]))).toBeNull()
  })

  it('derives neither from the row count when the server does not send it', () => {
    const fold = hunt([{ hypothesis_id: 'h1', statement: 'owned', status: 'active', supports: 95, weakens: 3, provenance: 'hunt_spec' }])
    expect(stanceTotals(fold)).toEqual({ supports: 95, weakens: 3, neither: 59 })
  })
})
