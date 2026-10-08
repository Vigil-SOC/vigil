/* What the run pages read of a run: its shapes, its two readers, and the small
   formatters both the Workflows screen and the Watch a run page use. Kept out of
   WorkflowsScreen.tsx so the page can import them without importing the screen. */
import { useCallback, useEffect, useRef, useState } from 'react'
import { workflowApi } from '../../services/api'

export function errMsg(e: unknown): string {
  const r = e as { response?: { data?: { detail?: string } }; message?: string }
  return r?.response?.data?.detail || r?.message || 'Something went wrong'
}

export interface WfRun {
  run_id: string
  status: string
  triggered_by?: string
  started_at?: string | null
  duration_ms?: number | null
  total_cost_usd?: number
  error?: string | null
  /** The agent-layer terminal. Null when this side finalized the run itself. */
  outcome?: string | null
  reason?: string | null
  /** Optional: runs that predate the name and version columns carry neither. */
  workflow_name?: string | null
  workflow_version?: number | null
  trigger_context?: Record<string, unknown> | null
}

export function fmtDuration(ms?: number | null): string {
  if (!ms) return '—'
  if (ms < 1000) return `${ms}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.round(ms / 60000)}m`
}

export interface WfPhase {
  phase_id: string
  phase_order: number
  agent_id: string
  status: string
  duration_ms?: number | null
  cost_usd?: number | null
  error?: string | null
}
/** A hunt reports beliefs and where each stands; it has no phases to report against. */
export interface HuntStanding {
  hypothesis_id: string
  statement: string
  status: string
  attack_technique?: string | null
  /** Techniques cited by evidence bearing on this belief — earned, not declared. */
  techniques_cited?: string[]
  resolution_reason?: string | null
  /** hunt_spec, operator or base_rate — which belief the operator put up themselves. */
  provenance?: string
  /** Counted from the full link set; absent on a run the projection predates. */
  supports?: number
  weakens?: number
}
/** One record the hunt gathered. */
export interface HuntEvidence {
  evidence_id: string
  iteration: number
  source_system: string
  summary: string
  why_notable?: string
  salience?: string
  attack_technique?: string | null
  attacker_influenceable?: boolean
  /** Whether anything the finding rests on was attested by the telemetry rather than
   *  authored by the adversary. The projection computes it, so the console shows the
   *  rule a verdict is gated on rather than a second opinion about it. */
  sensor_attested?: boolean
  rests_on?: { field: string; authored: 'sensor' | 'adversary' | 'third_party' }[]
  instruction_like?: boolean
  provenance?: string
  is_gap?: boolean
  /** Why the hunt could not look — kept out of the summary so plumbing is not read
   *  as telemetry, which makes this the only place an operator sees it. */
  gap_detail?: string | null
  bears_on?: { hypothesis_id: string; relation: string }[]
}

/** What the hunt could not answer. A blind spot, not a finding. */
export interface HuntGap {
  evidence_id: string
  iteration: number
  summary: string
  query_intent?: string
  hypothesis_id?: string | null
}
/** One critic verdict. The text is model output, not a finding. */
export interface HuntReview {
  iteration: number
  hypothesis_id: string
  survives: boolean
  strongest_benign_explanation: string
  rationale?: string
}
export interface HuntCheckpoint {
  checkpoint_id: string
  class: string
  raised_iteration?: number
  question: string
  resolution?: { answer: string; actor: string; text?: string } | null
}
/** A lead opened and not yet taken; an operator pins one with a boost directive. */
export interface HuntQuestion {
  question_id: string
  question: string
  entity_key?: string | null
  hypothesis_id?: string | null
  spawned_iteration?: number
}

/** One move the Hunt Lead made. The rationale is the whole of why a hunt did what
 *  it did, and rejected_attempts is the only account of a turn that stalled. */
export interface HuntMove {
  decision_id: string
  iteration: number
  action: string
  rationale: string
  target_entity?: string | null
  target_hypothesis_id?: string | null
  query_intent?: string
  worker_agent_id?: string | null
  cost_usd?: number
  rejected_attempts?: string[]
  duration_ms?: number
  created_at?: string
}
/** The account of the run, as data. next_steps arrive already normalised to
 *  strings, so this side never has two shapes to read. */
export interface HuntNarrative {
  summary: string
  what_happened: string
  next_steps: string[]
  model_id: string
  written_at: string
}

/** What a run read out of episodic memory before it started. Mirrors the recall
 *  contract in services/agent/contracts/memory.ts; the fields this panel does not
 *  show are omitted rather than restated. */
