import { ZERO_TOKENS, type SpendPayload } from "../../contracts/budget.js";
import type { State } from "../../core/seams.js";
import { InProcessState } from "../../core/state.js";
import { DEFAULT_RUNTIME, type RunSpec } from "../../core/spec.js";
import { DEFAULT_CHECKPOINTS, type Checkpoints } from "../../plugins/hunt/checkpoints.js";
import {
  DEFAULT_ENRICHMENT,
  DEFAULT_HYPOTHESIS_LOOP,
  DEFAULT_TERMINATION,
  DEFAULT_VERDICTS,
  type HuntSpec,
  type Termination,

} from "../../plugins/hunt/config.js";
import { startHunt } from "../../plugins/hunt/opening.js";
import { HuntBook } from "../../plugins/hunt/book.js";
import { InProcessDirectiveQueue } from "../../plugins/hunt/directives.js";
import { Journal, type HuntEvent, type HuntKinds } from "../../plugins/hunt/journal.js";
import { newId } from "../../plugins/hunt/ids.js";
import type { HuntReport } from "../../plugins/hunt/report.js";
import { NULL_CHECK_PROVENANCE } from "../../plugins/hunt/strength.js";
import { DEFAULT_BUDGETS, type Budgets, type Decision, type Entity, type EvidenceRecord, type LinkRelation, type NullCheckResult } from "../../plugins/hunt/types.js";

export const SCRIPTED_MODEL_ID = "scripted";

export const INVESTIGATE: Decision = { action: "INVESTIGATE", rationale: "look", query_intent: "baseline" };
export const SEED_IP: Entity = { type: "ip", value: "45.77.53.176" };

const LEAD = { prompt: "lead", description: "the hunt lead", output_schema: {}, tools: [], needs: [] };

export interface SpecOverrides {
  hypotheses?: string[];
  operatorHypotheses?: string[];
  // Keyed by statement, as the run modal sends them.
  operatorHypothesisSubjects?: Record<string, Entity[]>;
  // Positional against hypotheses, the same way the resolver builds it.
  attackTechniques?: string[];
  budgets?: Budgets;
  termination?: Partial<Termination>;
  checkpoints?: Partial<Checkpoints>;
  scope?: Record<string, unknown>;
  allowance?: RunSpec["allowance"];
  hypothesisLoop?: boolean;
  // What the plugin asks for and what the deployment answers with, so a test can
  // put a capability out of reach without standing up a registry.
  needs?: string[];
  tools?: RunSpec["tools"];
  // The playbook's standing brief and the job's own, which startHunt joins.
  narrative?: string;
  prompt?: string;
}

// Built as an object rather than parsed from three files: the loader has its own
// tests, and a controller test that had to write YAML would be testing both.
export function huntSpecFor(overrides: SpecOverrides = {}): HuntSpec {
  const hypotheses = overrides.hypotheses ?? ["a credential is used from new infrastructure"];
  return {
    sections: {},
    model: "scripted",
    budgets: overrides.budgets ?? DEFAULT_BUDGETS,
    runtime: DEFAULT_RUNTIME,
    tools: overrides.tools ?? [],
    approvals: [],
    thresholds: {},
    arch: "threathunt",
    hypothesis_loop: overrides.hypothesisLoop ?? DEFAULT_HYPOTHESIS_LOOP,
    name: "test hunt",
    description: "",
    use_case: "",
    trigger_examples: [],
    phases: [],
    prompt: overrides.prompt ?? "",
    objectives: [],
    scope: overrides.scope ?? {},
    narrative: overrides.narrative ?? "",
    roles: { lead: LEAD },
    ...(overrides.allowance === undefined ? {} : { allowance: overrides.allowance }),
    digest: {},
    hypotheses,
    operator_hypotheses: overrides.operatorHypotheses ?? [],
    operator_hypothesis_subjects: overrides.operatorHypothesisSubjects ?? {},
    attack_techniques: overrides.attackTechniques ?? [],
    data_domains: [],
    enrichment: DEFAULT_ENRICHMENT,
    checkpoints: { ...DEFAULT_CHECKPOINTS, ...overrides.checkpoints },
    termination: { ...DEFAULT_TERMINATION, ...overrides.termination },
  };
}

export interface Started {
  ledger: Journal;
  state: InProcessState<HuntKinds>;
  queue: InProcessDirectiveQueue;
  runId: string;
  hypothesisIds: string[];
  spend: Spender;
}

