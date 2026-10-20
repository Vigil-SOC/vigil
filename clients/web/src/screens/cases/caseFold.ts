// What the case page reads off GET /workflows/runs/{id}. Hunt-like runs arrive
// under `hunt`; every other kind arrives under `projection`.
import { sourceBadge } from '../../config/sourceBadges'
import type { Phase } from './useCases'

export interface CallRow {
  question: string
  tool: string
  result_length: number
  /** Null when nothing priced it: shown as not recorded, never as $0. */
  cost_usd: number | null
  duration_ms?: number
  iteration?: number
  /** The call's ref, opened whole at /workflows/runs/{id}/calls/{ref}. */
  ref?: string
  /** The query text it sent, when the run carries it. */
  query?: string
  /** Rows the result said it held. */
  rows?: number
  /** served, visibility_gap or defect. */
  class?: string
  /** The turn's words before its calls, shared by every call of that turn. */
  thought?: string
  /** The kind a failed call reported: timeout, unavailable, refused... */
  failed?: string
}

/** One lead move, newest first in `moves`. Lead decisions carry no time or iteration. */
interface MoveRow {
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
  /** Over every evidence row, like supports and weakens; null from a server that does not count it. */
  neither: number | null
  resolution_reason: string | null
  provenance: string
}

/**
 * What a row that read no system is, named for what it is rather than by the hunt's role words
 * (#2458): the critic's rebuttal, the deployment's own gap, an operator's declared gap, a query
 * that could not run.
 */
const NOT_A_SYSTEM: Record<string, string> = {
  critic: 'Reviewer',
  deployment: 'Not connected',
  operator: 'You',
  dispatcher: 'Not answered',
}

/** The system an evidence row's rows came from, as the board names it; the hunt's domain word only when none is known. One reading for the trail and the citation chips. */
export function evidenceSource(row: Pick<EvidenceRow, 'origins' | 'source_system'> & { is_gap?: boolean }): string {
  if (row.origins.length > 0) return row.origins.map((one) => sourceBadge(one).label).join(', ')
  const named = NOT_A_SYSTEM[row.source_system]
  if (named) return named
  // a query the hunt recorded as failed read nothing, whatever domain it was meant for
  return row.is_gap ? NOT_A_SYSTEM.dispatcher : row.source_system
}

interface EvidenceRow {
  evidence_id: string
  iteration: number
  source_system: string
  summary: string
  is_gap: boolean
  gap_detail: string | null
  /** Stamped by the API on non-gap hunt rows; a display label, as configured now. */
  source_tier: string | null
  /** The systems its rows came from (a cited alert's data_source, or the integration that answered); empty when none is known. */
  origins: string[]
  bears_on: { hypothesis_id: string; relation: string }[]
  /** worker, tool_failure, deployment_gap, operator_gap, null_check... */
  provenance: string
}

