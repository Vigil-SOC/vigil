import type { Budget } from "../../contracts/budget.js";
import type { State } from "../../core/seams.js";
import type { HuntKinds } from "./journal.js";
import { type HuntSpec } from "./config.js";
import { newId } from "./ids.js";
import {
  AUTO_ACTOR,
  DEFAULT_CHECKPOINTS,
  pendingCheckpoints,
  raiseCheckpoint,
  resolutionOf,
  resolveCheckpoint,
  type Checkpoint,
  type CheckpointClass,
  type Checkpoints,
  type Resolution,
} from "./checkpoints.js";
import { BudgetRefused, LeadParked } from "./adapters.js";
import { GatewayExhausted } from "../../core/limiter.js";
import { buildDigest, focusOf, rankFrontier, suppressedEntities } from "./digest.js";
import { buildEntityGraph, entitiesOf, fromText, key } from "./entities.js";
import { drain, grantOf, journalNote, peek } from "./inbox.js";
import { Journal, type Projection } from "./journal.js";
import type {
  DecisionProvider,
  DirectiveQueue,
  DisconfirmationCritic,
  Enricher,
  WorkerDispatcher,
} from "./ports.js";
import { narrativeOf } from "./render.js";
import { buildReport, renderCaseFile } from "./report.js";
import { sanitize, sanitizeQuestion } from "./sanitize.js";
import {
  DEFAULT_DISPATCH,
  unboundCapabilities,
  type DispatchPolicy,

} from "../../core/spec.js";
import {  DEFAULT_DIGEST,
  DEFAULT_ENRICHMENT,
  DEFAULT_TERMINATION,
  DEFAULT_VERDICTS,
  type DigestPolicy,
  type Termination,
  type Verdicts,} from "./config.js";
import { terminationVerdict, type TerminationVerdict } from "./termination.js";
import {
  CRITIC_SOURCE_SYSTEM,
  DEPLOYMENT_GAP_PROVENANCE,
  evidenceStrength,
  NULL_CHECK_PROVENANCE,
  OPERATOR_GAP_PROVENANCE,
  UNDECLARED_SOURCE,
  sensorAttested,
  unmetPredicates, pairKey, unclassified } from "./strength.js";
import {
  ACTIONS_REQUIRING_CITATION,
  DECISION_ACTIONS,
  OUTCOME_PRECEDENCE,
  type Budgets,
  type Decision,
  type Handoff,
  type DecisionAction,
  type DecisionResult,
  type Digest,
  type Directive,
  type DispatchRequest,
  type DispatchResult,
  type Entity,
  type EvidenceRecord,
  type Expansion,
  type HuntOutcome,
  type HuntState,
  type Hypothesis,
  type IterationResult,
  type NullCheckEvidence,
  type NullCheckInput,
  type NullCheckResult,
  type OpenQuestion,
  type WorkerEvidence,
  CALLS_PER_ITERATION,
} from "./types.js";

// A dispatch that never ran because an operator had already halted the hunt.
// Recorded through the failure path like any other unanswered query: the record
const SKIPPED_ON_ABORT = "skipped: an operator abort was queued before this worker started";

// A worker that was running when the operator halted the hunt. Cancelled rather
// than waited on: an abort that takes a minute to land is one an operator stops
const CANCELLED_ON_ABORT = "cancelled mid-query: an operator halted the hunt while this worker was running";

// How often the inbox is read while workers are in flight. Cheap — one small
// file — and it bounds how long a hard abort waits on work already started.
const ABORT_POLL_MS = 500;

// Prefixes the park a raised checkpoint sets, so lifting one can never lift the
// budget park, which takes a different set of answers entirely.
const AWAITING = "awaiting ";

// The verbs that spend a worker on an entity. What an operator's known-benign
// call actually forbids: not knowing about the entity, but chasing it further.
const OPENS_WORK: ReadonlySet<DecisionAction> = new Set(["INVESTIGATE", "DEEPEN", "PIVOT"]);

export const DEFAULT_WORKER_AGENT_ID = "threat_hunter";

// One emission plus two re-asks. Bounded because a Hunt Lead that cannot obey
// the vocabulary will not learn to on the tenth try, and every ask costs money.
export const MAX_DECISION_ATTEMPTS = 3;

// Seeded at its base rate: most activity that looks like this is not an attack,
// so the benign account starts as the hypothesis to beat rather than an objection.
export const BASE_RATE_PROVENANCE = "base_rate";
export const NULL_HYPOTHESIS = "the activity has a benign explanation and no attack occurred";

// What the deployment reports about its own reach, never a worker's telemetry. Kept
// out of data_domains so it earns no corroboration credit.
export const DEPLOYMENT_SOURCE = "deployment";

// A belief the caller put up for this run, rather than one the definition states.
// Both are contenders the null must beat; only the provenance tells them apart.
export const OPERATOR_HYPOTHESIS_PROVENANCE = "operator";

// EXPAND does not advance the iteration, so only this stops a lead reading forever.
export const MAX_EXPANSIONS = 3;

// Total characters of raw payload one expansion may add. Rounds are bounded
// already; without this the context is bounded only by how many ids are named.
const EXPANSION_BUDGET = 12_000;

export class HuntAlreadyTerminal extends Error {}
export class HuntParked extends Error {}
export class InvalidDecision extends Error {}

interface FanOutTarget {
  focus: string;
  hypothesisId: string | null;
  questionId: string | null;
}

// Either the critic ran, or it did not and the hunt is owed the reason: silence
// must never read the same as a hypothesis that withstood the argument. The cost
interface NullCheckAttempt {
  result: NullCheckResult | null;
  blocked: string;
  cost_usd: number;
  // Exactly what the critic was shown. A later verdict must not rest on an
  // argument that never saw half the evidence now on the record.
  argued: string[];
}

const NO_NULL_CHECK: NullCheckAttempt = { result: null, blocked: "", cost_usd: 0, argued: [] };

// Named because two places have to agree: the record is written in one and its
// entities are refused in the other.
export const TOOL_FAILURE = "tool_failure";

// Which arm of the budget stopped the run, or null while both have room. Named, so
// "budget exhausted" is never reported beside a cost ceiling with room left in it.
export function boundBy(hunt: HuntState): "iterations" | "cost" | null {
  if (hunt.cost_usd >= hunt.budgets.max_cost_usd) return "cost";
  if (hunt.iteration >= hunt.budgets.max_iterations) return "iterations";
  return null;
}

// Both numbers either way, because the one with room is the answer to "then why
// did it stop" and is the first thing an operator looks for.
export function boundReason(hunt: HuntState): string {
  const spend = `$${hunt.cost_usd.toFixed(4)} of $${hunt.budgets.max_cost_usd.toFixed(2)}`;
  const turns = `iteration ${hunt.iteration} of ${hunt.budgets.max_iterations}`;
  if (boundBy(hunt) === "cost") return `spent its allowance: ${spend}, at ${turns}`;
  return `ran out of turns: ${turns}, having spent ${spend}`;
}

// Where the hunt stood, claiming nothing about why it stopped — unlike boundReason,
// which is only ever asked when a ceiling bound.
export function standingOf(hunt: HuntState): string {
  return (
    `iteration ${hunt.iteration} of ${hunt.budgets.max_iterations}, ` +
    `having spent $${hunt.cost_usd.toFixed(4)} of $${hunt.budgets.max_cost_usd.toFixed(2)}`
  );
}

// A call that died mid-way still spent. Duck-typed rather than reaching into the
// LLM module, so the controller stays free of it.
function spentBefore(error: unknown): number {
  const cost = (error as { cost_usd?: unknown }).cost_usd;
  return typeof cost === "number" ? cost : 0;
}

// Raw payloads, not digest summaries: the critic argues against what was
// actually collected rather than against the Hunt Lead's compression of it.
function nullCheckInput(projection: Projection, hypothesis: Hypothesis): NullCheckInput {
  const evidence = projection.links
    .filter((link) => link.hypothesis_id === hypothesis.hypothesis_id)
    .map((link) => ({ relation: link.relation, record: projection.evidence.get(link.evidence_id) }))
    .filter((linked): linked is NullCheckEvidence => linked.record !== undefined);

  return {
    hypothesis_id: hypothesis.hypothesis_id,
    statement: hypothesis.statement,
    narrative: projection.hunt.narrative,
    evidence,
  };
}

// Evidence may only ever accrue to the hypothesis it was gathered for unless the
// lead is made to rule on all of them, which is what confirmation drift is.
function validateCoverage(decision: Decision, projection: Projection): void {
  if (!projection.hunt.spec.hypothesis_loop) return;
  const covered = new Set((decision.evidence_relations ?? []).map((r) => pairKey(r.evidence_id, r.hypothesis_id)));
  const missing = unclassified(projection).filter((pair) => !covered.has(pairKey(pair.evidence_id, pair.hypothesis_id)));
  if (missing.length === 0) return;
  const [first] = missing;
  throw new InvalidDecision(
    `${decision.action} leaves ${missing.length} observation(s) unruled: evidence ${first!.evidence_id} ` +
      `against hypothesis ${first!.hypothesis_id}. Every active hypothesis needs supports, weakens or neither.`,
  );
}