export async function newLedger(overrides: SpecOverrides = {}): Promise<Started> {
  const state = new InProcessState<HuntKinds>();
  const queue = new InProcessDirectiveQueue();
  const runId = newId("run");
  const ledger = await startHunt(state, queue, runId, huntSpecFor(overrides), "worker", "hunt", overrides.needs);
  return { ledger, state, queue, runId, hypothesisIds: [...ledger.projection.hypotheses.keys()], spend: spender(state, runId) };
}

// Journals one model call's spend the way the stream does (core/stream.ts): straight
// to State, past the controller's journal. hunt.cost_usd is folded from these, so a
// double that only returns cost_usd would leave the budget counter at zero.
export type Spender = (cost_usd: number, role: string) => Promise<void>;

export function spender(state: State<HuntKinds>, runId: string): Spender {
  return async (cost_usd, role) => {
    const payload: SpendPayload = {
      model_id: SCRIPTED_MODEL_ID,
      provider_type: "scripted",
      role,
      tokens: ZERO_TOKENS,
      cost_usd,
      pricing_source: "scripted",
      rates: null,
      fetched_at: null,
    };
    await state.append(runId, [{ run_id: runId, run_kind: "hunt", kind: "spend", payload }]);
  };
}

// What answering a checkpoint hours later does: nothing of the writing process
// carries over, and a directive queued while nobody held the ledger still waits.
export async function reopen(started: Pick<Started, "ledger" | "state" | "queue" | "runId">, from?: Journal): Promise<Journal> {
  await (from ?? started.ledger).flush();
  return Journal.open(started.state, started.queue, started.runId);
}

// The hunt's record as the kernel's hunt plugin keeps it: the same writes the
// controller made, reached through the book its tools use.
export function bookOf(ledger: Journal): HuntBook {
  return new HuntBook(ledger);
}

// What the disconfirmation critic answers. survives true means the benign
// explanation did not account for the evidence.
export function nullCheck(survives: boolean, explanation = "a scheduled backup job"): NullCheckResult {
  return {
    survives,
    strongest_benign_explanation: explanation,
    rationale: survives ? "the benign explanation does not account for the evidence" : "the benign explanation accounts for it",
    cost_usd: 0,
    model_id: SCRIPTED_MODEL_ID,
    prompt_version: "scripted",
  };
}

// Puts one hypothesis up for a verdict the way validate_hypothesis does: the critic
// argues against everything linked to it, and the book applies the rules.
export function validate(ledger: Journal, hypothesisId: string, survives = true): { said: string; parked?: string } {
  const book = bookOf(ledger);
  const hypothesis = ledger.projection.hypotheses.get(hypothesisId);
  if (hypothesis === undefined) throw new Error(`${hypothesisId} is not on the ledger`);
  const argued = book.nullCheckInput(hypothesis).evidence.map((linked) => linked.record.evidence_id);
  return book.applyVerdict(hypothesisId, nullCheck(survives), argued);
}

export interface EvidenceOptions {
  source?: string;
  relation?: LinkRelation;
  attackerInfluenceable?: boolean;
  restsOn?: { field: string; authored: "sensor" | "adversary" | "third_party" }[];
  entities?: Entity[];
  attackTechnique?: string;
}

export function evidenceOn(ledger: Journal, hypothesisId: string, options: EvidenceOptions = {}): string {
  const source = options.source ?? "duckdb";
  const evidenceId = newId("ev");
  ledger.append({
    kind: "evidence",
    payload: {
      evidence_id: evidenceId,
      dispatch_id: null,
      iteration: 1,
      source_system: source,
      summary: `${source} saw the identity authenticate from ${SEED_IP.value}`,
      payload: { rows: 3, src_ip: SEED_IP.value },
      salience: "notable",
      why_notable: "first use of this ASN by the identity",
      provenance: "worker",
      attacker_influenceable: options.attackerInfluenceable ?? false,
      ...(options.restsOn === undefined ? {} : { rests_on: options.restsOn }),
      instruction_like: false,
      entities: options.entities ?? [],
      captured_at: new Date().toISOString(),
      ...(options.attackTechnique !== undefined ? { attack_technique: options.attackTechnique } : {}),
    },
  });
  ledger.append({
    kind: "link",
    payload: { evidence_id: evidenceId, hypothesis_id: hypothesisId, relation: options.relation ?? "supports" },
  });
  return evidenceId;
}