interface LeadGapRow {
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

interface RecallView {
  keys: string[]
  gaps: RecalledGap[]
  sightings: RecalledSighting[]
  verdicts: RecalledVerdict[]
  unavailable: string | null
}

/** What the run row itself says, beside the fold of its ledger. */
interface RunMeta {
  status: string
  error: string
  reason: string
  /** The hunt's own status (`parked`, ...); empty for a run that is not a hunt. */
  huntStatus: string
}

/** The question a run is stopped on, as the run itself holds it. */
export interface OpenCheckpointRow {
  checkpoint_id: string
  checkpoint_class: string
  question: string
  raised_at: string
  /** A hypothesis approval's explanations, with where each came from where the run recorded it. */
  hypotheses: { hypothesis_id: string; statement: string; provenance: string }[]
}

export interface OpenQuestionRow {
  question_id: string
  question: string
}

interface HuntFold {
  kind: 'hunt'
  run: RunMeta
  iteration: number
  openCheckpoint: OpenCheckpointRow | null
  /** Leads the hunt opened and has not closed. */
  openQuestions: OpenQuestionRow[]
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
  /** What this run was granted, extensions included; null when the run row carries no budgets. */
  maxCostUsd: number | null
  /** The word the run concluded on, as held by the reviewer guard; null when it said none. */
  disposition: string | null
}

/** What a run's record holds: the explanations it weighed, the rows linked to them, the questions left open. */
export interface RunRecord {
  hypotheses: HypothesisRow[]
  evidence: EvidenceRow[]
  evidenceCount: number
  openQuestions: OpenQuestionRow[]
}

/** A dismissal the reviewer guard held for a person: what the run asked for, what it ends on, and why. */
export interface HeldRow {
  field: string
  asked: string
  final: string
  reason: string
}

interface LeadFold {
  kind: 'lead'
  /** A kernel investigation's record; null for a run that keeps none. */
  record: RunRecord | null
  held: HeldRow | null
  run: RunMeta
  iterations: number
  doing: string
  worker: string
  moves: MoveRow[]
  findings: { agent_id: string; answer: string }[]
  /** The steps the conclusion lists for a reader to take, most important first; empty when it lists none. */
  nextSteps: string[]
  calls: CallRow[]
  gaps: LeadGapRow[]
  recall: RecallView | null
  outcome: string | null
  reason: string
  costUsd: number | null
  /** What this run was granted; null when the projection carries no positive cap. */
  maxCostUsd: number | null
  /** The word the run concluded on, as held by the reviewer guard; null when it said none. */
  disposition: string | null
}

/** A run whose chain stays on Watch a run (root cause): its head only. The row fields stay empty so every reader shows nothing for it. */
interface HeadFold {
  kind: 'head'
  run: RunMeta
  openCheckpoint: OpenCheckpointRow | null
  outcome: string | null
  reason: string
  costUsd: number | null
  maxCostUsd: number | null
  /** The word the run concluded on, as held by the reviewer guard; null when it said none. */
  disposition: string | null
  doing: ''
  worker: ''
  moves: MoveRow[]
  calls: CallRow[]
  recall: null
}

export type RunFold = HuntFold | LeadFold | HeadFold

export type RecordChip = 'memory' | 'human' | 'system' | 'agent'

const HONEST = 'This workflow does not test explanations yet.'

export function honestLine(): string {
  return HONEST
}

// The hypothesis statuses explanationWord maps: HYPOTHESIS_STATUSES in services/agent
// contracts/events.ts, held to it by tests/unit/_ratchets/test_recall_contract_agrees.py.
export const EXPLAINED_STATUSES = ['active', 'proven', 'disproven', 'inconclusive', 'parked', 'handed_off'] as const

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

// The kernel hunt's moves (services/agent plugins/hunt/words.ts) come after the controller's.
const HUNT_ACTIONS = ['INVESTIGATE', 'EXPAND', 'PIVOT', 'DEEPEN', 'ABANDON', 'VALIDATE', 'CHECKPOINT', 'CONCLUDE', 'HANDOFF_IR', 'STALLED', 'RECORD', 'LINK', 'PROPOSE', 'QUERY', 'QUESTION', 'READ'] as const
type HuntAction = (typeof HUNT_ACTIONS)[number]

function isHuntAction(token: string): token is HuntAction {
  return (HUNT_ACTIONS as readonly string[]).includes(token)
}

/** A lead's decision action in plain words; an unknown token reads as a sentence, never the raw enum. */
export function actionWords(token: string): string {
  if (!isHuntAction(token)) return wordDisplay(token.toLowerCase())
  switch (token) {
    case 'INVESTIGATE':
      return 'Look into a new lead'
    case 'EXPAND':
      return 'Widen the search'
    case 'PIVOT':
      return 'Switch to a different angle'
    case 'DEEPEN':
      return 'Dig deeper into the same lead'
    case 'ABANDON':
      return 'Rule out an explanation'
    case 'VALIDATE':
      return 'Check an explanation against the evidence'
    case 'CHECKPOINT':
      return 'Stop to ask you'
    case 'CONCLUDE':
      return 'Wrap up and report'
    // The label core/workflows/hunt_preflight.py gives HANDOFF_IR.
    case 'HANDOFF_IR':
      return 'Start incident response on a proven explanation'
    case 'STALLED':
      return 'Stalled, no decision made'
    case 'RECORD':
      return 'Record evidence'
    case 'LINK':
      return 'Weigh evidence against the explanations'
    case 'PROPOSE':
      return 'Propose an explanation'
    case 'QUERY':
      return 'Query the estate'
    case 'QUESTION':
      return 'Open or close a lead'
    case 'READ':
      return 'Read the board'
    default: {
      const unhandled: never = token
      return unhandled
    }
  }
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

/** How many more evidence rows each "Show older" asks the server for; it lists 50 by default. */
export const EVIDENCE_PAGE = 50

export type Stance = 'supports' | 'weakens' | 'neither'

export const STANCE_WORD: Record<Stance, string> = { supports: 'Supports', weakens: 'Goes against', neither: 'Neither' }

export const TIER_WORD: Record<string, string> = { telemetry: 'Telemetry', feed: 'Feed', not_evidence: 'Not counted' }

/** The word for one link's relation; an unknown relation shows as written. */
export function relationWord(relation: string): string {
  return relation === 'supports' || relation === 'weakens' || relation === 'neither' ? STANCE_WORD[relation] : relation
}

/** A row bears on several explanations; it is counted once, by its link to this one (neither when it has none). */
export function stanceOn(row: EvidenceRow, hypothesisId: string | undefined): Stance {
  const relation = row.bears_on.find((link) => link.hypothesis_id === hypothesisId)?.relation
  return relation === 'supports' || relation === 'weakens' ? relation : 'neither'
}

/**
 * The explanation the hunt was opened to test: the caller's own, else the definition's. The base rate
 * and the deployment-gap check are never it, however they rank.
 */
export function huntHypothesis(fold: HuntFold): HypothesisRow | null {
  const own = (provenance: string) => fold.hypotheses.find((h) => h.provenance === provenance)
  return own('operator') ?? own('hunt_spec') ?? null
}

/** Totals over every row the run holds, not the slice listed, each row once; critic rows are listed but not counted. Null when there is no explanation to read against. */
export function stanceTotals(fold: HuntFold, hypothesisId = huntHypothesis(fold)?.hypothesis_id): Record<Stance, number> | null {
  const h = fold.hypotheses.find((x) => x.hypothesis_id === hypothesisId)
  if (!h) return null
  return { supports: h.supports, weakens: h.weakens, neither: h.neither ?? Math.max(0, fold.evidenceCount - h.supports - h.weakens) }
}

/** Who made the move at this iteration; null when it fell outside the capped list. */
export function workerAt(fold: HuntFold, iteration: number): string | null {
  return fold.moves.find((move) => move.iteration === iteration)?.worker || null
}

/**
 * Who wrote a record row. A case activity is typed by its own activity_type, never as the run-event
 * kind it may share a name with: runs and findings are the agent's, a merge the system's, the rest
 * an analyst's.
 */
export function recordChip(kind: string, source = 'run'): RecordChip {
  if (source === 'case_activities') {
    if (kind.startsWith('agent_run_') || kind === 'finding_added') return 'agent'
    return kind === 'case_merged' ? 'system' : 'human'
  }
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
    case 'unread':
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
  // A structured conclusion leads with its disposition, which a clamped JSON line would cut off.
  const o = answer as Record<string, unknown>
  if (typeof o.summary === 'string' && o.summary) return [typeof o.disposition === 'string' ? wordDisplay(o.disposition) : '', o.summary].filter(Boolean).join(' — ')
  try {
    return JSON.stringify(answer)
  } catch {
    return ''
  }
}

// The conclusion's next_steps, which answerText flattens away; only strings count.
function nextStepsOf(answer: unknown): string[] {
  const steps = answer && typeof answer === 'object' ? (answer as Record<string, unknown>).next_steps : null
  return Array.isArray(steps) ? steps.filter((one): one is string => typeof one === 'string' && one.trim() !== '') : []
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
      cost_usd: num(o.cost_usd),
      ...(typeof o.duration_ms === 'number' ? { duration_ms: o.duration_ms } : {}),
      ...(typeof o.iteration === 'number' ? { iteration: o.iteration } : {}),
      ...(typeof o.ref === 'string' ? { ref: o.ref } : {}),
      ...(typeof o.query === 'string' ? { query: o.query } : {}),
      ...(typeof o.rows === 'number' ? { rows: o.rows } : {}),
      ...(typeof o.class === 'string' ? { class: o.class } : {}),
      ...(typeof o.thought === 'string' ? { thought: o.thought } : {}),
      ...(typeof o.failed === 'string' ? { failed: o.failed } : {}),
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

function checkpointOf(raw: unknown): OpenCheckpointRow | null {
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  if (!str(o.checkpoint_id)) return null
  const context = o.context && typeof o.context === 'object' ? (o.context as Record<string, unknown>) : {}
  const hypotheses = Array.isArray(context.hypotheses)
    ? context.hypotheses.flatMap((item) => {
        if (!item || typeof item !== 'object') return []
        const h = item as Record<string, unknown>
        return str(h.statement) ? [{ hypothesis_id: str(h.hypothesis_id), statement: str(h.statement), provenance: str(h.provenance) }] : []
      })
    : []
  return { checkpoint_id: str(o.checkpoint_id), checkpoint_class: str(o.checkpoint_class), question: str(o.question), raised_at: str(o.raised_at), hypotheses }
}

/** The record rows a hunt projection carries, and a kernel investigation's carries in the same shapes. */
function recordOf(raw: Record<string, unknown>): RunRecord {
  const hypotheses: HypothesisRow[] = Array.isArray(raw.hypotheses)
    ? raw.hypotheses.flatMap((item) => {
        if (!item || typeof item !== 'object') return []
        const o = item as Record<string, unknown>
        return [{
          hypothesis_id: str(o.hypothesis_id),
          statement: str(o.statement),
          status: str(o.status),
          supports: num(o.supports) ?? 0,
          weakens: num(o.weakens) ?? 0,
          neither: num(o.neither),
          resolution_reason: typeof o.resolution_reason === 'string' ? o.resolution_reason : null,
          provenance: str(o.provenance),
        }]
      })
    : []
  const evidence: EvidenceRow[] = Array.isArray(raw.evidence)
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
          origins: Array.isArray(o.origins) ? o.origins.filter((one): one is string => typeof one === 'string' && one !== '') : [],
          bears_on: bears,
          provenance: str(o.provenance),
        }]
      })
    : []
  return {
    hypotheses,
    evidence,
    evidenceCount: num(raw.evidence_count) ?? evidence.length,
    openQuestions: Array.isArray(raw.open_questions)
      ? raw.open_questions.flatMap((item) => {
          if (!item || typeof item !== 'object') return []
          const o = item as Record<string, unknown>
          return str(o.question) ? [{ question_id: str(o.question_id), question: str(o.question) }] : []
        })
      : [],
  }
}