// The controller rejects anything outside the closed vocabulary, so the Hunt
// Lead cannot widen its own action space by emitting a new verb or a worker
export function validateDecision(decision: Decision, projection: Projection): void {
  // Widened for the membership test only: STALLED is a DecisionAction the
  // controller writes but no lead may emit, so it must fail this check.
  if (!(DECISION_ACTIONS as readonly string[]).includes(decision.action)) {
    throw new InvalidDecision(`unknown action ${String(decision.action)}`);
  }

  const workers = projection.hunt.spec.roles.workers;
  const agentId = decision.worker_agent_id;
  if (agentId !== undefined && agentId !== null && !(agentId in workers)) {
    throw new InvalidDecision(`no such worker ${agentId}; the registry declares ${Object.keys(workers).sort().join(", ")}`);
  }

  const hypothesisId = decision.target_hypothesis_id;
  if (hypothesisId !== undefined && hypothesisId !== null && !projection.hypotheses.has(hypothesisId)) {
    throw new InvalidDecision(`no such hypothesis ${hypothesisId}`);
  }

  // Citations before focus: a decision resting on evidence that does not exist
  // is wrong about the data, which is worth saying before it is wrong about a verb.
  if (ACTIONS_REQUIRING_CITATION.has(decision.action)) {
    const citations = decision.evidence_citations ?? [];
    if (citations.length === 0) {
      throw new InvalidDecision(`${decision.action} must cite the evidence it rests on`);
    }
    const unknown = citations.filter((id) => !projection.evidence.has(id));
    if (unknown.length > 0) {
      throw new InvalidDecision(`${decision.action} cites unknown evidence: ${unknown.join(", ")}`);
    }
  }
  validateFocus(decision, projection);
  validateCoverage(decision, projection);
  if (decision.action === "ABANDON") validateAbandon(decision, projection);

  // A verdict is about one claim. Without the target there is nothing to argue
  // the null against, so this is as much a violation as an uncited citation.
  if (decision.action === "VALIDATE" && !decision.target_hypothesis_id) {
    throw new InvalidDecision("VALIDATE must name the target_hypothesis_id it puts up for a verdict");
  }
}

// Dropping a branch is the one decision an adversary most wants the hunt to make,
// and the evidence it would rest on is exactly what an adversary can write. So a
function validateAbandon(decision: Decision, projection: Projection): void {
  if (!decision.target_hypothesis_id && !decision.target_entity) {
    throw new InvalidDecision("ABANDON must name the hypothesis or entity it is dropping");
  }

  const cited = (decision.evidence_citations ?? [])
    .map((id) => projection.evidence.get(id))
    .filter((record): record is EvidenceRecord => record !== undefined);

  if (!cited.some((record) => sensorAttested(record) && !record.instruction_like)) {
    throw new InvalidDecision(
      "every record ABANDON cites rests only on content an adversary could have authored; " +
        "cite at least one resting on something the telemetry attested",
    );
  }
}

// DEEPEN keeps the current entity and hypothesis; PIVOT changes at least one.
// Without the graph the rule is unenforceable, and the two verbs collapse into
function validateFocus(decision: Decision, projection: Projection): void {
  const graph = buildEntityGraph([...projection.evidence.values()], projection.hunt.scope["entity"] as Entity);
  const target = decision.target_entity;

  if (target !== undefined && target !== null && graph.node(target) === undefined) {
    const known = graph.nodes().map((node) => key(node.entity)).sort();
    // Names what the field holds: a hypothesis id reads as a well-formed key, so
    // without it every retry repeats the refused attempt until the bound.
    throw new InvalidDecision(
      known.length === 0
        ? `target_entity names a thing evidence mentions -- a host, ip, user or hash -- not ${target}. ` +
            "No evidence has been gathered yet, so leave it unset until a worker has answered."
        : `no evidence mentions ${target}; the graph knows ${known.slice(0, 8).join(", ")}`,
    );
  }
  // An operator's known-benign call is an authorization, so it binds the Hunt
  // Lead rather than merely nudging it: the digest can drop a suppressed entity
  if (target !== undefined && target !== null && OPENS_WORK.has(decision.action)) {
    const actor = suppressedEntities(projection).get(target);
    if (actor !== undefined) {
      throw new InvalidDecision(
        `${actor} marked ${target} known-benign, so the hunt opens no new work on it. ` +
          "Pursue another entity, or say in your rationale why the suppression should be lifted.",
      );
    }
  }
  if (decision.action !== "DEEPEN" && decision.action !== "PIVOT") return;

  const focus = focusOf(projection);
  const held =
    (target ?? focus.entity) === focus.entity &&
    (decision.target_hypothesis_id ?? focus.hypothesis) === focus.hypothesis;

  if (decision.action === "DEEPEN" && !held) {
    throw new InvalidDecision("DEEPEN must keep the current entity and hypothesis; changing one is a PIVOT");
  }
  if (decision.action === "PIVOT" && held) {
    throw new InvalidDecision("PIVOT must change the entity or the hypothesis; keeping both is a DEEPEN");
  }
}

// The violation goes back to the Hunt Lead as a digest note, which is where the
// digest already carries controller-side observations, so the re-ask needs no
function withRejection(digest: Digest, reason: string): Digest {
  return {
    ...digest,
    notes: [
      ...digest.notes,
      `Your previous emission was rejected: ${reason}. Emit one decision from the closed vocabulary, citing only evidence ids present in this digest.`,
    ],
  };
}

export async function startHunt(
  state: State<HuntKinds>,
  queue: DirectiveQueue,
  runId: string,
  spec: HuntSpec,
  startedBy = "worker",
): Promise<Journal> {
  const now = new Date().toISOString();
  const huntId = newId("hunt");
  const policy = (spec.checkpoints ?? DEFAULT_CHECKPOINTS).hypothesis_approval;
  const ledger = await Journal.create(state, queue, runId, {
    hunt_id: huntId,
    name: spec.name,
    spec,
    seed: newId("seed", 8),
    // A hunt awaiting its start approval has not begun: no query runs, no money
    // is spent, and the checkpoint below is the only thing that releases it.
    status: policy === "ask" ? "pending_approval" : "active",
    outcome: null,
    iteration: 0,
    cost_usd: 0,
    budgets: spec.budgets,
    scope: spec.scope,
    // Journalled once, so a replay shows exactly what the lead was told and the
    // critic argues the null against the same context.
    narrative: narrativeOf(spec),
    created_at: now,
    terminated_at: null,
    parked_at: null,
    parked_reason: null,
    termination_reason: null,
  }, "hunt", {
    run_kind: "hunt",
    spec,
    budgets: spec.budgets,
    seed: runId,
    tenant_id: null,
    started_by: startedBy,
  });

  for (const statement of spec.hypotheses) {
    ledger.append({
      kind: "hypothesis",
      payload: {
        hypothesis_id: newId("h", 4),
        statement,
        status: "active",
        // attack_techniques is the vocabulary a citation is gated against, not a
        // per-hypothesis label: what a hypothesis is about is what its evidence cited.
        attack_technique: null,
        provenance: "hunt_spec",
        resolution_reason: null,
        evidence_strength: null,
      },
    });
  }

  // The caller's own claim, on the board as a peer of the definition's.
  for (const statement of spec.operator_hypotheses) {
    ledger.append({
      kind: "hypothesis",
      payload: {
        hypothesis_id: newId("h", 4),
        statement,
        status: "active",
        attack_technique: null,
        provenance: OPERATOR_HYPOTHESIS_PROVENANCE,
        resolution_reason: null,
        evidence_strength: null,
      },
    });
  }

  // The null on the board before anything is argued. Without it the benign
  // explanation is only ever an objection, never a competing claim.
  if (spec.hypothesis_loop) {
    ledger.append({
      kind: "hypothesis",
      payload: {
        hypothesis_id: newId("h", 4),
        statement: NULL_HYPOTHESIS,
        status: "active",
        attack_technique: null,
        provenance: BASE_RATE_PROVENANCE,
        resolution_reason: null,
        evidence_strength: null,
      },
    });
  }

  // Journalled once at start, so a missing capability reaches the report as a blind
  // spot the verdicts count. resumeHunt seeds nothing, so a handover cannot re-declare.
  const unbound = unboundCapabilities(spec.roles, spec.tools);
  for (const capability of unbound) {
    ledger.append({
      kind: "evidence",
      payload: {
        evidence_id: newId("ev"),
        dispatch_id: null,
        iteration: 0,
        source_system: DEPLOYMENT_SOURCE,
        summary: `no tool in this deployment answers ${capability}`,
        payload: { capability, hypothesis_id: null },
        salience: "notable",
        why_notable: "the roles that need it run without it, so no query can close this",
        provenance: DEPLOYMENT_GAP_PROVENANCE,
        attacker_influenceable: false,
        instruction_like: false,
        entities: [],
        captured_at: now,
      },
    });
  }

  // Raised whichever way the policy falls, so the approval is a ledger fact
  // rather than something a caller remembers. An ask with nothing pending deadlocks.
  const checkpoint = raiseCheckpoint(
    "hypothesis_approval",
    0,
    `Approve and start this hunt on ${spec.hypotheses.length + spec.operator_hypotheses.length} hypothesis(es)` +
      `${spec.operator_hypotheses.length > 0 ? `, ${spec.operator_hypotheses.length} from your request` : ""}?`,
    {
      hypotheses: [...ledger.projection.hypotheses.values()].map((hypothesis) => ({
        hypothesis_id: hypothesis.hypothesis_id,
        statement: hypothesis.statement,
      })),
      budgets: spec.budgets,
      scope: spec.scope,
      // What the operator is approving a hunt to run without.
      unbound_capabilities: unbound,
    },
  );
  ledger.append({ kind: "checkpoint", payload: checkpoint });
  if (policy === "auto") {
    const resolution = resolveCheckpoint(checkpoint, "approve", AUTO_ACTOR, "checkpoint policy hypothesis_approval=auto");
    ledger.append({ kind: "resolution", payload: resolution });
  }
  await ledger.flush();
  return ledger;
}

// The ledger is the resume point: the spec came with it, so nothing is re-read
// from disk and a mid-run edit to an arch file cannot change a hunt in flight.
export async function resumeHunt(
  state: State<HuntKinds>,
  queue: DirectiveQueue,
  runId: string,
): Promise<{ ledger: Journal; spec: HuntSpec }> {
  const ledger = await Journal.open(state, queue, runId);
  const { hunt } = ledger.projection;
  if (hunt.status === "terminal") throw new HuntAlreadyTerminal(`${hunt.hunt_id} already ended as ${hunt.outcome}`);
  return { ledger, spec: hunt.spec };
}

