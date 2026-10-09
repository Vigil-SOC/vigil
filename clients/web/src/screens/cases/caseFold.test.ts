import { describe, expect, it } from 'vitest'
import { actionWords, readFold, runSentence } from './caseFold'

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
