// What the case page reads off GET /workflows/runs/{id}. Hunt-like runs arrive
// under `hunt`; every other kind arrives under `projection`.

export interface CallRow {
  question: string
  tool: string
  result_length: number
  cost_usd: number
  duration_ms?: number
}

export interface HypothesisRow {
  hypothesis_id: string
  statement: string
  status: string
  supports: number
  weakens: number
  resolution_reason: string | null
}

export interface EvidenceRow {
  evidence_id: string
  iteration: number
  source_system: string
  summary: string
  is_gap: boolean
  gap_detail: string | null
  bears_on: { hypothesis_id: string; relation: string }[]
}

export interface LeadGapRow {
  dispatch_id: string
  agent_id: string
  failure_reason: string | null
  query_intent?: string
}

export interface RecallView {
  keys: string[]
  gaps: string[]
  sightings: string[]
  verdicts: string[]
  unavailable: string | null
}

export interface HuntFold {
  kind: 'hunt'
  iteration: number
  doing: string
  worker: string
  hypotheses: HypothesisRow[]
  evidence: EvidenceRow[]
  evidenceCount: number
  calls: CallRow[]
  recall: RecallView | null
  outcome: string | null
  reason: string
  costUsd: number | null
}

export interface LeadFold {
  kind: 'lead'
  iterations: number
  doing: string
  worker: string
  findings: { agent_id: string; answer: string }[]
  calls: CallRow[]
  gaps: LeadGapRow[]
  recall: RecallView | null
  outcome: string | null
  reason: string
  costUsd: number | null
}

export type RunFold = HuntFold | LeadFold

export type RecordChip = 'memory' | 'human' | 'system' | 'agent'

const HONEST = 'This workflow does not test explanations yet.'

export function honestLine(): string {
  return HONEST
}

export function explanationWord(status: string, supports: number, weakens: number): string {
  if (status === 'proven') return 'proven'
  if (status === 'disproven') return 'ruled out'
  if (status === 'active') {
    if (weakens > 0) return 'weakened'
    if (supports > 0) return 'standing'
    return 'forming'
  }
  if (status === 'inconclusive' || status === 'parked' || status === 'handed_off') return status
  return status
}

export function recordChip(kind: string): RecordChip {
  switch (kind) {
    case 'recall':
      return 'memory'
    case 'resolution':
    case 'directive':
    case 'case_audit_logs':
      return 'human'
    case 'run':
    case 'spend':
    case 'resumed':
      return 'system'
    default:
      return 'agent'
  }
}

function num(value: unknown): number | null {
  return typeof value === 'number' ? value : null
}