export class HuntController {
  constructor(
    private readonly ledger: Journal,
    private readonly provider: DecisionProvider,
    private readonly dispatcher?: WorkerDispatcher | undefined,
    private readonly policy: DispatchPolicy = DEFAULT_DISPATCH,
    private readonly digestPolicy: DigestPolicy = DEFAULT_DIGEST,
    private readonly enricher?: Enricher | undefined,
    // Optional: a hunt with no critic runs to a legible end, it just cannot
    // prove anything, and says so rather than transitioning quietly.
    private readonly critic?: DisconfirmationCritic | undefined,
    private readonly verdicts: Verdicts = DEFAULT_VERDICTS,
    // The harness's own ceiling. Optional so a scripted controller needs no pool,
    // and held only so an extension can widen what the pool refuses on.
    private readonly pool?: Budget | undefined,
  ) {}

  // Read from the journaled spec rather than passed in: the chains a hunt runs
  // were fixed when it started, and resume must not pick up an edited config.
  private get enrichment() {
    return { ...DEFAULT_ENRICHMENT, ...(this.ledger.projection.hunt.spec.enrichment ?? {}) };
  }

  // Read from the journaled spec for the same reason. Merged key by key, not taken
  // whole: a ledger predating a threshold would otherwise read it as a NaN ceiling.
  private get termination(): Termination {
    return { ...DEFAULT_TERMINATION, ...(this.ledger.projection.hunt.spec.termination ?? {}) };
  }

  // Read from the journaled spec for the same reason the others are: whether a
  // class stops and asks was settled when the hunt started, so an edited config
  private get checkpoints(): Checkpoints {
    return { ...DEFAULT_CHECKPOINTS, ...(this.ledger.projection.hunt.spec.checkpoints ?? {}) };
  }

  private raiseAsk(
    checkpointClass: CheckpointClass,
    raisedIteration: number,
    question: string,
    context: Record<string, unknown> = {},
  ): Checkpoint {
    const checkpoint = raiseCheckpoint(checkpointClass, raisedIteration, question, context);
    this.ledger.append({ kind: "checkpoint", payload: checkpoint });
    return checkpoint;
  }

  private resolve(
    checkpoint: Checkpoint,
    answer: Resolution["answer"],
    actor: string,
    text: string,
    directive: Directive | null = null,
  ): Resolution {
    const resolution = resolveCheckpoint(checkpoint, answer, actor, text, directive);
    this.ledger.append({ kind: "resolution", payload: resolution });
    return resolution;
  }

  async advanceIteration(): Promise<IterationResult> {
    if (this.ledger.projection.hunt.status === "terminal") {
      const hunt = this.ledger.projection.hunt;
      throw new HuntAlreadyTerminal(`${hunt.hunt_id} already ended as ${hunt.outcome}`);
    }

    // Human input is integrated at the boundary, before anything is decided on
    // it — and it is how a parked hunt is resolved, so it is drained first.
    if (await this.applyDirectives()) return this.halted();

    const suspended = this.ledger.projection.hunt;
    if (suspended.status === "parked" || suspended.status === "pending_approval") {
      throw new HuntParked(this.suspendedBecause());
    }

    const projection = this.ledger.projection;
    const iteration = projection.hunt.iteration + 1;
    // Captured before the digest, not after the decision: dispatches are
    // journaled while the lead is still deciding, so only this prefix is what it saw.
    const digestSeq = this.ledger.log.length;
    const digest = buildDigest(projection, iteration, this.digestPolicy);

    const { presented, result } = await this.decide(digest, projection, digestSeq);

    const dispatchResults = await this.runDispatches(iteration, result.decision);
    const nullCheck = await this.runNullCheck(result.decision);
    return await this.write(iteration, digestSeq, presented, result, dispatchResults, nullCheck);
  }

  // A rejection is a correctable mistake, not a lost iteration: the Hunt Lead is
  // told what was wrong and asked again, boundedly. The digest returned is the
  private async decide(
    digest: Digest,
    projection: Projection,
    digestSeq: number,
  ): Promise<{ presented: Digest; result: DecisionResult }> {
    // Schema-level rejections from inside the provider and controller-level ones
    // from here are the same audit fact, so they merge into one list in order.
    const rejected: string[] = [];
    // A rejected emission was still paid for. Charging only the accepted one
    // would under-report spend by up to the attempt bound, which both hides
    let spent = 0;
    let presented = digest;
    let attempts = 0;
    let expansions = 0;
    // Carried out of the loop so a stall names the model and prompt that failed
    // rather than what the spec merely asked for.
    let attribution = { model_id: projection.hunt.spec.model, prompt_version: "" };

    const watch = this.watchForAbort();
    try {
    while (attempts < MAX_DECISION_ATTEMPTS) {
      let result: DecisionResult;
      try {
        result = await this.provider.decide(presented, watch.signal);
      } catch (error) {
        // A dead call has not decided this iteration, so it takes the same bounded
        // re-ask a schema-invalid emission gets rather than ending the run.
        // Exhausted budgets are the exception: the next call answers identically.
        if (error instanceof BudgetRefused || error instanceof GatewayExhausted) throw error;
        // A park is not a dead call — every re-ask folds the same ledger and refuses
        // again — so it surfaces as HuntParked and the run stays answerable.
        if (error instanceof LeadParked) throw new HuntParked(error.message);
        if (watch.signal.aborted) throw error;
        attempts += 1;
        spent += spentBefore(error);
        rejected.push(error instanceof Error ? error.message : String(error));
        continue;
      }
      rejected.push(...(result.rejected_attempts ?? []));
      spent += result.cost_usd;
      attribution = { model_id: result.model_id, prompt_version: result.prompt_version };

      try {
        validateDecision(result.decision, projection);
      } catch (error) {
        if (!(error instanceof InvalidDecision)) throw error;
        attempts += 1;
        rejected.push(error.message);
        presented = withRejection(presented, error.message);
        continue;
      }

      // EXPAND is a read, not a move: it buys raw payloads and asks again without
      // advancing the iteration. Cost still accrues, so it is not free, only
      if (result.decision.action === "EXPAND") {
        if (expansions < MAX_EXPANSIONS) {
          expansions += 1;
          presented = this.expand(presented, result.decision.evidence_citations ?? []);
          continue;
        }
        attempts += 1;
        const exhausted = `all ${MAX_EXPANSIONS} expansions are used; decide on what you have`;
        rejected.push(exhausted);
        presented = withRejection(presented, exhausted);
        continue;
      }

      // Left absent rather than empty when nothing was rejected, so a clean
      // iteration journals exactly what it did before.
      return {
        presented,
        result: {
          ...result,
          cost_usd: spent,
          ...(rejected.length > 0 ? { rejected_attempts: rejected } : {}),
        },
      };
    }

    // A stalled iteration is a fact about the hunt, not an absence of one: it
    // presented a digest and was billed for emissions. Journaling it before the
    this.recordStall(presented, digestSeq, rejected, spent, attribution);

    throw new InvalidDecision(
      `the Hunt Lead emitted nothing valid in ${MAX_DECISION_ATTEMPTS} attempts ` +
        `($${spent.toFixed(4)} spent): ${rejected.join(" | ")}`,
    );
    } finally {
      watch.stop();
    }
  }

  // Reuses the decision event rather than adding a kind of its own: what that
  // record means is "a digest was presented and paid for", which is exactly what
  private recordStall(
    presented: Digest,
    digestSeq: number,
    rejected: readonly string[],
    spent: number,
    attribution: { model_id: string; prompt_version: string },
  ): void {
    this.ledger.append({
      kind: "decision",
      payload: {
        ...attribution,
        decision: {
          action: "STALLED",
          rationale: `no valid decision in ${MAX_DECISION_ATTEMPTS} attempts`,
          target_entity: null,
          target_hypothesis_id: null,
        },
        decision_id: newId("dec"),
        iteration: presented.iteration,
        digest_presented: presented,
        digest_seq: digestSeq,
        cost_usd: spent,
        rejected_attempts: [...rejected],
        created_at: new Date().toISOString(),
      },
    });

    const hunt = this.ledger.projection.hunt;
    this.ledger.patch("hunt", hunt.hunt_id, {
      cost_usd: Number((hunt.cost_usd + spent).toFixed(6)),
    });
    // The iteration counter deliberately does not advance: a resume retries this
    // iteration. So only the cost arm of the budget can newly trip here.
    if (this.budgetExhausted()) this.terminate("budget_terminated");
  }

  // Whole records are dropped at the budget rather than one being cut mid-JSON,
  // and what was dropped is named so the lead can ask for less next time.
  private expand(digest: Digest, ids: readonly string[]): Digest {
    const expansions: Expansion[] = [];
    const dropped: string[] = [];
    let budget = EXPANSION_BUDGET;

    for (const evidenceId of ids) {
      const record = this.ledger.projection.evidence.get(evidenceId);
      if (record === undefined) continue;
      const payload = JSON.stringify(record.payload, null, 2);
      if (payload.length > budget) {
        dropped.push(evidenceId);
        continue;
      }
      budget -= payload.length;
      expansions.push({ evidence_id: evidenceId, payload });
    }

    const notes = dropped.length === 0 ? digest.notes : [...digest.notes, `Too large to expand: ${dropped.join(", ")}.`];
    return { ...digest, expansions: [...digest.expansions, ...expansions], notes };
  }

  // Returns true when the hunt ended here. A lead becomes a real lead; a note
  // only reaches the digest, so it steers without mutating anything; extend and
  private async applyDirectives(): Promise<boolean> {
    let abort = false;
    let conclude = false;

    // Everything pending is journaled by the drain before any of it is applied,
    // so an operator's input is on the record even when an earlier directive in
    for (const directive of await drain(this.ledger)) {
      if (this.ledger.projection.hunt.status === "terminal") break;
      switch (directive.kind) {
        case "abort":
          abort = true;
          break;
        case "conclude":
          conclude = true;
          break;
        case "lead":
          this.applyLead(directive);
          break;
        case "extend":
          this.extend(directive);
          break;
        case "approve":
        case "reject":
          this.applyResolution(directive);
          break;
        case "benign":
          this.applyBenign(directive);
          break;
        case "gap":
          this.applyGap(directive);
          break;
        case "boost":
          this.applyBoost(directive);
          break;
        case "note":
          break;
      }
    }
    // Durable before anything is decided on it: the lead folds the *stored* ledger to
    // see whether the run is answerable, so a buffered approval reads as no approval.
    await this.ledger.flush();

    if (this.ledger.projection.hunt.status === "terminal") return true;

    if (abort) {
      this.terminate("aborted", "an operator halted the hunt");
      return true;
    }
    // Two endings, not one: at a ceiling the operator accepted the stop, away from
    // one they asked for a verdict on what was in hand.
    if (conclude) {
      const bound = boundBy(this.ledger.projection.hunt);
      if (bound !== null) {
        this.terminate("budget_terminated", "an operator accepted the stop at the budget checkpoint");
      } else {
        this.terminate("completed", `an operator asked the hunt to conclude on what it had: ${standingOf(this.ledger.projection.hunt)}`);
      }
      return true;
    }
    // After the drain, not before: an answer already waiting in the inbox is an
    // operator who did respond, however late the process got around to reading it.
    return this.expireParked();
  }