export interface RecalledFrom {
  investigation_kind: string
  investigation_id: string
  concluded_at: string
}
export interface RecalledWindow { first_seen: string; last_seen: string }
export interface RecalledVerdict extends RecalledFrom {
  hypothesis_id: string
  statement: string
  outcome: string
  rationale: string
  subject_entities: string[]
  /** A conclusion resting only on fields an adversary could have written is not one
   *  to lean on, which is why it is called out rather than left in the rationale. */
  attacker_influenceable_only: boolean
  trust: string
  window: RecalledWindow
  window_source: string
}
export interface RecalledGap extends RecalledFrom {
  hypothesis_id: string
  statement: string
  disposition: string
  reason: string
  subject_entities: string[]
}
export interface RecalledSighting extends RecalledFrom {
  entity_key: string
  source_system: string
  hit_count: number
  attacker_influenceable: boolean
  window: RecalledWindow
}
export interface DroppedRows { per_key_cap: number; overall_cap: number }
/** The journaled read. `unavailable` means it could not be served; empty lists mean
 *  it ran and found nothing, which is a fact about the entities rather than about
 *  memory. The panel must not render the two alike. */
export interface HuntRecall {
  keys: string[]
  as_of: string
  unavailable?: string
  sightings?: RecalledSighting[]
  verdicts?: RecalledVerdict[]
  gaps?: RecalledGap[]
  dropped?: { sightings: DroppedRows; verdicts: DroppedRows; gaps: DroppedRows }
}

export interface HuntHandoff {
  case_id: string
  hypothesis_id: string
  iteration: number
  rationale: string
}
export interface HuntStrength {
  corroborating_sources: number
  contradicting_records: number
  open_gaps: number
  attacker_influenceable_only: boolean
  survived_disconfirmation: boolean
}
/** The derived deliverable. Null until the hunt writes one, so the panel reads
 *  the live fields until it exists and the report itself afterwards. */
export interface HuntReport {
  gaps: HuntGap[]
  checkpoints: HuntCheckpoint[]
  hypotheses: { hypothesis_id: string; evidence_strength?: HuntStrength | null }[]
  unruled?: number
}
export interface HuntCall {
  question: string
  tool: string
  result_length: number
  cost_usd: number
  duration_ms?: number
  iteration?: number
  /** How the call failed; absent when it did not, and from an older agent service. */
  failed?: CallFailure
}
export interface HuntBudgets {
  max_iterations: number
  max_cost_usd: number
}
export interface HuntView {
  run_id?: string
  name?: string
  scope?: Record<string, unknown>
  status: string
  /** Why it ended, which is not whether it succeeded: a hunt stopped at its ceiling
   *  finalises as completed. */
  outcome?: string | null
  /** Which arm of the budget bound, or what an operator did. Not an error. */
  reason?: string | null
  iteration: number
  evidence_count: number
  /** Capped by the projection; evidence_count stays the untruncated total. */
  evidence?: HuntEvidence[]
  cost_usd?: number
  /** What this run was granted, extensions included — not the shipped default. */
  budgets?: HuntBudgets
  hypotheses: HuntStanding[]
  /** Every critic verdict, in ledger order; absent before the projection carried them. */
  reviews?: HuntReview[]
  open_checkpoint?: {
    checkpoint_id: string
    checkpoint_class?: string
    question: string
    raised_at?: string
    context?: Record<string, unknown>
  } | null
  report?: HuntReport | null
  report_markdown?: string | null
  narrative?: HuntNarrative | null
  handoffs?: HuntHandoff[]
  /** Newest first, capped by the projection (MOVES_SHOWN). */
  moves?: HuntMove[]
  /** Every dispatch's calls in ledger order; `iteration` ties one to its move. */
  calls?: HuntCall[]
  open_questions?: HuntQuestion[]
  /** Off the run's own ledger, never a fresh read: memory has moved since, and a
   *  panel that re-read it would show what the hunt never saw. Absent when the run
   *  never asked -- older runs, and runs whose beliefs named no entity. */
  recall?: HuntRecall | null
}
export interface WfRunDetail extends WfRun {
  result_summary?: string | null
  phases?: WfPhase[]
  hunt?: HuntView | null
  /** What the agent layer folds for a run that is not a hunt. Its shape is the kind's. */
  projection?: unknown
}

export const RUN_POLL_MS = 5_000
export const IN_FLIGHT = ['running', 'paused', 'pending']

