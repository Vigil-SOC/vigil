// What the case page reads off GET /workflows/runs/{id}. Hunt-like runs arrive
// under `hunt`; every other kind arrives under `projection`.

export interface CallRow {
  question: string
  tool: string
  result_length: number
  cost_usd: number
  duration_ms?: number
  iteration?: number
}

/** One lead move, newest first in `moves`. Lead decisions carry no time or iteration. */
export interface MoveRow {
  doing: string
  worker: string
  at: string | null
  iteration: number | null
}

export interface HypothesisRow {
  hypothesis_id: string
  statement: string
  status: string
  supports: number
  weakens: number
  resolution_reason: string | null
  provenance: string
}

export interface EvidenceRow {
  evidence_id: string
  iteration: number
  source_system: string
  summary: string
  is_gap: boolean
  gap_detail: string | null
  /** Stamped by the API on non-gap hunt rows; a display label, as configured now. */
  source_tier: string | null
  bears_on: { hypothesis_id: string; relation: string }[]
}

export interface LeadGapRow {
  dispatch_id: string
  agent_id: string
  failure_reason: string | null
  query_intent?: string
}

/** Where a recalled row came from: the investigation that concluded it. */
export interface RecallProvenance {
  kind: string
  id: string
  concludedAt: string
}

export interface RecalledSighting extends RecallProvenance {
  entity: string
  source: string
  hits: number | null
}

export interface RecalledVerdict extends RecallProvenance {
  outcome: string
  statement: string
}

/** A Declared Gap from a prior investigation, not this run's Visibility Gap. */
export interface RecalledGap extends RecallProvenance {
  disposition: string
  statement: string
}

export interface RecallView {
  keys: string[]
  gaps: RecalledGap[]
  sightings: RecalledSighting[]
  verdicts: RecalledVerdict[]
  unavailable: string | null
}

/** What the run row itself says, beside the fold of its ledger. */
export interface RunMeta {
  status: string
  error: string
  reason: string
  /** The hunt's own status (`parked`, ...); empty for a run that is not a hunt. */
  huntStatus: string
}

export interface HuntFold {
  kind: 'hunt'
  run: RunMeta
  iteration: number
  doing: string
  worker: string
  moves: MoveRow[]
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
  run: RunMeta
  iterations: number
  doing: string
  worker: string
  moves: MoveRow[]
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

/** "handed_off" → "Handed off". */
export function wordDisplay(word: string): string {
  const text = word.replace(/_/g, ' ')
  return text.charAt(0).toUpperCase() + text.slice(1)
}

const ADDED_BY: Record<string, string> = {
  hunt_spec: 'the hunt definition',
  operator: 'you',
  base_rate: 'the base rate',
  deployment_gap: 'the deployment-gap check',
}

/** "Added by" words for a hypothesis provenance; the raw token when unknown, '' when absent. */
export function addedBy(provenance: string): string {
  return ADDED_BY[provenance] ?? provenance
}

export type Stance = 'supports' | 'weakens' | 'neither'

export const STANCE_WORD: Record<Stance, string> = { supports: 'Supports', weakens: 'Goes against', neither: 'Neither' }

export const TIER_WORD: Record<string, string> = { telemetry: 'Telemetry', feed: 'Feed', not_evidence: 'Not counted' }

/** The word for one link's relation; an unknown relation shows as written. */
export function relationWord(relation: string): string {
  return relation === 'supports' || relation === 'weakens' || relation === 'neither' ? STANCE_WORD[relation] : relation
}

const LEAD_ORDER = ['proven', 'standing', 'forming', 'weakened']

/** The explanation the evidence is read against when none is picked: best status, then most net support, then first listed. */
export function leadingExplanation(fold: HuntFold): HypothesisRow | null {
  const rank = (h: HypothesisRow) => {
    const i = LEAD_ORDER.indexOf(explanationWord(h.status, h.supports, h.weakens))
    return i < 0 ? LEAD_ORDER.length : i
  }
  return [...fold.hypotheses].sort((a, b) => rank(a) - rank(b) || b.supports - b.weakens - (a.supports - a.weakens))[0] ?? null
}

/** A row bears on several explanations; it is counted once, by its link to this one (neither when it has none). */
export function stanceOn(row: EvidenceRow, hypothesisId: string | undefined): Stance {
  const relation = row.bears_on.find((link) => link.hypothesis_id === hypothesisId)?.relation
  return relation === 'supports' || relation === 'weakens' ? relation : 'neither'
}

/** Totals over the rows shown, each row once; critic (not_evidence) rows are listed but not counted. */
export function stanceTotals(fold: HuntFold, hypothesisId = leadingExplanation(fold)?.hypothesis_id): Record<Stance, number> {
  const totals = { supports: 0, weakens: 0, neither: 0 }
  for (const row of fold.evidence) if (row.source_tier !== 'not_evidence') totals[stanceOn(row, hypothesisId)]++
  return totals
}

/** Who made the move at this iteration; null when it fell outside the capped list. */
export function workerAt(fold: HuntFold, iteration: number): string | null {
  return fold.moves.find((move) => move.iteration === iteration)?.worker || null
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
      ...(typeof o.iteration === 'number' ? { iteration: o.iteration } : {}),
    }]
  })
}

