import { describe, expect, it } from 'vitest'
import { actionWords, agentRows, endedRun, foldRecord, huntHypothesis, readFold, recordChip, runSentence, stanceTotals, stoppedRun, strongestRows, visibilityGaps, type RunFold } from './caseFold'
import heldProjection from '../workflows/__fixtures__/kernel-investigate-held-projection.json'

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

describe('recordChip', () => {
  it('types a case activity by who wrote it, not by a run-event kind it shares a name with', () => {
    expect(recordChip('agent_run_report', 'case_activities')).toBe('agent')
    expect(recordChip('finding_added', 'case_activities')).toBe('agent')
    expect(recordChip('case_merged', 'case_activities')).toBe('system')
    expect(recordChip('resolution', 'case_activities')).toBe('human')
    expect(recordChip('recall', 'case_activities')).toBe('human')
    expect(recordChip('recall', 'run')).toBe('memory')
  })
})

describe('an investigation\'s record', () => {
  it('reads it through the hunt\'s parsers, and a lead with none keeps no record', () => {
    const held = readFold({ status: 'completed', projection: heldProjection })
    expect(foldRecord(held)?.hypotheses.map((h) => [h.hypothesis_id, h.supports, h.weakens])).toEqual([['h1', 1, 2], ['h2', 0, 0], ['h3', 2, 0]])
    expect(foldRecord(held)?.openQuestions).toHaveLength(1)
    expect(endedRun(held)?.line).toBe('Asked benign · held as needs review · 1 standing · 1 weakened · 1 forming')

    const legacy = readFold({ status: 'completed', projection: { findings: [{ agent_id: 'lead', answer: 'benign' }] } })
    expect(foldRecord(legacy)).toBeNull()
    expect(endedRun(legacy)?.line).toBe('1 finding')
  })
})

describe('a root-cause run', () => {
  const head = {
    run_id: 'run-rca',
    run_kind: 'root_cause',
    status: 'terminal',
    outcome: 'concluded',
    reason: 'origin proven',
    cost_usd: 0.4,
    max_cost_usd: 2,
    steps: [{ step_id: 's1', proven: true }],
    proven: 1,
    notices: ['a notice'],
    searches: 3,
    recent_searches: [{ tool: 'search', args: '{}', rows: 2, failed: false }],
    open_checkpoint: null,
  }

  it('folds to its head only, and every reader shows nothing for it', () => {
    const fold = readFold({ status: 'completed', projection: head })
    expect(fold).toMatchObject({ kind: 'head', outcome: 'concluded', reason: 'origin proven', costUsd: 0.4, maxCostUsd: 2, openCheckpoint: null, calls: [], moves: [] })
    expect(foldRecord(fold)).toBeNull()
    expect(visibilityGaps(fold)).toEqual([])
    expect(agentRows(fold)).toEqual([])
    expect(strongestRows(fold)).toEqual([])
    expect(stoppedRun(fold)).toBeNull()
    expect(endedRun(fold)?.line).toBe('0 findings')
  })

  it('still folds without run_kind, through the projection key as before', () => {
    const { run_kind: _kind, ...bare } = head // eslint-disable-line @typescript-eslint/no-unused-vars
    const fold = readFold({ status: 'completed', projection: bare })
    expect(fold?.kind).toBe('lead')
    expect(foldRecord(fold)).toBeNull()
    expect(visibilityGaps(fold)).toEqual([])
  })

  it('leaves hunt and lead folds as they were, and reads the hunt ceiling off the head or its budgets', () => {
    expect(readFold({ status: 'running', hunt: { iteration: 1, budgets: { max_cost_usd: 5 } } })).toMatchObject({ kind: 'hunt', maxCostUsd: 5 })
    expect(readFold({ status: 'running', hunt: { run_kind: 'hunt', iteration: 1, max_cost_usd: 3, budgets: { max_cost_usd: 5 } } })).toMatchObject({ kind: 'hunt', maxCostUsd: 3 })
    expect(readFold({ status: 'running', projection: { max_cost_usd: 1, findings: [] } })).toMatchObject({ kind: 'lead', maxCostUsd: 1 })
  })
})

describe('the budget ceiling', () => {
  it('is read off the head, and off budgets on a ledger written before the head carried it', () => {
    const ceiling = (fold: RunFold | null) => fold?.maxCostUsd
    expect(ceiling(readFold({ hunt: { max_cost_usd: 5, budgets: { max_cost_usd: 2 } } }))).toBe(5)
    expect(ceiling(readFold({ hunt: { budgets: { max_cost_usd: 2 } } }))).toBe(2)
    expect(ceiling(readFold({ projection: heldProjection }))).toBe(3)
    expect(ceiling(readFold({ projection: { budgets: { max_cost_usd: 4 } } }))).toBe(4)
    expect(ceiling(readFold({ projection: { max_cost_usd: 0 } }))).toBeNull()
  })
})