  // Marking an entity known-benign appends a suppression; lifting it appends the
  // reversal. Neither touches a record: the evidence that mentions the entity
  private applyBenign(directive: Directive): void {
    const entityKey = directive.entity_key ?? directive.text.trim();
    if (entityKey === "") {
      journalNote(this.ledger, `${directive.actor} sent a benign directive naming no entity; nothing was suppressed.`);
      return;
    }
    journalNote(
      this.ledger,
      directive.revoke === true
        ? `${directive.actor} lifted the known-benign suppression on ${entityKey}; it is back in play.`
        : `${directive.actor} marked ${entityKey} known-benign. Its evidence stands; the hunt opens no new work on it.`,
    );
  }

  // A blind spot no query would ever return. Journaled as a gap record so it
  // counts exactly like a tool failure — including gap-locking a hypothesis it
  private applyGap(directive: Directive): void {
    const hypothesisId =
      directive.hypothesis_id !== undefined && this.ledger.projection.hypotheses.has(directive.hypothesis_id)
        ? directive.hypothesis_id
        : null;

    this.appendEvidence(
      [
        {
          source_system: "operator",
          summary: `operator-declared visibility gap: ${directive.text}`,
          payload: { hypothesis_id: hypothesisId, actor: directive.actor, directive_id: directive.directive_id },
          salience: "notable",
          why_notable: "the hunt cannot see here, and no query will tell it so",
          provenance: OPERATOR_GAP_PROVENANCE,
          attacker_influenceable: false,
          instruction_like: false,
        },
      ],
      this.ledger.projection.hunt.iteration,
      null,
    );
  }

  private applyBoost(directive: Directive): void {
    const questionId = directive.question_id ?? "";
    const question = this.ledger.projection.questions.get(questionId);
    if (question === undefined) {
      journalNote(this.ledger, `${directive.actor} boosted ${questionId || "(no question named)"}, which is not on the frontier.`);
      return;
    }
    journalNote(this.ledger, `${directive.actor} pinned ${questionId} to the top of the frontier: ${question.question}`);
  }

  // A lead is the one directive that can grow what the hunt looks at, so it is
  // where scope is consulted. A tenant crossing is refused outright — a boundary
  private applyLead(directive: Directive): void {
    const iteration = this.ledger.projection.hunt.iteration + 1;
    if (this.refuseCrossTenant(directive)) return;

    const named = directive.entity_key ?? fromText(directive.text).map(key)[0] ?? null;
    const outside = this.outsideDeclared(named);
    if (outside !== null && this.growScope(outside, directive.text, iteration, directive.actor)) return;

    this.raise(directive.text, {
      entity_key: directive.entity_key ?? outside ?? null,
      spawned_iteration: iteration,
    });
  }

  // The one gate on growth, whoever asked for it. Returns true when the caller
  // must stop: the hunt is parked on the question, and the lead is raised on
  private growScope(entityKey: string, leadText: string, iteration: number, actor: string): boolean {
    const { checkpoint, parked } = this.ask(
      "scope_extension",
      iteration,
      `Extend this hunt's scope to ${entityKey}?`,
      { question: leadText, entity_key: entityKey, actor },
    );
    if (parked) return true;

    this.autoResolved(checkpoint, "checkpoint policy scope_extension=auto");
    this.extendScope(entityKey);
    return false;
  }

  private refuseCrossTenant(directive: Directive): boolean {
    const declared = this.ledger.projection.hunt.scope["tenant"];
    if (typeof declared !== "string" || declared === "") return false;

    // The typed field first; the regex is the fallback for a directive someone
    // appended to the inbox by hand rather than through the CLI.
    const named = directive.tenant ?? /\btenant[:=]\s*([^\s,;]+)/i.exec(directive.text)?.[1];
    if (named === undefined || named.toLowerCase() === declared.toLowerCase()) return false;

    journalNote(
      this.ledger,
      `${directive.actor}'s lead names tenant ${named}, and this hunt is scoped to ${declared}. ` +
        "Refused outright rather than raised as a checkpoint: a tenant boundary is not one an operator may waive " +
        "from inside the hunt. Start a hunt in that tenant instead. The hunt continues.",
    );
    return true;
  }

  // Only meaningful when the playbook declared a scope.entities list: with no
  // declared boundary nothing is outside it, which is what keeps a plain hunt
  private outsideDeclared(entityKey: string | null): string | null {
    if (entityKey === null) return null;
    const declared = new Set((this.ledger.projection.hunt.scope["entities"] as string[] | undefined) ?? []);
    return declared.size > 0 && !declared.has(entityKey) ? entityKey : null;
  }

  // Why the hunt will not step. The budget park keeps 08's wording, because its
  // three answers are extend, conclude and abort; a raised checkpoint takes two
  private suspendedBecause(): string {
    const hunt = this.ledger.projection.hunt;
    const pending = pendingCheckpoints(this.ledger.projection);

    if (pending.length > 0) {
      // Every one of them, not just the first: a hunt released on one answer
      // while another is outstanding would step with a question still open.
      return (
        `${hunt.hunt_id} is waiting on ${pending.length} checkpoint(s). ` +
        pending
          .map((checkpoint) => `${checkpoint.checkpoint_class} ${checkpoint.checkpoint_id}: ${checkpoint.question}`)
          .join(" | ") +
        ` Answer each with a directive — approve <id>, or reject <id> with a reason.`
      );
    }
    return (
      `${hunt.hunt_id} is parked: ${hunt.parked_reason ?? "awaiting an operator"}. ` +
      "Resolve it with a directive — extend (grant +N iterations or +$N), conclude (accept the stop), or abort."
    );
  }

  // Raise, and stop if the policy says a human owns this one. The active-time
  // clock pauses for free: a parked hunt advances no iteration and spends
  private ask(
    checkpointClass: CheckpointClass,
    iteration: number,
    question: string,
    payload: Record<string, unknown>,
  ): { checkpoint: Checkpoint; parked: boolean } {
    const checkpoint = this.raiseAsk(checkpointClass, iteration, question, payload);
    if (this.checkpoints[checkpointClass] !== "ask") return { checkpoint, parked: false };

    const hunt = this.ledger.projection.hunt;
    this.ledger.patch("hunt", hunt.hunt_id, {
      status: "parked",
      parked_at: new Date().toISOString(),
      // Prefixed, so unpark can tell a checkpoint park from the budget park it
      // must never lift on an approval.
      parked_reason: `${AWAITING}${checkpointClass} checkpoint ${checkpoint.checkpoint_id}: ${question}`,
    });
    return { checkpoint, parked: true };
  }

  private autoResolved(checkpoint: Checkpoint, reason: string): void {
    this.resolve(checkpoint, "approve", AUTO_ACTOR, reason);
  }

  // Lifted only when nothing is left to answer. Keying on the checkpoint id in
  // parked_reason would let an answer to one question release a hunt still
  private unpark(): void {
    const hunt = this.ledger.projection.hunt;
    if (hunt.status !== "parked" && hunt.status !== "pending_approval") return;
    if (hunt.status === "parked" && !(hunt.parked_reason ?? "").startsWith(AWAITING)) return;
    if (pendingCheckpoints(this.ledger.projection).length > 0) return;
    this.ledger.patch("hunt", hunt.hunt_id, { status: "active", parked_at: null, parked_reason: null });
  }

  // An operator's answer to a raised question. The checkpoint is the authority:
  // an answer to one that does not exist, or to one already settled, is journaled
  private applyResolution(directive: Directive): void {
    const projection = this.ledger.projection;
    const checkpoint = projection.checkpoints.get(directive.checkpoint_id ?? "");
    if (checkpoint === undefined) {
      journalNote(
        this.ledger,
        `${directive.actor} answered checkpoint ${directive.checkpoint_id ?? "(none named)"}, which this hunt never raised.`,
      );
      return;
    }

    const settled = resolutionOf(projection, checkpoint.checkpoint_id);
    if (settled !== undefined) {
      journalNote(
        this.ledger,
        `${directive.actor} answered ${checkpoint.checkpoint_id}, already ${settled.answer} by ${settled.actor}. ` +
          "The first answer stands; reversing a decision is its own directive.",
      );
      return;
    }

    const approved = directive.kind === "approve";
    this.resolve(
      checkpoint,
      approved ? "approve" : "reject",
      directive.actor,
      directive.text,
      directive,
    );

    switch (checkpoint.checkpoint_class) {
      case "hypothesis_approval":
        this.resolveStart(checkpoint, directive, approved);
        return;
      case "verdict_review":
        this.resolveVerdict(checkpoint, directive, approved);
        return;
      case "scope_extension":
        this.resolveScope(checkpoint, directive, approved);
        return;
      case "budget_anomaly":
        // The Hunt Lead asked for an adult. Either answer releases it — an
        // operator who wants the hunt stopped says so with abort or conclude.
        journalNote(
          this.ledger,
          approved
            ? `${directive.actor} acknowledged the Hunt Lead's checkpoint: ${directive.text || "carry on"}.`
            : `${directive.actor} did not accept the Hunt Lead's concern: ${directive.text || "carry on"}. ` +
                "The hunt continues; take it into account.",
        );
        this.unpark();
        return;
    }
  }