function provenanceOf(o: Record<string, unknown>): RecallProvenance {
  return { kind: str(o.investigation_kind), id: str(o.investigation_id), concludedAt: str(o.concluded_at) }
}

/** Rows of one recalled kind; a bare string is a statement with no provenance. */
function rowsOf<T>(raw: unknown, read: (o: Record<string, unknown>) => T | null): T[] {
  if (!Array.isArray(raw)) return []
  return raw.flatMap((item) => {
    const o = typeof item === 'string' ? { statement: item } : item
    if (!o || typeof o !== 'object') return []
    const row = read(o as Record<string, unknown>)
    return row ? [row] : []
  })
}

function recallOf(raw: unknown): RecallView | null {
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  const keys = Array.isArray(o.keys) ? o.keys.flatMap((key) => (typeof key === 'string' ? [key] : [])) : []
  const gaps = rowsOf(o.gaps, (r) => (str(r.statement) ? { ...provenanceOf(r), disposition: str(r.disposition), statement: str(r.statement) } : null))
  const sightings = rowsOf(o.sightings, (r) =>
    str(r.entity_key) ? { ...provenanceOf(r), entity: str(r.entity_key), source: str(r.source_system), hits: num(r.hit_count) } : null,
  )
  const verdicts = rowsOf(o.verdicts, (r) => (str(r.statement) ? { ...provenanceOf(r), outcome: str(r.outcome), statement: str(r.statement) } : null))
  const unavailable = typeof o.unavailable === 'string' ? o.unavailable : null
  // A journaled empty result is known-to-be-none. An absent payload is not a recall.
  if (unavailable === null && !Array.isArray(o.keys)) return null
  return { keys, gaps, sightings, verdicts, unavailable }
}