// Evidence with no link at all, so a test can wire the relations it needs
// across several hypotheses rather than the single one evidenceOn assumes.
export function bareEvidence(ledger: Journal, source = "duckdb", iteration = 1): string {
  const evidenceId = newId("ev");
  ledger.append({
    kind: "evidence",
    payload: {
      evidence_id: evidenceId,
      dispatch_id: null,
      iteration,
      source_system: source,
      summary: `${source} observed something`,
      payload: { rows: 3 },
      salience: "notable",
      why_notable: "",
      provenance: "worker",
      attacker_influenceable: false,
      instruction_like: false,
      entities: [],
      captured_at: new Date().toISOString(),
    },
  });
  return evidenceId;
}

export function relate(ledger: Journal, evidenceId: string, hypothesisId: string, relation: LinkRelation): void {
  ledger.append({ kind: "link", payload: { evidence_id: evidenceId, hypothesis_id: hypothesisId, relation } });
}

// Enough support to clear every verdict predicate, so what a test is measuring is
// the checkpoint or the termination rather than the strength computation.
export function provable(ledger: Journal, hypothesisId: string): string[] {
  return [
    evidenceOn(ledger, hypothesisId, { source: "cloudtrail", entities: [SEED_IP] }),
    evidenceOn(ledger, hypothesisId, { source: "duckdb", entities: [SEED_IP] }),
  ];
}

// A query the hunt wanted and could not run, recorded the way a failed dispatch
// records one — each distinct blind spot needs its own intent.
let unanswered = 0;
export function gapOn(ledger: Journal, hypothesisId: string | null, intent = `question ${(unanswered += 1)}`): void {
  const dispatchId = newId("dsp");
  ledger.append({
    kind: "dispatch",
    payload: {
      dispatch_id: dispatchId,
      iteration: 1,
      agent_id: "threat_hunter",
      status: "failed",
      query_intent: intent,
      target_hypothesis_id: hypothesisId,
      question_id: null,
      failure_reason: "timeout",
      cost_usd: 0,
      calls: [],
    },
  });
  ledger.append({
    kind: "evidence",
    payload: {
      evidence_id: newId("ev"),
      dispatch_id: dispatchId,
      iteration: 1,
      source_system: "dispatcher",
      summary: "worker failed: timeout",
      payload: {},
      salience: "routine",
      why_notable: "a query the hunt wanted could not be run",
      provenance: "tool_failure",
      attacker_influenceable: false,
      instruction_like: false,
      entities: [],
      captured_at: new Date().toISOString(),
    },
  });
}

export interface QuestionFields {
  entity_key?: string | null;
  spawned_iteration?: number;
  status?: "open" | "closed";
  hypothesis_id?: string | null;
  spawning_evidence_id?: string | null;
}

export function question(ledger: Journal, text: string, fields: QuestionFields = {}): string {
  const questionId = newId("q", 4);
  ledger.append({
    kind: "question",
    payload: {
      question_id: questionId,
      question: text,
      status: fields.status ?? "open",
      entity_key: fields.entity_key ?? null,
      spawning_evidence_id: fields.spawning_evidence_id ?? null,
      spawning_dispatch_id: null,
      spawned_iteration: fields.spawned_iteration ?? 1,
      hypothesis_id: fields.hypothesis_id ?? null,
      closed_reason: null,
    },
  });
  return questionId;
}

// Terminal by any route the hunt actually has; the predicate cares that nothing
// is active, not how it got there.
export function resolve(ledger: Journal, hypothesisId: string, status = "parked"): void {
  ledger.patch("hypothesis", hypothesisId, { status, resolution_reason: `${status} for this test` });
}

// Drives one hypothesis to inconclusive through the real gap-lock path: enough
// support to clear every other predicate, and enough blindness that it cannot be.
export async function gapLock(ledger: Journal, hypothesisId: string): Promise<void> {
  evidenceOn(ledger, hypothesisId, { source: "cloudtrail" });
  evidenceOn(ledger, hypothesisId, { source: "duckdb" });
  for (let gap = 0; gap < DEFAULT_VERDICTS.gap_lock_threshold; gap += 1) gapOn(ledger, hypothesisId);
  validate(ledger, hypothesisId, true);
  await ledger.flush();
}

export function nullChecks(ledger: Journal): EvidenceRecord[] {
  return [...ledger.projection.evidence.values()].filter((r) => r.provenance === NULL_CHECK_PROVENANCE);
}

export function events(ledger: Journal): readonly HuntEvent[] {
  return ledger.log;
}

export function finalized(ledger: Journal): HuntReport[] {
  return events(ledger)
    .filter((event) => event.kind === "finalize")
    .map((event) => event.payload as HuntReport);
}