  private resolveStart(checkpoint: Checkpoint, directive: Directive, approved: boolean): void {
    if (approved) {
      this.unpark();
      return;
    }
    // Through terminate() like every other ending, so a hunt that was never
    // allowed to start still finalizes: the report is a header and the
    this.terminate("aborted", `${directive.actor} rejected the hypotheses at the start checkpoint: ${directive.text}`);
  }

  // The one place an approval reaches a hypothesis, and it applies the patch
  // applyVerdict computed at VALIDATE time. Not a second path to proven: the
  private resolveVerdict(checkpoint: Checkpoint, directive: Directive, approved: boolean): void {
    const payload = (checkpoint.context ?? {});

    if (payload["kind"] === "conclude") {
      if (approved) {
        for (const questionId of (payload["park"] as string[] | undefined) ?? []) {
          this.ledger.patch("question", questionId, {
            status: "parked",
            closed_reason: `parked to the backlog: below the priority floor of ${this.termination.priority_floor} when the hunt ended`,
          });
        }
        this.terminate(
          payload["outcome"] as HuntOutcome,
          `${directive.actor} approved the conclusion at checkpoint ${checkpoint.checkpoint_id}`,
        );
        return;
      }
      journalNote(
        this.ledger,
        `${directive.actor} refused the conclusion at ${checkpoint.checkpoint_id}: ${directive.text || "no reason given"}. ` +
          "The hunt continues; resolve what they raised before recommending CONCLUDE again.",
      );
      this.unpark();
      return;
    }

    const hypothesisId = String(payload["hypothesis_id"] ?? "");
    if (approved) {
      this.applyReviewedVerdict(checkpoint, directive, hypothesisId);
    } else {
      // Stays active, and the reason reaches the next digest — a rejected
      // verdict is information the Hunt Lead needs, not a silent no.
      journalNote(
        this.ledger,
        `${directive.actor} rejected the verdict on ${hypothesisId}: ${directive.text || "no reason given"}. ` +
          "It stays active; the evidence that would answer them is what to go after.",
      );
    }
    this.unpark();
  }

  // A verdict waits on a human, and the ledger can move while it waits: an
  // operator may declare a visibility gap and then approve, answering a question
  private applyReviewedVerdict(checkpoint: Checkpoint, directive: Directive, hypothesisId: string): void {
    const strength = evidenceStrength(this.ledger.projection, hypothesisId);

    // Gap-locked while it waited. Closed the way applyVerdict would close it —
    // inconclusive, on the numbers as they now stand — because not having been
    if (strength.open_gaps >= this.verdicts.gap_lock_threshold) {
      this.ledger.patch("hypothesis", hypothesisId, {
        status: "inconclusive",
        resolution_reason:
          `gap-locked before the approved verdict landed: ${strength.open_gaps} open visibility gap(s) ` +
          "bear on this hypothesis, so the hunt could not look rather than having cleared it",
        evidence_strength: strength,
      });
      journalNote(
        this.ledger,
        `${directive.actor} approved the verdict on ${hypothesisId}, but ${strength.open_gaps} open gap(s) now ` +
          "bear on it. It closes inconclusive rather than proven — the approval stands, the evidence moved.",
      );
      return;
    }

    const unmet = unmetPredicates(strength, this.verdicts);
    if (unmet.length > 0) {
      journalNote(
        this.ledger,
        `${directive.actor} approved the verdict on ${hypothesisId} at ${checkpoint.checkpoint_id}, but the ` +
          `evidence moved while it waited and no longer carries it: ${unmet.join("; ")}. It stays active — ` +
          "VALIDATE it again on the record as it now stands.",
      );
      return;
    }

    this.ledger.patch("hypothesis", hypothesisId, (checkpoint.context ?? {})["patch"] as Record<string, unknown>);
    journalNote(
      this.ledger,
      `${directive.actor} approved the verdict on ${hypothesisId} at ${checkpoint.checkpoint_id}.`,
    );
  }

  private resolveScope(checkpoint: Checkpoint, directive: Directive, approved: boolean): void {
    const question = String((checkpoint.context ?? {})["question"] ?? "");
    const entityKey = ((checkpoint.context ?? {})["entity_key"] as string | null) ?? null;

    if (!approved) {
      journalNote(
        this.ledger,
        `${directive.actor} refused the scope extension to ${entityKey ?? question}: ${directive.text || "no reason given"}.`,
      );
      this.unpark();
      return;
    }

    this.extendScope(entityKey);
    this.raise(question, {
      entity_key: entityKey,
      spawned_iteration: this.ledger.projection.hunt.iteration + 1,
    });
    journalNote(
      this.ledger,
      `${directive.actor} extended the hunt's scope to ${entityKey ?? question} at ${checkpoint.checkpoint_id}.`,
    );
    this.unpark();
  }

  // The declared scope grows by an append like everything else, so what the hunt
  // was authorised to look at is readable at any point in its history.
  private extendScope(entityKey: string | null): void {
    if (entityKey === null) return;
    const hunt = this.ledger.projection.hunt;
    const declared = (hunt.scope["entities"] as string[] | undefined) ?? [];
    if (declared.includes(entityKey)) return;
    this.ledger.patch("hunt", hunt.hunt_id, { scope: { ...hunt.scope, entities: [...declared, entityKey] } });
  }

  // Asks the predicate the same question CONCLUDE asks, before parking. A hunt
  // with nothing active and a cleared frontier is finished; parking it would put
  private budgetCheckpoint(iteration: number): string {
    const verdict = terminationVerdict(this.ledger.projection, iteration, this.termination, this.verdicts);
    if (verdict.outcome === null) return this.park();

    this.concludeWith(verdict, `the termination predicate passed as the budget ran out at iteration ${iteration}`);
    return `concluded as ${verdict.outcome} on the last of the budget`;
  }

  // The harness refused a call on a ceiling the hunt's predicate does not read — the
  // wall clock — so it parks on the same three answers every other ceiling parks on.
  parkOnRefusal(reason: string): string {
    const hunt = this.ledger.projection.hunt;
    if (hunt.status !== "active") return this.suspendedBecause();
    this.ledger.patch("hunt", hunt.hunt_id, {
      status: "parked",
      parked_at: new Date().toISOString(),
      parked_reason: reason,
    });
    return `parked: ${reason} — extend, conclude or abort`;
  }

  // The budget checkpoint. The hunt stops spending and waits: extend, conclude or
  // abort. Parked rather than terminated, because "the money ran out" is a
  private park(): string {
    const hunt = this.ledger.projection.hunt;
    const reason = boundReason(hunt);

    // Not a checkpoint, though it is a question: Run.settled() reads an unresolved one
    // as "no call may proceed", which no extension could then lift. parked_reason holds it.
    this.ledger.patch("hunt", hunt.hunt_id, {
      status: "parked",
      parked_at: new Date().toISOString(),
      parked_reason: reason,
    });
    return `parked: ${reason} — extend, conclude or abort`;
  }

  // Raises the budgets, capped by the hard per-hunt ceiling, and un-parks only if
  // the grant actually bought a turn: an extension clamped down to what the hunt
  private extend(directive: Directive): void {
    const hunt = this.ledger.projection.hunt;
    const grant = grantOf(directive);

    if (grant.iterations <= 0 && grant.cost_usd <= 0 && grant.wall_ms <= 0) {
      journalNote(
        this.ledger,
        `extend "${directive.text}" granted nothing the controller could read; ` +
          "say how many iterations, how many dollars or how many minutes " +
          "(e.g. \"+5 iterations\", \"+$10\", \"+30 minutes\").",
      );
      return;
    }

    // An extension buys iterations, dollars and wall clock. How long the hunt may
    // sit parked is not on offer and carries over untouched.
    const asked: Budgets = {
      max_iterations: hunt.budgets.max_iterations + grant.iterations,
      max_calls: (hunt.budgets.max_iterations + grant.iterations) * CALLS_PER_ITERATION,
      max_cost_usd: Number((hunt.budgets.max_cost_usd + grant.cost_usd).toFixed(6)),
      max_wall_ms: hunt.budgets.max_wall_ms + grant.wall_ms,
      max_park_ms: hunt.budgets.max_park_ms,
    };
    const { hard_max_iterations, hard_max_calls, hard_max_cost_usd, hard_max_wall_ms } = this.termination;
    const budgets: Budgets = {
      max_iterations: Math.min(asked.max_iterations, hard_max_iterations),
      max_calls: Math.min(asked.max_calls, hard_max_calls),
      max_wall_ms: Math.min(asked.max_wall_ms, hard_max_wall_ms),
      max_cost_usd: Math.min(asked.max_cost_usd, hard_max_cost_usd),
      max_park_ms: hunt.budgets.max_park_ms,
    };
    this.ledger.patch("hunt", hunt.hunt_id, { budgets });
    // The pool enforces the wall and the call meter and was built from the spec, so an
    // extension it is not told about buys nothing.
    this.pool?.raise(budgets);

    if (
      budgets.max_iterations < asked.max_iterations ||
      budgets.max_cost_usd < asked.max_cost_usd ||
      budgets.max_wall_ms < asked.max_wall_ms
    ) {
      journalNote(
        this.ledger,
        `${directive.actor} extended the hunt to ${asked.max_iterations} iterations / ` +
          `$${asked.max_cost_usd.toFixed(2)} / ${Math.round(asked.max_wall_ms / 60_000)} minutes; clamped to the hard ` +
          `ceiling of ${hard_max_iterations} iterations / $${hard_max_cost_usd.toFixed(2)} / ` +
          `${Math.round(hard_max_wall_ms / 60_000)} minutes.`,
      );
    }

    if (this.budgetExhausted()) {
      journalNote(
        this.ledger,
        `the extension leaves no room at ${budgets.max_iterations} iterations / $${budgets.max_cost_usd.toFixed(2)}, ` +
          "so the hunt stays parked; conclude or abort it.",
      );
      return;
    }
    this.ledger.patch("hunt", hunt.hunt_id, { status: "active", parked_at: null, parked_reason: null });
  }

