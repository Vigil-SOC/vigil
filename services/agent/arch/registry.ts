import { fileURLToPath } from "node:url";
import type { AgentEvent, RunKind } from "../contracts/events.js";
import type { Notes } from "../core/memory.js";
import type { State } from "../core/seams.js";
import { SpecError, type Owned } from "../core/spec.js";
import { composeProjection } from "../workflows/compose/projection.js";
import { huntDistil } from "../workflows/hunt/distil.js";
import type { HuntKinds } from "../workflows/hunt/ledger.js";
import { huntProjection } from "../workflows/hunt/projection.js";
import { huntNotes } from "../workflows/hunt/recall.js";
import { leadProjection } from "../workflows/lead/projection.js";
import type { LeadKinds } from "../workflows/lead/workflow.js";

// Which loop drives a kind, and what its workflow may act on. Named here rather
// than switched on in the worker: an agent type is a file and an entry, not a branch.
export type WorkflowId = "lead" | "compose" | "hunt";

export interface ArchEntry {
  arch: string;
  workflow: WorkflowId;
  actions: readonly string[];
  halts: readonly string[];
  // Sections of the playbook and config this workflow reads. The loader accepts
  // them and looks no further: their meaning is the workflow's to give.
  owned?: Owned;
  // How a later run recalls a finished one of this kind. Absent means it carries
  // nothing forward, which is the honest default rather than a summary of events.
  notes?: (state: State, runId: string) => Notes;
  // What a reader outside this process is told about a run of this kind. Absent
  // means there is nothing to report but the terminal the ledger already carries.
  projection?: (runId: string, events: readonly AgentEvent<Record<never, never>>[]) => unknown;
  // What episodic memory is told about a finished run of this kind; the fold
  // itself says why it is not the projection. Absent means a run of this kind is
  // not distilled, which is the honest default rather than empty rows.
  distil?: (runId: string, events: readonly AgentEvent<Record<never, never>>[]) => unknown;
}

// The hunt lead-loop, minus its arch prompt. Both `hunt` and `root_cause` run this
// exact loop — same actions, halts, ownership, projection and notes — and differ only
// in the arch that frames the lead's job, so the shared mechanics live here and drift
// between the two kinds is impossible rather than a two-place edit to remember.
// notes/projection are retyped here because this is the one place that already knows
// the kind, the same trade the worker makes when it hands a ledger to a workflow.
const HUNT_LOOP: Omit<ArchEntry, "arch"> = {
  workflow: "hunt",
  actions: ["INVESTIGATE", "EXPAND", "PIVOT", "DEEPEN", "ABANDON", "VALIDATE", "CHECKPOINT", "CONCLUDE", "HANDOFF_IR"],
  halts: ["CONCLUDE"],
  owned: { playbook: ["hypotheses", "attack_techniques", "data_domains"], config: ["enrichment", "checkpoints", "hypothesis_loop"] },
  notes: (state, runId) => huntNotes(state as unknown as State<HuntKinds>, runId),
  projection: (runId, events) => huntProjection(runId, events as readonly AgentEvent<HuntKinds>[]),
};

const REGISTERED: Partial<Record<RunKind, ArchEntry>> = {
  // distil is not in HUNT_LOOP: huntDistil stamps investigation_kind "hunt", and
  // that is the only kind episodic memory accepts for a run. Sharing it would
  // file root-cause runs under a kind they are not.
  hunt: {
    arch: packaged("threathunt.yaml"),
    ...HUNT_LOOP,
    distil: (runId, events) => huntDistil(runId, events as readonly AgentEvent<HuntKinds>[]),
  },
  // root-cause is a hunt run backward: only rootcause.yaml differs, framing the
  // lead's job as tracing a confirmed compromise to its origin. Sharing HUNT_LOOP
  // keeps the kind honest rather than borrowing "hunt".
  root_cause: { arch: packaged("rootcause.yaml"), ...HUNT_LOOP },
  investigate: {
    arch: packaged("investigate.yaml"),
    workflow: "lead",
    actions: ["EXAMINE", "CONCLUDE"],
    halts: ["CONCLUDE"],
    projection: (runId, events) => leadProjection(runId, events as readonly AgentEvent<LeadKinds>[]),
  },
  // No actions: nothing emits one. A step ends when its agent answers, and the run
  // ends when the list does, so there is no verb for a model to choose or to halt on.
  compose: {
    arch: packaged("compose.yaml"),
    workflow: "compose",
    actions: [],
    halts: [],
    projection: composeProjection,
  },
  // No actions: the lead answers in prose, so there is no emission to constrain.
  // Served over SSE by serve.ts rather than the queue, so drive() never sees it.
  chat: { arch: packaged("chat.yaml"), workflow: "lead", actions: [], halts: [] },
};

// Resolved against the package rather than the cwd: the arch files ship with the
// worker, so a run started from any directory finds the same ones.
function packaged(file: string): string {
  return fileURLToPath(new URL(`./${file}`, import.meta.url));
}

export function archFor(kind: RunKind): ArchEntry {
  const entry = REGISTERED[kind];
  if (entry === undefined) {
    throw new SpecError(`no architecture is registered for run_kind ${kind}; registered: ${registeredKinds().join(", ")}`);
  }
  return entry;
}

export function registeredKinds(): RunKind[] {
  return (Object.keys(REGISTERED) as RunKind[]).sort();
}

// Whether a kind runs the shared hunt lead-loop -- `hunt`, `root_cause`, and any
// future kind that reuses HUNT_LOOP. The one place the membership is decided, so a
// new hunt-like kind is registered above and nothing downstream has to be found and
// widened by hand. Mirrors Python's is_hunt_like / HUNT_LIKE_RUN_KINDS.
export function isHuntLike(kind: RunKind): boolean {
  return REGISTERED[kind]?.workflow === "hunt";
}