function heldOf(raw: unknown): HeldRow | null {
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  return str(o.asked) && str(o.final) ? { field: str(o.field), asked: str(o.asked), final: str(o.final), reason: str(o.reason) } : null
}

// Every projection carries its disposition in the head, null when the run concluded none.
function dispositionOf(raw: Record<string, unknown>): string | null {
  return str(raw.disposition) || null
}

// Every projection carries the ceiling in its head; a ledger read before it did keeps it under budgets.
function ceilingOf(raw: Record<string, unknown>): number | null {
  const head = num(raw.max_cost_usd)
  if (head !== null) return head
  return raw.budgets && typeof raw.budgets === 'object' ? num((raw.budgets as Record<string, unknown>).max_cost_usd) : null
}

function asHunt(raw: Record<string, unknown>, run: RunMeta): HuntFold {
  const moves = Array.isArray(raw.moves) ? raw.moves : []
  // The server sends moves newest first.
  const rows = moves.flatMap((item) => {
    if (!item || typeof item !== 'object') return []
    const o = item as Record<string, unknown>
    return [{
      doing: str(o.query_intent) || actionWords(str(o.action)),
      worker: str(o.worker_agent_id),
      at: typeof o.created_at === 'string' ? o.created_at : null,
      iteration: num(o.iteration),
    }]
  })
  return {
    kind: 'hunt',
    run,
    iteration: num(raw.iteration) ?? 0,
    openCheckpoint: checkpointOf(raw.open_checkpoint),
    ...recordOf(raw),
    doing: rows[0]?.doing ?? '',
    worker: rows[0]?.worker ?? '',
    moves: rows,
    calls: callsOf(raw.calls),
    recall: recallOf(raw.recall),
    outcome: typeof raw.outcome === 'string' ? raw.outcome : null,
    reason: str(raw.reason),
    costUsd: num(raw.cost_usd),
    maxCostUsd: ceilingOf(raw),
    disposition: dispositionOf(raw),
  }
}