  // Lazy expiry: no timers and no daemon in a single-process app, so the TTL is
  // enforced wherever the hunt is next touched. A hunt nobody answered for a week
  private expireParked(): boolean {
    const hunt = this.ledger.projection.hunt;
    if (hunt.status !== "parked" || !hunt.parked_at) return false;

    const parkedFor = Date.now() - Date.parse(hunt.parked_at);
    const ttl = this.termination.park_ttl_ms;
    if (!(parkedFor >= ttl)) return false;

    this.terminate(
      "aborted",
      `parked since ${hunt.parked_at} with no operator decision, past the ${Math.round(ttl / 86_400_000)}-day park TTL`,
    );
    return true;
  }

  // What a boundary-ended iteration reports: nothing was decided and nothing was
  // spent, so the only news is the state the hunt landed in.
  private halted(): IterationResult {
    const hunt = this.ledger.projection.hunt;
    return {
      hunt_id: hunt.hunt_id,
      iteration: hunt.iteration,
      action: "CONCLUDE",
      decision_id: "",
      cost_usd: 0,
      evidence_appended: 0,
      enriched: 0,
      hunt_status: hunt.status,
      hunt_outcome: hunt.outcome,
      note: hunt.termination_reason ?? `ended ${hunt.outcome ?? "at the iteration boundary"}`,
    };
  }

  // A crash between journaling a dispatch and recording its result leaves a lead
  // closed but unanswered. Reaping hands it back and records the gap.
  reap(): number {
    const stale = [...this.ledger.projection.dispatches.values()].filter(
      (dispatch) => dispatch.status === "pending",
    );
    for (const dispatch of stale) {
      this.persistDispatch(dispatch.iteration, {
        dispatch_id: dispatch.dispatch_id,
        evidence: [],
        failed: true,
        failure_reason: "interrupted before the worker returned",
        // Whatever the interrupted worker spent went with it; nothing is known.
        cost_usd: 0,
      });
      if (dispatch.question_id !== null) {
        this.ledger.patch("question", dispatch.question_id, { status: "open" });
      }
    }
    return stale.length;
  }

  // One worker per open lead, capped. Serial is simply max_workers of 1, so
  // there is no second code path for it.
  private fanOut(decision: Decision): FanOutTarget[] {
    const held = decision.target_entity ?? focusOf(this.ledger.projection).entity;
    const scoped = (text: string, entityKey: string | null) =>
      entityKey === null ? text : `${text} [entity ${entityKey}]`;

    const fallback: FanOutTarget[] = [
      {
        focus: scoped("", held ?? null).trim(),
        hypothesisId: decision.target_hypothesis_id ?? null,
        questionId: null,
      },
    ];
    if (this.policy.max_workers === 1) return fallback;

    const projection = this.ledger.projection;
    const targets: FanOutTarget[] =
      this.policy.fan_out_over === "questions"
        ? rankFrontier(projection, projection.hunt.iteration + 1)
            .map((question) => ({
              // A lead carries the entity it is about, so the worker is told what
              // to look at rather than inferring it from prose.
              focus: scoped(question.question, question.entity_key),
              // The lead carries the hypothesis it was opened for, so a worker
              // that fails leaves a gap attributed to what it was serving.
              hypothesisId: question.hypothesis_id,
              questionId: question.question_id,
            }))
        : [...projection.hypotheses.values()]
            .filter((hypothesis) => hypothesis.status === "active")
            .map((hypothesis) => ({
              focus: hypothesis.statement,
              hypothesisId: hypothesis.hypothesis_id,
              questionId: null,
            }));

    return targets.length === 0 ? fallback : targets.slice(0, this.policy.max_workers);
  }

  // One appender for every lead, so the priority features are always populated:
  // a lead with no provenance is a lead the frontier cannot rank.
  private raise(question: string, provenance: Partial<Omit<OpenQuestion, "question_id" | "question" | "status">>): void {
    this.ledger.append({
      kind: "question",
      payload: {
        question_id: newId("q", 4),
        question,
        status: "open",
        entity_key: null,
        spawning_evidence_id: null,
        spawning_dispatch_id: null,
        spawned_iteration: 0,
        hypothesis_id: null,
        ...provenance,
      },
    });
  }

  // PIVOT is a move of attention, not a query: it puts its new target on the
  // frontier and lets the next INVESTIGATE pick it up from there.
  private pivot(iteration: number, decision: Decision): void {
    const target = decision.target_entity ?? decision.target_hypothesis_id ?? "";
    const question = decision.query_intent || `pursue ${target}: ${decision.rationale}`;

    // The Hunt Lead stands inside the same wall an operator does. A pivot onto
    // an entity this hunt was never authorised to look at is scope growth
    const outside = this.outsideDeclared(decision.target_entity ?? null);
    if (outside !== null && this.growScope(outside, question, iteration, "hunt_lead")) return;

    this.raise(question, {
      entity_key: decision.target_entity ?? null,
      spawning_evidence_id: decision.evidence_citations?.[0] ?? null,
      spawned_iteration: iteration,
    });
  }

  // Parked rather than disproven: the hunt stopped looking, which is not the same
  // as having cleared the branch. validateAbandon has already established that
  private abandon(decision: Decision): void {
    const reason = `abandoned at the Hunt Lead's decision: ${decision.rationale} [${(decision.evidence_citations ?? []).join(", ")}]`;
    if (decision.target_hypothesis_id) {
      this.ledger.patch("hypothesis", decision.target_hypothesis_id, { status: "parked", resolution_reason: reason });
    }

    for (const question of this.ledger.projection.questions.values()) {
      if (question.status === "open" && question.entity_key !== null && question.entity_key === decision.target_entity) {
        this.ledger.patch("question", question.question_id, { status: "closed" });
      }
    }
  }

  // DEEPEN dispatches like INVESTIGATE; validateFocus has already established
  // that it kept the focus, so the only difference is what the worker is told.
  private async runDispatches(iteration: number, decision: Decision): Promise<DispatchResult[]> {
    if (decision.action === "PIVOT") {
      this.pivot(iteration, decision);
      return [];
    }
    if (decision.action === "ABANDON") {
      this.abandon(decision);
      return [];
    }
    const dispatches = decision.action === "INVESTIGATE" || decision.action === "DEEPEN";
    if (!dispatches || this.dispatcher === undefined) return [];
    const dispatcher = this.dispatcher;

    const targets = this.fanOut(decision);
    const requests = targets.map(({ focus, hypothesisId }) => ({
      dispatch_id: newId("dsp"),
      hunt_id: this.ledger.projection.hunt.hunt_id,
      agent_id: decision.worker_agent_id ?? DEFAULT_WORKER_AGENT_ID,
      query_intent: decision.query_intent || decision.rationale,
      focus,
      target_hypothesis_id: hypothesisId,
      scope: this.ledger.projection.hunt.scope,
    })) satisfies DispatchRequest[];

    // Closed once taken, not once answered: a lead left open would be re-issued
    // every iteration, and a failed one is already recorded as a visibility gap.
    for (const [index, request] of requests.entries()) {
      const questionId = targets[index]?.questionId ?? null;
      if (questionId !== null) this.ledger.patch("question", questionId, { status: "closed" });
      this.ledger.append({
        kind: "dispatch",
        payload: {
          dispatch_id: request.dispatch_id,
          iteration,
          agent_id: request.agent_id,
          status: "pending",
          query_intent: request.focus ? `${request.query_intent} — ${request.focus}` : request.query_intent,
          target_hypothesis_id: request.target_hypothesis_id,
          question_id: questionId,
          failure_reason: null,
          cost_usd: 0,
          calls: [],
        },
      });
    }

    // One signal for the iteration, cancelled the moment an abort appears in the
    // inbox: a worker that has not started is skipped, and one already running is
    const halt = new AbortController();
    // The check is a query, so a slow one must not stack ticks behind it: at most
    // one in flight, and a tick arriving during it is dropped rather than queued.
    let checking = false;
    const poll = setInterval(() => {
      if (checking) return;
      checking = true;
      void this.abortQueued()
        .then((queued) => {
          if (queued) halt.abort(new Error(CANCELLED_ON_ABORT));
        })
        .finally(() => {
          checking = false;
        });
    }, ABORT_POLL_MS);
    // The timer must not be what keeps the process alive once the work is done.
    poll.unref?.();

    // Started in order rather than all at once, with the inbox read before each
    // one and a turn of the event loop between them: a worker that has already
    const started: Promise<DispatchResult>[] = [];
    try {
      for (const request of requests) {
        if (halt.signal.aborted || (await this.abortQueued())) {
          started.push(
            Promise.resolve({
              dispatch_id: request.dispatch_id,
              evidence: [],
              failed: true,
              failure_reason: SKIPPED_ON_ABORT,
              cost_usd: 0,
            }),
          );
          continue;
        }
        started.push(
          (async () => {
            try {
              return await dispatcher.dispatch({ ...request, signal: halt.signal });
            } catch (error) {
              return {
                dispatch_id: request.dispatch_id,
                evidence: [],
                failed: true,
                // A cancelled worker reports what actually stopped it, not
                // whatever wording the client threw on the way down.
                failure_reason: halt.signal.aborted ? CANCELLED_ON_ABORT : (error as Error).message,
                cost_usd: spentBefore(error),
              };
            }
          })(),
        );
        await new Promise((resolve) => setImmediate(resolve));
      }

      // Promise.all resolves in request order regardless of completion order, so
      // two runs over the same inputs produce the same ledger.
      return await Promise.all(started);
    } finally {
      clearInterval(poll);
    }
  }

  // Only what an operator queued and the drain has not taken: a halt on the ledger
  // already ended the hunt. Returns the stop, so no interval outlives its call.
  private watchForAbort(): { signal: AbortSignal; stop: () => void } {
    const halt = new AbortController();
    let checking = false;
    const poll = setInterval(() => {
      if (checking) return;
      checking = true;
      void this.abortQueued()
        .then((queued) => {
          if (queued) halt.abort(new Error(CANCELLED_ON_ABORT));
        })
        .finally(() => {
          checking = false;
        });
    }, ABORT_POLL_MS);
    poll.unref?.();
    return { signal: halt.signal, stop: () => clearInterval(poll) };
  }

