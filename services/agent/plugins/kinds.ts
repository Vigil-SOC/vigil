import { fileURLToPath } from "node:url";
import type { CaseAccount } from "../contracts/account.js";
import type { AgentEvent, RunKind } from "../contracts/events.js";
import type { Notes } from "../core/memory.js";
import type { State } from "../core/seams.js";
import { SpecError, type Owned } from "../core/spec.js";
import { composeProjection } from "./compose/projection.js";
import { huntDistil } from "./hunt/distil.js";
import type { HuntKinds } from "./hunt/ledger.js";
import { huntProjection, type ProjectionOptions } from "./hunt/projection.js";
import { huntNotes } from "./hunt/recall.js";
import { leadProjection } from "./investigate/legacy/projection.js";
import { isAgentRun } from "../core/agent.js";
import { investigateAccount } from "./investigate/account.js";
import { investigateView } from "./investigate/view.js";
import { huntView } from "./hunt/view.js";
import type { LeadKinds } from "./investigate/legacy/vocabulary.js";
import { rootCauseProjection } from "./rootcause/projection.js";
import type { RootCauseKinds } from "./rootcause/proof.js";

// Which loop drives a kind, and what its workflow may act on. Named here rather
// than switched on in the worker: an agent type is a file and an entry, not a branch.
export type WorkflowId = "lead" | "compose" | "hunt" | "rootcause";

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
  projection?: (runId: string, events: readonly AgentEvent<Record<never, never>>[], options?: ProjectionOptions) => unknown;
  // What episodic memory is told about a finished run of this kind; the fold
  // itself says why it is not the projection. Absent means a run of this kind is
  // not distilled, which is the honest default rather than empty rows.
  distil?: (runId: string, events: readonly AgentEvent<Record<never, never>>[]) => unknown;
  // What a run of this kind concluded, in the shape the case page reads. Absent, or
  // null for a ledger it cannot read, means the run has no account to show.
  account?: (runId: string, events: readonly AgentEvent<Record<never, never>>[]) => CaseAccount | null;
  // The plugin the worker runs this kind as, on the kernel (core/agent.ts). Absent
  // for the kinds whose plugin runs through their own wrapper.
  plugin?: string;
}

// The hunt lead-loop, minus its arch prompt. `hunt` and `adjudicate` run this exact
// loop — same actions, halts, ownership, projection and notes — and differ only in
// the arch that frames the lead's job, so the shared mechanics live here and drift
// between the kinds is impossible rather than a two-place edit. `root_cause` does
// not: a backward trace is its own workflow.
// notes/projection are retyped here because this is the one place that already knows
// the kind, the same trade the worker makes when it hands a ledger to a workflow.
const HUNT_LOOP: Omit<ArchEntry, "arch"> = {
  workflow: "hunt",
  actions: ["INVESTIGATE", "EXPAND", "PIVOT", "DEEPEN", "ABANDON", "VALIDATE", "CHECKPOINT", "CONCLUDE", "HANDOFF_IR"],
  halts: ["CONCLUDE"],
  owned: { playbook: ["hypotheses", "attack_techniques", "data_domains"], config: ["enrichment", "checkpoints", "hypothesis_loop"] },
  notes: (state, runId) => huntNotes(state as unknown as State<HuntKinds>, runId),
  // Either engine answers in the hunt's shape; a kernel hunt's moves are its calls.
  projection: (runId, events, options) =>
    isAgentRun(events) ? huntView(runId, events, options) : huntProjection(runId, events as readonly AgentEvent<HuntKinds>[], options),
};

const REGISTERED: Partial<Record<RunKind, ArchEntry>> = {
  // distil is not in HUNT_LOOP: huntDistil stamps investigation_kind "hunt", and
  // that is the only kind episodic memory accepts for a run. Sharing it would
  // file an adjudication under a kind it is not.
  hunt: {
    arch: packaged("hunt"),
    ...HUNT_LOOP,
    distil: (runId, events) => huntDistil(runId, events as readonly AgentEvent<HuntKinds>[]),
  },
  // One investigator, prose, no hunt vocabulary. checkpoints is owned so a
  // definition that sets hypothesis_approval: ask still parses and can park.
  root_cause: {
    arch: packaged("rootcause"),
    workflow: "rootcause",
    actions: [],
    halts: [],
    owned: { config: ["checkpoints"] },
    projection: (runId, events) => rootCauseProjection(runId, events as readonly AgentEvent<RootCauseKinds>[]),
  },
  // adjudicate is a hunt run as a second opinion: the lead is shown a finding
  // intake admitted and the workflow intake chose, tests the stated intent against
  // the seeded benign account, and proposes a workflow without starting one. Same
  // HUNT_LOOP, no distil for the reason given above.
  adjudicate: { arch: packaged("adjudicate"), ...HUNT_LOOP },
  investigate: {
    arch: packaged("investigate"),
    workflow: "lead",
    actions: ["EXAMINE", "CONCLUDE"],
    halts: ["CONCLUDE"],
    plugin: "investigate",
    // Either engine answers in the lead's shape, so the console needs no second view.
    projection: (runId, events) =>
      isAgentRun(events) ? investigateView(runId, events) : leadProjection(runId, events as readonly AgentEvent<LeadKinds>[]),
    // The lead loop kept no record to read an account from.
    account: (runId, events) => (isAgentRun(events) ? investigateAccount(runId, events) : null),
  },
  // No actions: nothing emits one. A step ends when its agent answers, and the run
  // ends when the list does, so there is no verb for a model to choose or to halt on.
  compose: {
    arch: packaged("compose"),
    workflow: "compose",
    actions: [],
    halts: [],
    projection: composeProjection,
  },
  // No actions: the lead answers in prose, so there is no emission to constrain.
  // Served over SSE by serve.ts rather than the queue, so drive() never sees it.
  chat: { arch: packaged("chat"), workflow: "lead", actions: [], halts: [] },
};

// A plugin's plugin.yaml, which carries the arch layer too. Resolved against the
// package rather than the cwd: the plugins ship with the worker, so a run started
// from any directory finds the same ones.
function packaged(plugin: string): string {
  return fileURLToPath(new URL(`./${plugin}/plugin.yaml`, import.meta.url));
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

// Whether a kind runs the shared hunt lead-loop -- `hunt`, `adjudicate`, and any
// future kind that reuses HUNT_LOOP. The one place the membership is decided, so a
// new hunt-like kind is registered above and nothing downstream has to be found and
// widened by hand. Mirrors Python's is_hunt_like / HUNT_LIKE_RUN_KINDS.
export function isHuntLike(kind: RunKind): boolean {
  return REGISTERED[kind]?.workflow === "hunt";
}