/** One run's detail, refreshed while it is in flight. Shared by the start modal and
 *  the history row so one run has one poller. */
export function useRunDetail(runId: string, watching: boolean, seed?: string) {
  const [detail, setDetail] = useState<WfRunDetail | null>(null)
  const [dphase, setDphase] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle')

  const current = useRef(runId)
  current.current = runId
  const load = useCallback(
    () =>
      workflowApi
        .getRun(runId)
        .then((res) => {
          if (current.current !== runId) return // a read for a run no longer shown
          setDetail(res.data as WfRunDetail)
          setDphase('ready')
        })
        .catch(() => setDphase((p) => (p === 'ready' ? p : 'error'))),
    [runId],
  )

  // Polls only while we know the run is in flight. A missing status is not
  // treated as running: a deep-link with no seed would otherwise poll a 404
  // until a later effect stopped it.
  const status = detail?.status ?? seed
  const live = Boolean(watching && status && IN_FLIGHT.includes(status))
  useEffect(() => {
    if (!live) return
    const timer = setInterval(() => { void load() }, RUN_POLL_MS)
    return () => clearInterval(timer)
  }, [live, load])

  return { detail, dphase, setDphase, load }
}

/** What replay returns for an investigate run. Compose 404s
 *  here; that is not a failure, and the phase view stays as it was. */
export interface InvestigateDecisionView {
  iteration: number
  action: string
  rationale: string
  cost_usd: number
  calls: unknown[]
  /** Absent until the agent records them; the page hides what it cannot show. */
  at?: string
  worker?: string | null
  duration_ms?: number
}

/** One entry of a root-cause replay, in ledger order. `recorded_at` is when the
 *  ledger wrote it; a step's own `at` is when its event happened. */
export type RootCauseEntry =
  | { kind: 'search'; recorded_at: string; tool: string; args: string; rows: number; failed: boolean; failure?: CallFailure }
  | {
      kind: 'step'; recorded_at: string; step_id: string; event: string; who: string; at: string; link: string
      cause_id: string | null; origin: boolean; link_status: string; origin_status: string
    }
  | { kind: 'notice'; recorded_at: string; text: string }

/** Absent on a ledger that predates the run event's budgets. */
export interface RootCauseBudgets { max_calls?: number }

export type ReplayRead =
  | { kind: 'pending' }
  | { kind: 'absent' }
  | { kind: 'failed'; message: string }
  | { kind: 'investigate'; decisions: InvestigateDecisionView[] }
  | { kind: 'root_cause'; entries: RootCauseEntry[]; budgets: RootCauseBudgets }

export function statusOf(e: unknown): number | undefined {
  return (e as { response?: { status?: number } }).response?.status
}

export function decisionsOf(raw: unknown[]): InvestigateDecisionView[] {
  return raw.map((item, at) => {
    const row = (typeof item === 'object' && item !== null ? item : {}) as Partial<InvestigateDecisionView>
    return {
      iteration: typeof row.iteration === 'number' ? row.iteration : at + 1,
      action: typeof row.action === 'string' ? row.action : '',
      rationale: typeof row.rationale === 'string' ? row.rationale : '',
      cost_usd: typeof row.cost_usd === 'number' ? row.cost_usd : 0,
      calls: Array.isArray(row.calls) ? row.calls : [],
      ...(typeof row.at === 'string' ? { at: row.at } : {}),
      ...(typeof row.worker === 'string' ? { worker: row.worker } : {}),
      ...(typeof row.duration_ms === 'number' ? { duration_ms: row.duration_ms } : {}),
    }
  })
}

const FAILURES: readonly string[] = ['timeout', 'unavailable', 'backend_error', 'refused', 'invalid_args']
const str = (v: unknown) => (typeof v === 'string' ? v : '')

/** Unknown entry kinds are dropped, so a newer agent service cannot break the page. */
export function entriesOf(raw: unknown[]): RootCauseEntry[] {
  return raw.flatMap((item): RootCauseEntry[] => {
    const row = (typeof item === 'object' && item !== null ? item : {}) as Record<string, unknown>
    const recorded_at = str(row.recorded_at)
    if (row.kind === 'search') {
      const failure = FAILURES.includes(str(row.failure)) ? (row.failure as CallFailure) : undefined
      return [{
        kind: 'search', recorded_at, tool: str(row.tool) || 'search', args: str(row.args),
        rows: typeof row.rows === 'number' ? row.rows : 0, failed: row.failed === true, ...(failure ? { failure } : {}),
      }]
    }
    if (row.kind === 'step') {
      return [{
        kind: 'step', recorded_at, step_id: str(row.step_id), event: str(row.event), who: str(row.who), at: str(row.at),
        link: str(row.link), cause_id: typeof row.cause_id === 'string' ? row.cause_id : null, origin: row.origin === true,
        link_status: str(row.link_status) || 'none', origin_status: str(row.origin_status) || 'none',
      }]
    }
    return row.kind === 'notice' ? [{ kind: 'notice', recorded_at, text: str(row.text) }] : []
  })
}