  private async abortQueued(): Promise<boolean> {
    try {
      return (await peek(this.ledger)).some((directive) => directive.kind === "abort");
    } catch {
      return false;
    }
  }

  // The only call that can lead to proven. Runs before anything is written, so
  // an iteration that pays the critic records the charge with its decision.
  private async runNullCheck(decision: Decision): Promise<NullCheckAttempt> {
    if (decision.action !== "VALIDATE") return NO_NULL_CHECK;
    if (this.critic === undefined) {
      return { ...NO_NULL_CHECK, blocked: "no disconfirmation critic is configured, so it stays active" };
    }

    // validateDecision has already required a target the ledger knows.
    const hypothesis = this.ledger.projection.hypotheses.get(decision.target_hypothesis_id ?? "");
    if (hypothesis === undefined) return NO_NULL_CHECK;
    if (hypothesis.status !== "active") {
      return { ...NO_NULL_CHECK, blocked: `already ${hypothesis.status}; no second verdict was run` };
    }

    const input = nullCheckInput(this.ledger.projection, hypothesis);
    const argued = input.evidence.map((linked) => linked.record.evidence_id);
    try {
      const result = await this.critic.argueNull(input);
      return { result, blocked: "", cost_usd: result.cost_usd, argued };
    } catch (error) {
      // A critic that cannot run fails closed. An unavailable argument is not a
      // won one, and the hunt keeps going with the hypothesis still open — but
      return {
        result: null,
        blocked: `the disconfirmation critic failed (${(error as Error).message}), so it stays active`,
        cost_usd: spentBefore(error),
        argued: [],
      };
    }
  }

  // The one writer of proven, and kept off write() because termination reads
  // hypothesis terminality. Returns what to tell the operator.
  private applyVerdict(iteration: number, decision: Decision, attempt: NullCheckAttempt): string {
    const hypothesisId = decision.target_hypothesis_id ?? "";
    if (attempt.result === null) return `${hypothesisId}: ${attempt.blocked}`;
    const nullCheck = attempt.result;

    const [record] = this.appendEvidence(
      [
        {
          source_system: CRITIC_SOURCE_SYSTEM,
          summary: `strongest benign explanation: ${nullCheck.strongest_benign_explanation}`,
          payload: {
            hypothesis_id: hypothesisId,
            survives: nullCheck.survives,
            // What the argument was made against, so a later verdict can tell a
            // current survival from one that predates half the evidence.
            argued_evidence_ids: attempt.argued,
            strongest_benign_explanation: nullCheck.strongest_benign_explanation,
            rationale: nullCheck.rationale,
            model_id: nullCheck.model_id,
            prompt_version: nullCheck.prompt_version,
            cost_usd: nullCheck.cost_usd,
          },
          salience: "notable",
          why_notable: nullCheck.rationale,
          provenance: NULL_CHECK_PROVENANCE,
          attacker_influenceable: false,
          instruction_like: false,
          // A benign explanation that stands is counter-evidence like any other:
          // it enters the record, reaches the next digest, and is never a verdict.
          ...(nullCheck.survives ? {} : { weakens: [hypothesisId] }),
        },
      ],
      iteration,
      null,
    );
    if (record === undefined) return `${hypothesisId}: the critic's argument could not be recorded`;

    const strength = evidenceStrength(this.ledger.projection, hypothesisId);

    // Checked whichever way the critic argued: not having been able to look is
    // never the same as having cleared it, so this closes inconclusive.
    if (strength.open_gaps >= this.verdicts.gap_lock_threshold) {
      this.ledger.patch("hypothesis", hypothesisId, {
        status: "inconclusive",
        resolution_reason:
          `gap-locked: ${strength.open_gaps} open visibility gap(s) bear on this hypothesis, ` +
          "so the hunt could not look rather than having cleared it",
        evidence_strength: strength,
      });
      return `${hypothesisId} inconclusive (gap-locked)`;
    }

    const unmet = unmetPredicates(strength, this.verdicts);
    if (unmet.length > 0) return `${hypothesisId} stays active: ${unmet.join("; ")}`;

    // The verdict, computed here and only here. Under "ask" it is carried in the
    // checkpoint payload and applied verbatim on approval — the review decides
    const patch = {
      status: "proven",
      resolution_reason:
        `survived the argue-the-null pass against "${nullCheck.strongest_benign_explanation}" ` +
        `on ${strength.corroborating_sources} corroborating source system(s)`,
      evidence_strength: strength,
    };

    const hypothesis = this.ledger.projection.hypotheses.get(hypothesisId);
    const { checkpoint, parked } = this.ask(
      "verdict_review",
      iteration,
      `Mark ${hypothesisId} proven? ${hypothesis?.statement ?? ""}`.trim(),
      { kind: "hypothesis", hypothesis_id: hypothesisId, patch, evidence_strength: strength },
    );
    if (parked) {
      return `${hypothesisId} awaits verdict review at checkpoint ${checkpoint.checkpoint_id}`;
    }

    this.autoResolved(checkpoint, "checkpoint policy verdict_review=auto");
    this.ledger.patch("hypothesis", hypothesisId, patch);
    return `${hypothesisId} proven`;
  }

  // The Hunt Lead asking for an adult. It is not a verdict and it resolves
  // nothing by itself: under "ask" the hunt stops until someone answers, under
  private leadCheckpoint(iteration: number, decision: Decision): string {
    const { checkpoint, parked } = this.ask("budget_anomaly", iteration, decision.rationale, {
      raised_by: "hunt_lead",
      target_hypothesis_id: decision.target_hypothesis_id ?? null,
      stated_confidence: decision.stated_confidence ?? null,
    });
    if (parked) return `parked on checkpoint ${checkpoint.checkpoint_id}: ${decision.rationale}`;

    this.autoResolved(checkpoint, "checkpoint policy budget_anomaly=auto");
    journalNote(
      this.ledger,
      `The Hunt Lead raised a checkpoint: ${decision.rationale}. Checkpoint policy is auto, so nobody was asked ` +
        "and the hunt continued — act on the concern yourself rather than raising it again.",
    );
    return `checkpoint ${checkpoint.checkpoint_id} journaled; policy is auto, so the hunt continues`;
  }

  // An escalation rests on a verdict, so a hypothesis that is not proven is
  // refused the way 08 refuses a premature CONCLUDE: recorded, explained, and
  private handoff(iteration: number, decision: Decision): string {
    const hypothesisId = decision.target_hypothesis_id ?? "";
    const hypothesis = this.ledger.projection.hypotheses.get(hypothesisId);

    if (hypothesis === undefined || hypothesis.status !== "proven") {
      const state = hypothesis === undefined ? "not on the ledger" : hypothesis.status;
      const refusal = `HANDOFF_IR refused: ${hypothesisId || "no hypothesis named"} is ${state}, not proven`;
      journalNote(
        this.ledger,
        `${refusal}. An escalation rests on a verdict, not a hunch — VALIDATE it first, and hand it off if it survives.`,
      );
      return refusal;
    }

    const caseId = newId("case", 4);
    this.ledger.patch("hypothesis", hypothesisId, { status: "handed_off", spawned_case_id: caseId });

    const handoff: Handoff = {
      case_id: caseId,
      hypothesis_id: hypothesisId,
      iteration,
      rationale: decision.rationale,
      created_at: new Date().toISOString(),
    };
    // The deliverable, not a pointer to one: what IR is handed is the claim, the
    // strength numbers, the cited records and what the hunt could not see.
    handoff.case_markdown = renderCaseFile(this.ledger.projection, handoff);
    this.ledger.append({ kind: "handoff", payload: handoff });
    return `${hypothesisId} handed off to incident response as ${caseId}`;
  }

  // Corroboration is counted over source systems, so a label the hunt never
  // declared earns no independence credit: it collapses into one bucket rather
  private attributeSource(record: WorkerEvidence): Pick<EvidenceRecord, "source_system" | "payload"> {
    const declared = this.ledger.projection.hunt.spec.data_domains;
    if (record.provenance !== "worker" || declared.length === 0 || declared.includes(record.source_system)) {
      return { source_system: record.source_system, payload: record.payload };
    }
    return {
      source_system: UNDECLARED_SOURCE,
      payload: { ...record.payload, claimed_source_system: record.source_system },
    };
  }

  // One observation against every active hypothesis, "neither" included: the
  // ruling is what makes the update auditable, not just the supports and weakens.
  private applyRelations(decision: Decision): void {
    const known = this.ledger.projection.hypotheses;
    for (const relation of decision.evidence_relations ?? []) {
      if (!this.ledger.projection.evidence.has(relation.evidence_id)) continue;
      if (!known.has(relation.hypothesis_id)) continue;
      this.ledger.append({ kind: "link", payload: { ...relation } });
    }
  }

