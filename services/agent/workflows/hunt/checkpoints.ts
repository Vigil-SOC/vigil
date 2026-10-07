import { logger } from "../../core/log.js";
import { newId } from "./ids.js";
import type { Projection } from "./ledger.js";
import type { CheckpointPayload, ResolutionPayload } from "../../contracts/events.js";
import type { Directive } from "./types.js";

const log = logger("agent.hunt.checkpoints");

// The four moments a hunt is allowed to stop and ask. Closed, because a class
// the controller has no policy for would raise a checkpoint nobody can answer.
export const CHECKPOINT_CLASSES = [
  // At hunt start, before any query runs.
  "hypothesis_approval",
  // Growth inside the declared tenant. A crossing of the tenant itself is
  // refused outright rather than asked about — see refuseCrossTenant in loop.ts.
  "scope_extension",
  // Before a hypothesis is marked proven, and before the hunt concludes.
  "verdict_review",
  // The Hunt Lead's own CHECKPOINT: it is asking for an adult, not emitting a verdict.
  "budget_anomaly",
] as const;

export type CheckpointClass = (typeof CHECKPOINT_CLASSES)[number];

// ask suspends the hunt until a human answers; auto answers it on the spot and
// journals that it did. Both leave the same record — an approval that is not on
export type CheckpointPolicy = "ask" | "auto";
export type Checkpoints = Record<CheckpointClass, CheckpointPolicy>;

// hypothesis_approval and verdict_review default to auto so a headless run — CI,
// --scripted, any programmatic startHunt — advances with no TTY and no pending
export const DEFAULT_CHECKPOINTS: Checkpoints = {
  hypothesis_approval: "auto",
  scope_extension: "auto",
  verdict_review: "auto",
  budget_anomaly: "auto",
};

// The actor on a resolution nobody was asked for. Named rather than blank so a
// reader can tell policy from a person at a glance, and so grepping the ledger
export const AUTO_ACTOR = "policy:auto";

// What a definition asked for, made safe to act on. Policies now arrive from a
// WORKFLOW.md's front matter, which is text nobody type-checks: YAML reads
// `hypothesis_approval: yes` as the boolean true, and the controller branches on
// `=== "ask"` for the gate and `=== "auto"` for the auto-answer, so a value that is
// neither takes no branch at all -- the run starts active AND leaves an unresolved
// checkpoint behind. That is an approval gate switched off by a typo, so anything
// that is not exactly "auto" is read as "ask": a definition nobody can parse asks a
// human rather than spending on its own say-so. Unknown classes are dropped, since
// the controller has no policy for one and would raise what nobody can answer.
export function checkpointsFrom(declared: unknown): Partial<Checkpoints> {
  const asked = (declared ?? {}) as Record<string, unknown>;
  const safe: Partial<Checkpoints> = {};
  for (const [name, policy] of Object.entries(asked)) {
    if (!(CHECKPOINT_CLASSES as readonly string[]).includes(name)) {
      log.warn("ignoring a checkpoint policy that names no checkpoint class", { name });
      continue;
    }
    if (policy !== "ask" && policy !== "auto") {
      log.warn("a checkpoint policy is neither ask nor auto, asking", { name, policy: JSON.stringify(policy) });
    }
    safe[name as CheckpointClass] = policy === "auto" ? "auto" : "ask";
  }
  return safe;
}

export type Resolution = ResolutionPayload;
export type Checkpoint = CheckpointPayload;

// Built rather than appended: the controller writes them with its own events in
// one transaction, so a checkpoint cannot land without what raised it.
export function raiseCheckpoint(
  checkpointClass: CheckpointClass,
  raisedIteration: number,
  question: string,
  context: Record<string, unknown> = {},
): Checkpoint {
  return {
    checkpoint_id: newId("cp", 4),
    checkpoint_class: checkpointClass,
    raised_iteration: raisedIteration,
    question,
    context,
    raised_at: new Date().toISOString(),
  };
}

export function resolveCheckpoint(
  checkpoint: Checkpoint,
  answer: Resolution["answer"],
  actor: string,
  text: string,
  directive: Directive | null = null,
): Resolution {
  return {
    checkpoint_id: checkpoint.checkpoint_id,
    answer,
    actor,
    text,
    directive_id: directive?.directive_id ?? null,
    resolved_at: new Date().toISOString(),
  };
}

export function resolutionOf(projection: Projection, checkpointId: string): Resolution | undefined {
  // First wins: a second answer to a settled question is a duplicate, not a
  // change of mind, and reversing a decision is its own directive.
  return projection.resolutions.find((resolution) => resolution.checkpoint_id === checkpointId);
}

// Raised minus resolved, folded like everything else, so a pending checkpoint
// survives process death and comes back from the ledger alone.
export function pendingCheckpoints(projection: Projection): Checkpoint[] {
  return [...projection.checkpoints.values()].filter(
    (checkpoint) => resolutionOf(projection, checkpoint.checkpoint_id) === undefined,
  );
}