function str(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function answerText(answer: unknown): string {
  if (typeof answer === 'string') return answer
  if (answer == null) return ''
  try {
    return JSON.stringify(answer)
  } catch {
    return ''
  }
}

function callsOf(raw: unknown): CallRow[] {
  if (!Array.isArray(raw)) return []
  return raw.flatMap((item) => {
    if (!item || typeof item !== 'object') return []
    const o = item as Record<string, unknown>
    return [{
      question: str(o.question),
      tool: str(o.tool),
      result_length: num(o.result_length) ?? 0,
      cost_usd: num(o.cost_usd) ?? 0,
      ...(typeof o.duration_ms === 'number' ? { duration_ms: o.duration_ms } : {}),
    }]
  })
}

function lineOf(value: unknown): string {
  if (typeof value === 'string') return value
  if (!value || typeof value !== 'object') return ''
  const o = value as Record<string, unknown>
  if (typeof o.statement === 'string') {
    return [typeof o.outcome === 'string' ? o.outcome : o.disposition, o.statement].filter(Boolean).join(' — ')
  }
  if (typeof o.entity_key === 'string') {
    const hits = typeof o.hit_count === 'number' ? ` · ${o.hit_count}` : ''
    return `${o.entity_key}${typeof o.source_system === 'string' ? ` · ${o.source_system}` : ''}${hits}`
  }
  return JSON.stringify(value)
}

function recallOf(raw: unknown): RecallView | null {
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  const keys = Array.isArray(o.keys) ? o.keys.flatMap((key) => (typeof key === 'string' ? [key] : [])) : []
  const gaps = Array.isArray(o.gaps) ? o.gaps.map(lineOf).filter(Boolean) : []
  const sightings = Array.isArray(o.sightings) ? o.sightings.map(lineOf).filter(Boolean) : []
  const verdicts = Array.isArray(o.verdicts) ? o.verdicts.map(lineOf).filter(Boolean) : []
  const unavailable = typeof o.unavailable === 'string' ? o.unavailable : null
  // A journaled empty result is known-to-be-none. An absent payload is not a recall.
  if (unavailable === null && !Array.isArray(o.keys)) return null
  return { keys, gaps, sightings, verdicts, unavailable }
}

function asHunt(raw: Record<string, unknown>): HuntFold {
  const moves = Array.isArray(raw.moves) ? raw.moves : []
  const latest = moves[0] && typeof moves[0] === 'object' ? (moves[0] as Record<string, unknown>) : null
  const intent = latest ? str(latest.query_intent) : ''
  const action = latest ? str(latest.action) : ''
  const hypotheses = Array.isArray(raw.hypotheses)
    ? raw.hypotheses.flatMap((item) => {
        if (!item || typeof item !== 'object') return []
        const o = item as Record<string, unknown>
        return [{
          hypothesis_id: str(o.hypothesis_id),
          statement: str(o.statement),
          status: str(o.status),
          supports: num(o.supports) ?? 0,
          weakens: num(o.weakens) ?? 0,
          resolution_reason: typeof o.resolution_reason === 'string' ? o.resolution_reason : null,
        }]
      })
    : []
  const evidence = Array.isArray(raw.evidence)
    ? raw.evidence.flatMap((item) => {
        if (!item || typeof item !== 'object') return []
        const o = item as Record<string, unknown>
        const bears = Array.isArray(o.bears_on)
          ? o.bears_on.flatMap((link) => {
              if (!link || typeof link !== 'object') return []
              const b = link as Record<string, unknown>
              return [{ hypothesis_id: str(b.hypothesis_id), relation: str(b.relation) }]
            })
          : []
        return [{
          evidence_id: str(o.evidence_id),
          iteration: num(o.iteration) ?? 0,
          source_system: str(o.source_system),
          summary: str(o.summary),
          is_gap: o.is_gap === true,
          gap_detail: typeof o.gap_detail === 'string' ? o.gap_detail : null,
          bears_on: bears,
        }]
      })
    : []
  return {
    kind: 'hunt',
    iteration: num(raw.iteration) ?? 0,
    doing: intent || action,
    worker: latest ? str(latest.worker_agent_id) : '',
    hypotheses,
    evidence,
    evidenceCount: num(raw.evidence_count) ?? evidence.length,
    calls: callsOf(raw.calls),
    recall: recallOf(raw.recall),
    outcome: typeof raw.outcome === 'string' ? raw.outcome : null,
    reason: str(raw.reason),
    costUsd: num(raw.cost_usd),
  }
}

function asLead(raw: Record<string, unknown>): LeadFold {
  const decisions = Array.isArray(raw.decisions) ? raw.decisions : []
  const latest = decisions.length > 0 && decisions[decisions.length - 1] && typeof decisions[decisions.length - 1] === 'object'
    ? (decisions[decisions.length - 1] as Record<string, unknown>)
    : null
  const findings = Array.isArray(raw.findings)
    ? raw.findings.flatMap((item) => {
        if (!item || typeof item !== 'object') return []
        const o = item as Record<string, unknown>
        return [{ agent_id: str(o.agent_id), answer: answerText(o.answer) }]
      })
    : []
  const gaps = Array.isArray(raw.gaps)
    ? raw.gaps.flatMap((item) => {
        if (!item || typeof item !== 'object') return []
        const o = item as Record<string, unknown>
        return [{
          dispatch_id: str(o.dispatch_id),
          agent_id: str(o.agent_id),
          failure_reason: typeof o.failure_reason === 'string' ? o.failure_reason : null,
          ...(typeof o.query_intent === 'string' ? { query_intent: o.query_intent } : {}),
        }]
      })
    : []
  return {
    kind: 'lead',
    iterations: num(raw.iterations) ?? 0,
    doing: latest ? str(latest.action) : '',
    worker: latest ? str(latest.worker) : '',
    findings,
    calls: callsOf(raw.calls),
    gaps,
    recall: recallOf(raw.recall),
    outcome: typeof raw.outcome === 'string' ? raw.outcome : null,
    reason: str(raw.reason),
    costUsd: num(raw.cost_usd),
  }
}

export function readFold(body: unknown): RunFold | null {
  if (!body || typeof body !== 'object') return null
  const row = body as Record<string, unknown>
  if (row.hunt && typeof row.hunt === 'object') return asHunt(row.hunt as Record<string, unknown>)
  if (row.projection && typeof row.projection === 'object') return asLead(row.projection as Record<string, unknown>)
  return null
}

export function recallEntityCalls(fold: RunFold | null): CallRow[] {
  if (!fold) return []
  return fold.calls.filter((call) => call.tool === 'recall_entity')
}

export function visibilityGaps(fold: RunFold | null): { id: string; text: string }[] {
  if (!fold) return []
  if (fold.kind === 'hunt') {
    return fold.evidence
      .filter((row) => row.is_gap)
      .map((row) => ({ id: row.evidence_id, text: row.gap_detail || row.summary || 'Could not look' }))
  }
  return fold.gaps.map((gap) => ({
    id: gap.dispatch_id,
    text: [gap.query_intent || gap.agent_id, gap.failure_reason].filter(Boolean).join(' — ') || 'Dispatch failed',
  }))
}
