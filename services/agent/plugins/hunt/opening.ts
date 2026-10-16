import type { RunKind } from "../../contracts/events.js";
import type { State } from "../../core/seams.js";
import { unboundCapabilities } from "../../core/spec.js";
import { AUTO_ACTOR, DEFAULT_CHECKPOINTS, raiseCheckpoint, resolveCheckpoint } from "./checkpoints.js";
import type { HuntSpec } from "./config.js";
import { newId } from "./ids.js";
import { Journal, type HuntKinds } from "./journal.js";
import type { DirectiveQueue } from "./ports.js";
import { narrativeOf } from "./render.js";
import { DEPLOYMENT_GAP_PROVENANCE, unboundInWords } from "./strength.js";
import type { HuntState } from "./types.js";

// How a hunt opens and is picked back up, and the names its record is written in, so
// the ledger a hunt opens on is the one every fold over it was written against.

// Seeded at its base rate: most activity that looks like this is not an attack,
// so the benign account starts as the hypothesis to beat rather than an objection.
export const BASE_RATE_PROVENANCE = "base_rate";
// The generic fallback, used only when there is no hypothesis to specialise from.
export const NULL_HYPOTHESIS = "the activity has a benign explanation and no attack occurred";

// The null, phrased against the activity actually under test. A generic "no attack
// occurred" is a strawman the evidence never has to engage; a base rate that names
// this activity's legitimate explanation is a harder claim to beat, and it keeps the
// benign account a genuine competitor from turn 0 rather than a placeholder. The
// argue-the-null critic still sharpens it further with the specific counter-story it
// constructs against the linked evidence at VALIDATE.
export function nullHypothesisFor(hypotheses: readonly string[]): string {
  const primary = hypotheses.find((h) => h.trim().length > 0);
  if (primary === undefined) return NULL_HYPOTHESIS;
  return `the activity described in "${primary}" has a legitimate explanation — expected operations, sanctioned tooling, or normal user/automation behavior — and is not adversary action`;
}

// What the deployment reports about its own reach, never a worker's telemetry. Kept
// out of data_domains so it earns no corroboration credit.
export const DEPLOYMENT_SOURCE = "deployment";

// A belief the caller put up for this run, rather than one the definition states.
// Both are contenders the null must beat; only the provenance tells them apart.
export const OPERATOR_HYPOTHESIS_PROVENANCE = "operator";

export class HuntAlreadyTerminal extends Error {}

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

export async function startHunt(
  state: State<HuntKinds>,
  queue: DirectiveQueue,
  runId: string,
  spec: HuntSpec,
  startedBy = "worker",
  // Defaults to "hunt" so every existing caller and test is unchanged. Adjudicate
  // passes its own kind so the ledger is not filed as a hunt.
  runKind: RunKind = "hunt",
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
  }, runKind, {
    run_kind: runKind,
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

  // The caller's own claim, on the board as a peer of the definition's, carrying
  // whatever they said it was about. Absent when they said nothing, which is not
  // the same as a claim about nobody.
  for (const statement of spec.operator_hypotheses) {
    const subjects = spec.operator_hypothesis_subjects[statement];
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
        ...(subjects === undefined ? {} : { subjects }),
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
        statement: nullHypothesisFor([...spec.operator_hypotheses, ...spec.hypotheses]),
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
        summary: unboundInWords(capability),
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
  //
  // "hunt" only for a hunt: a root-cause run parks here too, and it is usually one
  // no operator started -- a hunt's escalation teed it up -- so calling it a hunt
  // names the wrong run. spec.name is no substitute: it is the playbook's own name,
  // as often a phrase about the activity as it is a title.
  //
  // Counted over the whole board, the benign explanation included, because that is what
  // the case's Explanations tab shows once the run starts; each carries its provenance so
  // the approval can say which one is the claim to beat.
  const count = ledger.projection.hypotheses.size;
  const checkpoint = raiseCheckpoint(
    "hypothesis_approval",
    0,
    `Approve and start this ${runKind === "hunt" ? "hunt" : "run"} on ${count} ${count === 1 ? "explanation" : "explanations"}`,
    {
      hypotheses: [...ledger.projection.hypotheses.values()].map((hypothesis) => ({
        hypothesis_id: hypothesis.hypothesis_id,
        statement: hypothesis.statement,
        provenance: hypothesis.provenance,
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
  // Only reached for a ledger with no run event to read the kind off, which a
  // resume does not have: Journal.open prefers what the ledger itself says.
  runKind: RunKind = "hunt",
): Promise<{ ledger: Journal; spec: HuntSpec }> {
  const ledger = await Journal.open(state, queue, runId, runKind);
  const { hunt } = ledger.projection;
  if (hunt.status === "terminal") throw new HuntAlreadyTerminal(`${hunt.hunt_id} already ended as ${hunt.outcome}`);
  return { ledger, spec: hunt.spec };
}