export function pullReplay(runId: string, live: () => boolean, setRead: (next: ReplayRead) => void) {
  workflowApi
    .replayRun(runId)
    .then((res) => {
      if (!live()) return
      const body = res.data as { run_kind?: unknown; decisions?: unknown; steps?: unknown; budgets?: { max_calls?: unknown } }
      if (body?.run_kind === 'root_cause' && Array.isArray(body.steps)) {
        const max = body.budgets?.max_calls
        setRead({ kind: 'root_cause', entries: entriesOf(body.steps), budgets: typeof max === 'number' ? { max_calls: max } : {} })
      } else if (body?.run_kind === 'investigate' && Array.isArray(body.decisions)) {
        setRead({ kind: 'investigate', decisions: decisionsOf(body.decisions) })
      } else {
        setRead({ kind: 'absent' })
      }
    })
    .catch((e: unknown) => {
      if (!live()) return
      setRead(statusOf(e) === 404 ? { kind: 'absent' } : { kind: 'failed', message: errMsg(e) })
    })
}

/** Own request, not folded into getRun. Polled on the same interval while the
 *  detail is open and the run is in flight; a finished run is read once.
 *  A slower earlier response cannot overwrite a later one. */
export function useInvestigateReplay(runId: string, inFlight: boolean): ReplayRead {
  const [read, setRead] = useState<ReplayRead>({ kind: 'pending' })
  const req = useRef(0)

  const pull = useCallback(() => {
    const mine = ++req.current
    pullReplay(runId, () => req.current === mine, setRead)
  }, [runId])

  useEffect(() => {
    setRead({ kind: 'pending' })
    pull()
    return () => { req.current += 1 }
  }, [pull])

  useEffect(() => {
    if (!inFlight) return
    const timer = setInterval(pull, RUN_POLL_MS)
    return () => clearInterval(timer)
  }, [inFlight, pull])

  // getRun can observe terminal before the next replay tick. One more read then,
  // so the decision that ended the run is on the open panel.
  const wasLive = useRef(inFlight)
  useEffect(() => {
    if (wasLive.current && !inFlight) pull()
    wasLive.current = inFlight
  }, [inFlight, pull])

  return read
}

export function callLine(call: unknown): { tool: string; rest: string } {
  if (typeof call !== 'object' || call === null) return { tool: 'call', rest: String(call) }
  const rec = call as { tool?: unknown; arguments?: unknown; result?: unknown }
  const tool = typeof rec.tool === 'string' && rec.tool !== '' ? rec.tool : 'call'
  const args = rec.arguments
  const argText = typeof args === 'string' ? args : args === undefined ? '' : JSON.stringify(args)
  const result = typeof rec.result === 'string' ? rec.result : ''
  const clipped = result.length > 160 ? `${result.slice(0, 160)}…` : result
  const rest = [argText !== '' && argText !== '{}' ? argText : '', clipped].filter(Boolean).join(' — ')
  return { tool, rest }
}

/** How a journaled call failed, if it did. No status is stored: a failed call's
 *  result is the wrapped text `failed: <kind> -- <detail>` (core/security.ts). */
export type CallFailure = 'timeout' | 'unavailable' | 'backend_error' | 'refused' | 'invalid_args'
const FAILED_CALL = /^(?:<vigil:tool_result[^>]*>\s*)?failed: (timeout|unavailable|backend_error|refused|invalid_args) -- /

export function callFailure(call: unknown): CallFailure | null {
  const result = (call as { result?: unknown } | null)?.result
  return typeof result === 'string' ? ((FAILED_CALL.exec(result)?.[1] as CallFailure | undefined) ?? null) : null
}

export function runStatusColor(s: string): string {
  if (s === 'completed') return 'var(--ok)'
  if (s === 'failed' || s === 'cancelled') return 'var(--crit)'
  if (s === 'paused') return 'var(--high)'
  return 'var(--med)' // running
}