function asHead(raw: Record<string, unknown>, run: RunMeta): HeadFold {
  const ceiling = ceilingOf(raw)
  return {
    kind: 'head',
    run,
    openCheckpoint: checkpointOf(raw.open_checkpoint),
    outcome: typeof raw.outcome === 'string' ? raw.outcome : null,
    reason: str(raw.reason),
    costUsd: num(raw.cost_usd),
    maxCostUsd: (ceiling ?? 0) > 0 ? ceiling : null,
    disposition: dispositionOf(raw),
    doing: '',
    worker: '',
    moves: [],
    calls: [],
    recall: null,
  }
}

function asLead(raw: Record<string, unknown>, run: RunMeta): LeadFold {
  const decisions = Array.isArray(raw.decisions) ? raw.decisions : []
  // Decisions arrive oldest first; keep moves newest first like a hunt's.
  const rows = decisions.flatMap((item) => {
    if (!item || typeof item !== 'object') return []
    const o = item as Record<string, unknown>
    return [{ doing: actionWords(str(o.action)), worker: str(o.worker), at: null, iteration: null }]
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
  const ceiling = ceilingOf(raw)
  return {
    kind: 'lead',
    // A legacy lead ledger keeps no record, so carries none of its fields.
    record: Array.isArray(raw.hypotheses) ? recordOf(raw) : null,
    held: heldOf(raw.held),
    run,
    iterations: num(raw.iterations) ?? 0,
    doing: rows[0]?.doing ?? '',
    worker: rows[0]?.worker ?? '',
    moves: rows,
    findings,
    nextSteps: nextStepsOf(Array.isArray(raw.findings) && raw.findings[0] ? (raw.findings[0] as Record<string, unknown>).answer : null),
    calls: callsOf(raw.calls),
    gaps,
    recall: recallOf(raw.recall),
    outcome: typeof raw.outcome === 'string' ? raw.outcome : null,
    reason: str(raw.reason),
    costUsd: num(raw.cost_usd),
    maxCostUsd: (ceiling ?? 0) > 0 ? ceiling : null,
    disposition: dispositionOf(raw),
  }
}

export function readFold(body: unknown): RunFold | null {
  if (!body || typeof body !== 'object') return null
  const row = body as Record<string, unknown>
  const hunt = row.hunt && typeof row.hunt === 'object' ? (row.hunt as Record<string, unknown>) : null
  const projection = row.projection && typeof row.projection === 'object' ? (row.projection as Record<string, unknown>) : null
  const folded = hunt ?? projection
  if (!folded) return null
  const run = { status: str(row.status), error: str(row.error), reason: str(row.reason), huntStatus: str(hunt?.status) }
  // The head's run_kind decides; a body without one (a hunt before #2399) goes by the key it came under.
  const kind = str(folded.run_kind)
  if (kind === 'root_cause') return asHead(folded, run)
  if (kind === 'hunt' || (!kind && hunt)) return asHunt(folded, { ...run, huntStatus: str(folded.status) })
  return asLead(folded, run)
}

/** The record a run keeps, a hunt's or an investigation's; null for a run that tests no explanations. */
export function foldRecord(fold: RunFold | null): RunRecord | null {
  if (!fold) return null
  return fold.kind === 'hunt' ? fold : fold.kind === 'lead' ? fold.record : null
}

/** "Asked benign · held as needs review": the answer the guard would not let stand, in words. */
export function heldLine(held: HeldRow): string {
  return `Asked ${held.asked.replace(/_/g, ' ')} · held as ${held.final.replace(/_/g, ' ')}`
}

/** A run that is not going on and did not conclude: parked or paused (it can wake), or failed (it cannot). */
export interface StoppedRun {
  state: 'paused' | 'stopped'
  /** One plain sentence: the first of the joined reasons. */
  line: string
  /** The reason as the server wrote it; '' when none was recorded. */
  raw: string
  /** A hunt parked at one of its own limits (turns, time or budget), not on a question: Keep going lifts it. */
  atLimit: boolean
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
  const atLimit = !stopped && fold.kind === 'hunt' && huntStatus === 'parked' && fold.openCheckpoint === null
  return { state: stopped ? 'stopped' : 'paused', line, raw, atLimit }
}

/** A run that ended for good without failing: what it found, in one statement read off the ledger's own
 *  verdicts, and the open question it could not conclude past, when its reason names one. */
export interface EndedRun {
  /** "1 proven · 1 inconclusive"; for a run that tests no explanations, its findings. */
  line: string
  /** The reason as the server wrote it, model prose included; '' when none was recorded. */
  raw: string
  question: OpenQuestionRow | null
}

// The termination predicate's refusal names the question it would not conclude past.
const BLOCKED_BY = /\bopen question (q-[\w-]+)/

const ENDED_ORDER = ['proven', 'handed_off', 'standing', 'weakened', 'forming', 'inconclusive', 'parked', 'ruled out']

export function endedRun(fold: RunFold | null): EndedRun | null {
  if (!fold || ENDED.includes(fold.run.status)) return null
  if (fold.run.status !== 'completed' && fold.run.huntStatus !== 'terminal') return null
  const record = foldRecord(fold)
  if (!record) return { line: plural(fold.kind === 'lead' ? fold.findings.length : 0, 'finding'), raw: fold.reason, question: null }
  const tally = new Map<string, number>()
  for (const row of record.hypotheses) {
    const word = explanationWord(row.status, row.supports, row.weakens)
    tally.set(word, (tally.get(word) ?? 0) + 1)
  }
  const rank = (word: string) => (ENDED_ORDER.indexOf(word) < 0 ? ENDED_ORDER.length : ENDED_ORDER.indexOf(word))
  const line = [...tally.keys()].sort((a, b) => rank(a) - rank(b)).map((word) => `${tally.get(word)} ${word.replace(/_/g, ' ')}`).join(' · ')
  const cited = BLOCKED_BY.exec(fold.reason)?.[1]
  const held = fold.kind === 'lead' && fold.held ? heldLine(fold.held) : ''
  return {
    line: [held, line || 'No explanation was tested'].filter(Boolean).join(' · '),
    raw: fold.reason,
    question: cited ? record.openQuestions.find((q) => q.question_id === cited) ?? null : null,
  }
}

/** Model or worker text as one plain line: list markers, headings and emphasis dropped, so no raw markdown shows. */
export function plainLine(text: string): string {
  return text
    .split('\n')
    .map((line) => line.replace(/^\s*(?:[-*+]|\d+[.)]|#{1,6})\s+/, '').trim())
    .filter(Boolean)
    .join(' ')
    .replace(/(\*\*|__|`)/g, '')
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`
}

/** The header's sentence for a live run: where it stands, from what the fold already holds. */
export function runSentence(fold: RunFold | null): string {
  if (!fold) return ''
  if (fold.kind === 'lead') return `Step ${fold.iterations} · ${plural(fold.findings.length, 'finding')} so far`
  if (fold.kind === 'head') return ''
  return `Step ${fold.iteration} · ${plural(fold.hypotheses.length, 'explanation')} · ${plural(fold.evidenceCount, 'evidence row')} so far`
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
  if (fold.kind === 'head') return []
  return fold.gaps.map((gap) => ({
    id: gap.dispatch_id,
    text: [gap.query_intent || gap.agent_id, gap.failure_reason].filter(Boolean).join(' — ') || 'Dispatch failed',
  }))
}

interface AgentRow {
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

interface StrongRow {
  step: string
  text: string
  stance: 'For' | 'Against' | null
}

/** Up to 3 rows for the closed summary: a hunt's evidence that bears on a hypothesis, or a lead's answers. */
export function strongestRows(fold: RunFold | null): StrongRow[] {
  if (!fold || fold.kind === 'head') return []
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

/** One run as the case verdict reads it. */
export interface VerdictRun {
  fold: RunFold | null
  /** The run's title, as the page's headings write it. */
  label: string
  phase: Phase
}

export interface CaseVerdict {
  word: string
  /** One sentence naming the run that objects or could not be read; null when the runs agree. */
  basis: string | null
  /** What the run that spoke says to do next; empty when it lists none (a hunt lists none). */
  doNow: string[]
}

const ALARMS = ['malicious', 'suspicious']
const DISMISSALS = ['benign', 'false_positive']

/**
 * The one word every run on the case stands behind, or null before one can be said. Runs come NEWEST
 * FIRST (the page sorts by created_at descending; its own list puts the primary run first, which is not
 * that order). A run with no disposition does not speak; one still loading holds the whole verdict back,
 * so a dismissal never shows before every run is read. The newest speaker speaks, except that malicious
 * outranks suspicious and neither is outranked by a later inconclusive or needs_review. A dismissal
 * stands only over silent or dismissing runs and readable details: otherwise it reads needs_review and
 * the basis names the objecting or unread run.
 */
export function caseVerdict(runs: VerdictRun[]): CaseVerdict | null {
  if (runs.some((run) => run.phase === 'loading')) return null
  const spoke = runs.flatMap((run) => (run.fold?.disposition ? [{ label: run.label, word: run.fold.disposition, fold: run.fold }] : []))
  const newest = spoke[0]
  if (!newest) return null
  const doNow = (fold: RunFold) => (fold.kind === 'lead' ? fold.nextSteps : [])
  if (!DISMISSALS.includes(newest.word)) {
    const lead = ALARMS.map((word) => spoke.find((one) => one.word === word)).find(Boolean) ?? newest
    return { word: lead.word, basis: null, doNow: doNow(lead.fold) }
  }
  const objectors = spoke.filter((one) => !DISMISSALS.includes(one.word))
  const unread = runs.filter((run) => run.phase === 'error')
  if (objectors.length === 0 && unread.length === 0) return { word: newest.word, basis: null, doNow: doNow(newest.fold) }
  const parts = [...objectors.map((one) => `${one.label} says ${one.word.replace(/_/g, ' ')}`), ...unread.map((run) => `${run.label} could not be read`)]
  // The objector's own steps: the dismissing run's would argue the other way.
  const speaker = objectors[0] ?? newest
  return { word: 'needs_review', basis: `${parts.join('; ')}.`, doNow: doNow(speaker.fold) }
}

/** A closure that says false positive or says nothing, over a verdict that is not a dismissal. */
export function closureFlag(closure: { closure_category: string } | null | undefined, word: string | null | undefined): boolean {
  const category: string = closure?.closure_category ?? ''
  return (category === 'false_positive' || category === 'unspecified') && ['malicious', 'suspicious', 'needs_review'].includes(word ?? '')
}