function asHunt(raw: Record<string, unknown>, run: RunMeta): HuntFold {
  const moves = Array.isArray(raw.moves) ? raw.moves : []
  // The server sends moves newest first.
  const rows = moves.flatMap((item) => {
    if (!item || typeof item !== 'object') return []
    const o = item as Record<string, unknown>
    return [{
      doing: str(o.query_intent) || str(o.action),
      worker: str(o.worker_agent_id),
      at: typeof o.created_at === 'string' ? o.created_at : null,
      iteration: num(o.iteration),
    }]
  })
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
          provenance: str(o.provenance),
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
          source_tier: typeof o.source_tier === 'string' ? o.source_tier : null,
          bears_on: bears,
        }]
      })
    : []
  return {
    kind: 'hunt',
    run,
    iteration: num(raw.iteration) ?? 0,
    doing: rows[0]?.doing ?? '',
    worker: rows[0]?.worker ?? '',
    moves: rows,
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

function asLead(raw: Record<string, unknown>, run: RunMeta): LeadFold {
  const decisions = Array.isArray(raw.decisions) ? raw.decisions : []
  // Decisions arrive oldest first; keep moves newest first like a hunt's.
  const rows = decisions.flatMap((item) => {
    if (!item || typeof item !== 'object') return []
    const o = item as Record<string, unknown>
    return [{ doing: str(o.action), worker: str(o.worker), at: null, iteration: null }]
  }).reverse()
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
    run,
    iterations: num(raw.iterations) ?? 0,
    doing: rows[0]?.doing ?? '',
    worker: rows[0]?.worker ?? '',
    moves: rows,
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
  const hunt = row.hunt && typeof row.hunt === 'object' ? (row.hunt as Record<string, unknown>) : null
  const run = { status: str(row.status), error: str(row.error), reason: str(row.reason), huntStatus: str(hunt?.status) }
  if (hunt) return asHunt(hunt, run)
  if (row.projection && typeof row.projection === 'object') return asLead(row.projection as Record<string, unknown>, run)
  return null
}

/** A run that is not going on and did not conclude: parked or paused (it can wake), or failed (it cannot). */
export interface StoppedRun {
  state: 'paused' | 'stopped'
  /** One plain sentence: the first of the joined reasons. */
  line: string
  /** The reason as the server wrote it; '' when none was recorded. */
  raw: string
}

const ENDED = ['failed', 'cancelled', 'canceled']

export function stoppedRun(fold: RunFold | null): StoppedRun | null {
  if (!fold) return null
  const { status, error, reason, huntStatus } = fold.run
  const stopped = ENDED.includes(status)
  if (!stopped && huntStatus !== 'parked' && status !== 'paused') return null
  const raw = stopped ? error || fold.reason : fold.reason || reason
  const first = raw.split(' | ')[0].trim()
  const line = first
    ? `${first.charAt(0).toUpperCase()}${first.slice(1)}${/[.!?]$/.test(first) ? '' : '.'}`
    : stopped ? 'The run ended without concluding and did not say why.' : 'The run is paused and did not say why.'
  return { state: stopped ? 'stopped' : 'paused', line, raw }
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

export interface AgentRow {
  who: string
  doing: string
  tool: string
  at: string | null
}

/** The tool of the call that ran for this move; none when no call carries its iteration. */
export function moveTool(fold: RunFold, move: MoveRow | undefined): string {
  if (!move || move.iteration === null) return ''
  return fold.calls.find((call) => call.iteration === move.iteration)?.tool ?? ''
}

/** One row per distinct worker, latest action first. Moves with no worker are not attributed. */
export function agentRows(fold: RunFold | null): AgentRow[] {
  if (!fold) return []
  const seen = new Set<string>()
  return fold.moves.flatMap((move) => {
    if (!move.worker || seen.has(move.worker)) return []
    seen.add(move.worker)
    return [{ who: move.worker, doing: move.doing, tool: moveTool(fold, move), at: move.at }]
  })
}

export interface StrongRow {
  step: string
  text: string
  stance: 'For' | 'Against' | null
}

/** Up to 3 rows for the closed summary: a hunt's evidence that bears on a hypothesis, or a lead's answers. */
export function strongestRows(fold: RunFold | null): StrongRow[] {
  if (!fold) return []
  if (fold.kind === 'lead') {
    return fold.findings.slice(0, 3).map((row) => ({ step: '—', text: row.answer || '—', stance: null }))
  }
  const rows = fold.evidence.flatMap((row, i) => {
    if (row.is_gap) return []
    const relation = row.bears_on.find((link) => link.relation === 'supports' || link.relation === 'weakens')?.relation
    if (!relation) return []
    const stance: StrongRow['stance'] = relation === 'supports' ? 'For' : 'Against'
    return [{ i, step: String(row.iteration), text: row.summary || row.evidence_id, stance }]
  })
  // Keep one of each stance first so both sides show, then fill in order.
  const picked = new Set([rows.find((r) => r.stance === 'For'), rows.find((r) => r.stance === 'Against')].filter(Boolean))
  for (const r of rows) if (picked.size < 3) picked.add(r)
  return rows.filter((r) => picked.has(r)).map(({ step, text, stance }) => ({ step, text, stance }))
}