  private async write(
    iteration: number,
    digestSeq: number,
    digest: Digest,
    result: DecisionResult,
    dispatchResults: readonly DispatchResult[],
    nullCheck: NullCheckAttempt,
  ): Promise<IterationResult> {
    const decisionId = newId("dec");
    this.ledger.append({
      kind: "decision",
      payload: {
        ...result,
        decision_id: decisionId,
        iteration,
        digest_presented: digest,
        digest_seq: digestSeq,
        created_at: new Date().toISOString(),
      },
    });

    // Applied before this iteration's own dispatches append anything, so the
    // rulings land against the observations the lead was actually shown.
    this.applyRelations(result.decision);

    // Every paid call in the iteration lands in the budget counter: the workers
    // are the largest share of a real hunt's spend, and a max_cost_usd that only
    const workers = dispatchResults.reduce((total, dispatchResult) => total + dispatchResult.cost_usd, 0);
    const spent = Number((result.cost_usd + workers + nullCheck.cost_usd).toFixed(6));
    const hunt = this.ledger.projection.hunt;
    this.ledger.patch("hunt", hunt.hunt_id, {
      iteration,
      cost_usd: Number((hunt.cost_usd + spent).toFixed(6)),
    });

    const appended = dispatchResults.flatMap((dispatchResult) => this.persistDispatch(iteration, dispatchResult));
    const enriched = await this.enrich(iteration, appended.flatMap((record) => record.entities));

    // Before termination: a verdict reached this iteration must be on the record
    // when the terminal path coerces whatever is still active.
    const notes: string[] = [];
    if (result.decision.action === "VALIDATE") {
      notes.push(this.applyVerdict(iteration, result.decision, nullCheck));
    }

    // Both are decisions about what the hunt already knows rather than requests
    // for more of it, so neither dispatches and both land here beside the verdict.
    if (result.decision.action === "HANDOFF_IR") notes.push(this.handoff(iteration, result.decision));
    if (result.decision.action === "CHECKPOINT") notes.push(this.leadCheckpoint(iteration, result.decision));

    // CONCLUDE is a recommendation; the predicate is the judge. A refusal leaves
    // the hunt active, so the budget check below still applies to it.
    if (result.decision.action === "CONCLUDE") notes.push(this.concludeOrRefuse(iteration));
    if (this.ledger.projection.hunt.status === "active" && this.budgetExhausted()) {
      notes.push(this.budgetCheckpoint(iteration));
    }

    const note = notes.filter((entry) => entry !== "").join("; ");

    const final = this.ledger.projection.hunt;
    return {
      hunt_id: final.hunt_id,
      iteration,
      action: result.decision.action,
      decision_id: decisionId,
      cost_usd: spent,
      evidence_appended: appended.length,
      enriched,
      hunt_status: final.status,
      hunt_outcome: final.outcome,
      note,
    };
  }

  // A link to a hypothesis the worker invented would corrupt the contrarian
  // quota, so only ids the ledger already knows are linked.
  private link(evidenceId: string, supports?: string[], weakens?: string[]): void {
    const known = this.ledger.projection.hypotheses;
    for (const [relation, ids] of [["supports", supports], ["weakens", weakens]] as const) {
      for (const hypothesisId of ids ?? []) {
        if (!known.has(hypothesisId)) continue;
        this.ledger.append({ kind: "link", payload: { evidence_id: evidenceId, hypothesis_id: hypothesisId, relation } });
      }
    }
  }

  // Shared by workers and by enrichment, so no evidence source can reach the
  // ledger without sanitize() and entity extraction.
  private appendEvidence(
    records: readonly WorkerEvidence[],
    iteration: number,
    dispatchId: string | null,
  ): EvidenceRecord[] {
    return records.map(sanitize).map(({ supports, weakens, ...record }) => {
      const evidenceId = newId("ev");
      const stored: EvidenceRecord = {
        ...record,
        ...this.attributeSource(record),
        evidence_id: evidenceId,
        dispatch_id: dispatchId,
        iteration,
        // A failed dispatch's text is ours, not the estate's: its addresses are the
        // gateway and the Docker bridge, and a hunt must not investigate its own plumbing.
        entities: record.provenance === TOOL_FAILURE ? [] : entitiesOf(record),
        captured_at: new Date().toISOString(),
      };
      this.ledger.append({ kind: "evidence", payload: stored });
      this.link(evidenceId, supports, weakens);
      return stored;
    });
  }

  // Read-only follow-up on the entities this iteration introduced. Deterministic,
  // so it costs no decision — only rounds, breadth and the once-per-entity rule
  private async enrich(iteration: number, seed: readonly Entity[]): Promise<number> {
    const enricher = this.enricher;
    if (enricher === undefined) return 0;
    const { max_depth, max_entities } = this.enrichment;

    let frontier = seed;
    let total = 0;

    for (let depth = 0; depth < max_depth && frontier.length > 0; depth += 1) {
      const done = this.enrichedEntities();
      // An operator called it known-benign, so the hunt stops spending on it —
      // enrichment is the cheapest place that shows, and the records already
      const suppressed = suppressedEntities(this.ledger.projection);
      const fresh = new Map(frontier.map((entity) => [key(entity), entity] as const));
      const pending = [...fresh].filter(([id]) => !done.has(id) && !suppressed.has(id)).slice(0, max_entities);
      if (pending.length === 0) break;

      const records = (await Promise.all(pending.map(([, entity]) => enricher(entity)))).flat();
      const appended = this.appendEvidence(records, iteration, null);
      total += appended.length;
      frontier = appended.flatMap((record) => record.entities);
    }
    return total;
  }

  private enrichedEntities(): Set<string> {
    const done = new Set<string>();
    for (const record of this.ledger.projection.evidence.values()) {
      if (record.provenance.startsWith("enrichment:")) done.add(String(record.payload["entity"] ?? ""));
    }
    return done;
  }

  private persistDispatch(iteration: number, result: DispatchResult): EvidenceRecord[] {
    // Idempotency on dispatch_id: a retried dispatch re-delivers the same
    // evidence, and appending it twice would inflate corroboration counts. Keyed
    const settled = this.ledger.projection.dispatches.get(result.dispatch_id)?.status;
    if (settled === undefined || settled === "complete") return [];

    // A failed worker is evidence about visibility, not a lost turn, and the rows it
    // did gather come too.
    const records = result.failed
      ? [
          {
            source_system: "dispatcher",
            // The reason stays out of the summary and in the payload: it is our plumbing,
            // and the lead only needs to know a query could not be run.
            summary: "a query the hunt wanted could not be run",
            payload: { failure_reason: result.failure_reason },
            salience: "routine" as const,
            why_notable: "a blind spot in what this run could see, not a finding",
            provenance: TOOL_FAILURE,
            attacker_influenceable: false,
            instruction_like: false,
          },
          ...result.evidence,
        ]
      : result.evidence;

    const appended = this.appendEvidence(records, iteration, result.dispatch_id);

    for (const question of result.questions ?? []) {
      this.raise(sanitizeQuestion(question), {
        spawning_dispatch_id: result.dispatch_id,
        spawned_iteration: iteration,
        // Inherited from the work that opened it, so the lead stays attached to
        // the hypothesis it serves however far it travels down the frontier.
        hypothesis_id: this.ledger.projection.dispatches.get(result.dispatch_id)?.target_hypothesis_id ?? null,
      });
    }

    this.ledger.patch("dispatch", result.dispatch_id, {
      status: result.failed ? "failed" : "complete",
      failure_reason: result.failed ? result.failure_reason : null,
      cost_usd: result.cost_usd,
      calls: result.calls ?? [],
    });
    // A gap record is a fact about visibility, not a finding, so it counts as neither
    // evidence appended nor something worth enriching. Salvaged rows are telemetry, and do.
    return appended.filter((record) => record.provenance !== TOOL_FAILURE);
  }

  // The Hunt Lead recommends stopping; the controller decides. A refusal is not
  // an invalid emission — the decision was schema- and citation-valid, so it
  private concludeOrRefuse(iteration: number): string {
    const verdict = terminationVerdict(this.ledger.projection, iteration, this.termination, this.verdicts);

    if (verdict.outcome === null) {
      const refusal = `CONCLUDE refused: ${verdict.blocked_by}`;
      journalNote(this.ledger, `${refusal}. Resolve it before recommending CONCLUDE again.`);
      return refusal;
    }

    // The second verdict review: before the hunt ends, not only before a
    // hypothesis is proven. The would-be outcome and the leads it would park are
    const { checkpoint, parked } = this.ask(
      "verdict_review",
      iteration,
      `Conclude this hunt as ${verdict.outcome}?`,
      {
        kind: "conclude",
        outcome: verdict.outcome,
        park: verdict.park.map((question) => question.question_id),
        reason: `the termination predicate passed at iteration ${iteration}`,
      },
    );
    if (parked) return `awaiting review of the conclusion at checkpoint ${checkpoint.checkpoint_id}`;

    this.autoResolved(checkpoint, "checkpoint policy verdict_review=auto");
    this.concludeWith(verdict, `the termination predicate passed at iteration ${iteration}`);
    return `concluded as ${verdict.outcome}`;
  }

  // What a passing verdict does, wherever it was asked for. One writer, so the
  // budget checkpoint and a CONCLUDE cannot disagree about what concluding means.
  private concludeWith(verdict: TerminationVerdict & { outcome: HuntOutcome }, reason: string): void {
    // Below the floor and never pulled: these are the backlog deliverable, and
    // closing them here is what makes "done" mean the frontier was cleared.
    for (const question of verdict.park) {
      this.ledger.patch("question", question.question_id, {
        status: "parked",
        closed_reason: `parked to the backlog: below the priority floor of ${this.termination.priority_floor} when the hunt ended`,
      });
    }
    this.terminate(verdict.outcome, reason);
  }

  // Unresolved hypotheses become inconclusive, never disproven: the hunt
  // stopped looking, which is not the same as having cleared them. Every outcome
  terminate(outcome: HuntOutcome, reason = ""): void {
    const hunt = this.ledger.projection.hunt;
    if (hunt.outcome !== null && OUTCOME_PRECEDENCE[hunt.outcome] >= OUTCOME_PRECEDENCE[outcome]) return;

    for (const hypothesis of this.ledger.projection.hypotheses.values()) {
      if (hypothesis.status !== "active") continue;
      this.ledger.patch("hypothesis", hypothesis.hypothesis_id, {
        status: "inconclusive",
        resolution_reason: `hunt ended (${outcome}) with the hypothesis unresolved`,
      });
    }

    this.ledger.patch("hunt", hunt.hunt_id, {
      status: "terminal",
      outcome,
      terminated_at: new Date().toISOString(),
      ...(reason === "" ? {} : { termination_reason: reason }),
    });
    this.finalize();
  }

  // Journaled, not written: the report is a fold over the ledger, so a file
  // beside one would be a second copy that can disagree with it.
  private finalize(): void {
    this.ledger.append({ kind: "finalize", payload: buildReport(this.ledger.projection) });
  }

  private budgetExhausted(): boolean {
    return boundBy(this.ledger.projection.hunt) !== null;
  }
}
