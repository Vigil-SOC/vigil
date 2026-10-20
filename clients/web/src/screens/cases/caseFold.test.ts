import { describe, expect, it } from 'vitest'
import { actionWords, agentRows, caseVerdict, closureFlag, endedRun, foldRecord, huntHypothesis, readFold, recordChip, runSentence, stanceTotals, stoppedRun, strongestRows, visibilityGaps, type RunFold } from './caseFold'
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
    disposition: null,
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

describe('the disposition', () => {
  const disposition = (fold: RunFold | null) => fold?.disposition
  it('is read off the head of every fold, and null when the head says none', () => {
    expect(disposition(readFold({ hunt: { run_kind: 'hunt', disposition: 'malicious' } }))).toBe('malicious')
    expect(disposition(readFold({ projection: { run_kind: 'investigate', disposition: 'needs_review', findings: [] } }))).toBe('needs_review')
    expect(disposition(readFold({ projection: { run_kind: 'root_cause', disposition: null } }))).toBeNull()
    expect(disposition(readFold({ hunt: { run_kind: 'hunt' } }))).toBeNull()
    expect(disposition(readFold({ projection: { findings: [] } }))).toBeNull()
  })

  it('is not read out of the finding the lead shows', () => {
    const fold = readFold({ projection: { run_kind: 'investigate', findings: [{ agent_id: 'agent', answer: { disposition: 'benign', summary: 'quiet' } }] } })
    expect(fold).toMatchObject({ kind: 'lead', disposition: null })
  })
})

describe('caseVerdict', () => {
  const run = (label: string, disposition: string | null, over: { phase?: 'loading' | 'ready' | 'error'; steps?: string[]; kind?: 'hunt' | 'investigate' } = {}) => ({
    label,
    phase: over.phase ?? 'ready',
    fold: over.phase === 'error' || over.phase === 'loading' ? null : readFold(
      over.kind === 'hunt'
        ? { hunt: { run_kind: 'hunt', disposition } }
        : { projection: { run_kind: 'investigate', disposition, findings: over.steps ? [{ agent_id: 'agent', answer: { summary: 's', next_steps: over.steps } }] : [] } },
    ),
  })
  // Newest first.
  it('is null with no disposition, or while any run is loading', () => {
    expect(caseVerdict([run('a', null), run('b', null)])).toBeNull()
    expect(caseVerdict([])).toBeNull()
    expect(caseVerdict([run('a', 'malicious'), run('b', null, { phase: 'loading' })])).toBeNull()
  })

  it('lets the newest run speak, and malicious then suspicious outrank a later word that is not a dismissal', () => {
    expect(caseVerdict([run('a', 'malicious')])).toEqual({ word: 'malicious', basis: '', doNow: [] })
    expect(caseVerdict([run('new', 'inconclusive'), run('old', 'malicious')])?.word).toBe('malicious')
    expect(caseVerdict([run('new', 'needs_review'), run('old', 'suspicious')])?.word).toBe('suspicious')
    expect(caseVerdict([run('new', 'suspicious'), run('old', 'malicious')])?.word).toBe('malicious')
    expect(caseVerdict([run('new', 'inconclusive'), run('old', 'benign')])?.word).toBe('inconclusive')
    expect(caseVerdict([run('new', null), run('old', 'benign')])?.word).toBe('benign')
  })

  it('holds a dismissal for review when another run objects, naming it with the word it said', () => {
    expect(caseVerdict([run('rerun', 'benign'), run('hunt', 'suspicious')])).toMatchObject({ word: 'needs_review', basis: 'hunt said Suspicious.' })
    expect(caseVerdict([run('rerun', 'false_positive'), run('hunt', 'malicious'), run('x', 'needs_review')])?.basis).toBe('hunt said Malicious.')
    expect(caseVerdict([run('rerun', 'benign'), run('hunt', 'needs_review')])?.basis).toBe('hunt said Needs review.')
  })

  it('does not let a dismissal stand over a run that could not be read, and names it', () => {
    expect(caseVerdict([run('a', 'benign'), run('b', null, { phase: 'error' })])).toEqual({ word: 'needs_review', basis: 'b could not be read.', doNow: [] })
    expect(caseVerdict([run('a', 'malicious'), run('b', null, { phase: 'error' })])?.word).toBe('malicious')
    expect(caseVerdict([run('a', null), run('b', null, { phase: 'error' })])).toBeNull()
  })

  it('lets two dismissals stand', () => {
    expect(caseVerdict([run('a', 'benign'), run('b', 'false_positive')])).toEqual({ word: 'benign', basis: '', doNow: [] })
  })

  it('carries an investigate run\'s next steps, and none for a hunt', () => {
    expect(caseVerdict([run('a', 'suspicious', { steps: ['Isolate the host', 'Reset the token'] })])?.doNow).toEqual(['Isolate the host', 'Reset the token'])
    expect(caseVerdict([run('a', 'suspicious', { kind: 'hunt' })])?.doNow).toEqual([])
    // the objector's steps, not the dismissal's
    expect(caseVerdict([run('new', 'benign', { steps: ['Close it'] }), run('old', 'malicious', { steps: ['Isolate'] })])?.doNow).toEqual(['Isolate'])
  })
})

describe('closureFlag', () => {
  const words = ['malicious', 'suspicious', 'needs_review', 'inconclusive', 'benign', 'false_positive', null]
  it.each([
    ['false_positive', true],
    ['unspecified', true],
    ['resolved', false],
    ['duplicate', false],
    ['unable_to_resolve', false],
  ])('%s against each word', (category, flags) => {
    for (const word of words) {
      expect(closureFlag({ closure_category: category }, word)).toBe(flags && ['malicious', 'suspicious', 'needs_review'].includes(word ?? ''))
    }
  })
  it('is false with no closure', () => expect(closureFlag(null, 'malicious')).toBe(false))
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
