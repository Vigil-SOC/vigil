import type { SpendPayload } from "../../contracts/budget.js";
import type { CheckpointPayload, DispatchPayload, NewEvent, ResolutionPayload, RunOutcome } from "../../contracts/events.js";
import { commitTurn, type Harness, type Outcome, type TurnConfig } from "../../core/loop.js";
import { drain, streamTurn } from "../../core/stream.js";
import type { State } from "../../core/seams.js";
import { SpecError, type PhaseSpec, type RunSpec } from "../../core/spec.js";
import { nullMirror, type Mirror, type PhaseUpdate } from "./mirror.js";
import { PHASE_APPROVAL, PHASE_SCHEMA, type ComposeKinds, type PhaseAnswer, type PhasePayload } from "./vocabulary.js";

export interface ComposeOptions {
  run_id: string;
  spec: RunSpec;
  started_by?: string;
  mirror?: Mirror;
  // Aborted when this worker loses its lease: a run another worker has taken over
  // must stop writing, and stop paying for calls nobody will record.
  signal?: AbortSignal;
}

export interface ComposeReport {
  status: RunOutcome | "waiting_approval";
  reason: string;
  completed: number;
  total: number;
  pending: Outcome<unknown>["pending"];
}

type Event = NewEvent<ComposeKinds>;

// Deny-by-default per step, off the resolved playbook: a step may call what it was
// granted, and no step sees another's. Keyed by step, since one agent may run two.
export function grantsOf(spec: RunSpec): Record<string, readonly string[]> {
  return Object.fromEntries(spec.phases.map((phase) => [phase.id, phase.tools]));
}

// Addressed by step rather than derived from content: a resume has to recognise
// the checkpoint it already raised, and a rephrased question must not orphan it.
export function checkpointFor(phase: PhaseSpec): string {
  return `chk-${phase.id}`;
}

// Refused before the ledger opens. A step that stops for a human, on a deployment
// with nowhere to ask, parks on its first gate and no answer can ever reach it.
function assertAnswerable(spec: RunSpec, mirror: Mirror): void {
  if (mirror.answerable) return;
  const gated = spec.phases.filter((phase) => phase.approval_required).map((phase) => phase.id);
  if (gated.length > 0) {
    throw new SpecError(`${spec.name} stops for a human at ${gated.join(", ")}, and no mirror is configured to carry a decision back`);
  }
}

// The whole of the control flow. The playbook decided the order, so this walks it:
// no decision is taken here, and no step is skipped, reordered or repeated.
export async function runCompose(harness: Harness<ComposeKinds>, options: ComposeOptions): Promise<ComposeReport> {
  const { run_id, spec } = options;
  const mirror = options.mirror ?? nullMirror;
  assertAnswerable(spec, mirror);
  if ((await harness.state.latestSeq(run_id)) === null) await open(harness, options);

  await journal(harness, run_id, mirror);

  for (const [order, phase] of spec.phases.entries()) {
    const ledger = await readLedger(harness.state, run_id);
    if (ledger.done.has(phase.id)) continue;

    const gate = await clear(harness, options, phase, ledger, mirror, order);
    if (gate !== null) return gate;

    await mirror.phase(run_id, { ...at(phase, order), ...answered(phase, ledger), status: "running" });
    const outcome = await drain(
      streamTurn<PhaseAnswer, ComposeKinds>(turnFor(options, phase, brief(spec, ledger.answers)), harness),
    );

    if (outcome.status === "waiting_approval") {
      await commitTurn(harness.state, run_id, []);
      await mirror.phase(run_id, { ...at(phase, order), status: "pending_approval", ...idOf(outcome.pending), question: outcome.reason });
      return { ...progress(spec, ledger.done.size), status: "waiting_approval", reason: outcome.reason, pending: outcome.pending };
    }

    // A step that failed ends the run rather than being stepped over: the steps
    // after it were written expecting its answer, not expecting its absence.
    if (outcome.status === "failed" || outcome.value === null) {
      await commitTurn(harness.state, run_id, [dispatched(options, phase, outcome.reason)]);
      await mirror.phase(run_id, { ...at(phase, order), status: "failed", error: outcome.reason });
      const status: RunOutcome = outcome.refusal === null ? "failed" : "budget_exhausted";
      return end(harness, options, status, `${phase.name}: ${outcome.reason}`, mirror);
    }

    await commitTurn(harness.state, run_id, [
      dispatched(options, phase, null),
      { run_id, run_kind: "compose", kind: "phase", payload: { phase_id: phase.id, agent: phase.agent, name: phase.name, answer: outcome.value } },
    ]);
    await mirror.phase(run_id, { ...at(phase, order), status: "completed", output: { ...outcome.value } });
  }

  return end(harness, options, "completed", `all ${spec.phases.length} phases ran`, mirror);
}

// The one place a human's answer becomes a ledger event. Python records the
// decision and this side journals it, so the ledger keeps its single writer.
async function journal(harness: Harness<ComposeKinds>, runId: string, mirror: Mirror): Promise<void> {
  const ledger = await readLedger(harness.state, runId);
  const fresh = (await mirror.decisions(runId)).filter((decision) => !ledger.resolutions.has(decision.checkpoint_id));
  if (fresh.length === 0) return;

  await append(
    harness.state,
    runId,
    fresh.map((payload) => ({ run_id: runId, run_kind: "compose" as const, kind: "resolution" as const, payload })),
  );
}

// Enough of a phase to address its row over there. Order is the list's, so a
// mirrored row sorts the way the playbook reads.
function at(phase: PhaseSpec, order: number): Pick<PhaseUpdate, "phase_id" | "agent" | "name" | "order"> {
  return { phase_id: phase.id, agent: phase.agent, name: phase.name, order: order + 1 };
}

function idOf(pending: Outcome<unknown>["pending"]): { checkpoint_id?: string } {
  return pending === null ? {} : { checkpoint_id: pending.checkpoint_id };
}

// A gate that has been answered says so on the row. Without it the projection
// reads pending for a step that cleared its approval and ran hours ago.
function answered(phase: PhaseSpec, ledger: Ledger): Pick<PhaseUpdate, "approval_state"> {
  if (!phase.approval_required) return {};
  return ledger.resolutions.has(checkpointFor(phase)) ? { approval_state: "approved" } : {};
}

interface Ledger {
  done: Set<string>;
  answers: PhasePayload[];
  resolutions: Map<string, ResolutionPayload>;
  raised: Set<string>;
  // Summed here rather than read again at the end: this is the one read of the
  // run, and the terminal has to carry the figure to the console.
  spent: number;
}

// One read per step, folded into everything the step needs to know: which steps
// already ran, what they answered, and which pauses have been answered since.
async function readLedger(state: State<ComposeKinds>, runId: string): Promise<Ledger> {
  const events = await state.read(runId);
  const answers = events.filter((event) => event.kind === "phase").map((event) => event.payload as PhasePayload);
  const resolutions = new Map<string, ResolutionPayload>();
  const raised = new Set<string>();
  let spent = 0;

  for (const event of events) {
    if (event.kind === "checkpoint") raised.add((event.payload as CheckpointPayload).checkpoint_id);
    if (event.kind === "resolution") {
      const payload = event.payload as ResolutionPayload;
      resolutions.set(payload.checkpoint_id, payload);
    }
    // A call nobody could price contributes nothing rather than a fabricated
    // zero, which is how spentOn has it for every other kind.
    if (event.kind === "spend") spent += (event.payload as SpendPayload).cost_usd ?? 0;
  }
  return { done: new Set(answers.map((answer) => answer.phase_id)), answers, resolutions, raised, spent };
}

// Parks the run when a step needs a human and nobody has answered. A rejection ends
// the run: refusing a step then running it anyway would make the gate a formality.
async function clear(
  harness: Harness<ComposeKinds>,
  options: ComposeOptions,
  phase: PhaseSpec,
  ledger: Ledger,
  mirror: Mirror,
  order: number,
): Promise<ComposeReport | null> {
  if (!phase.approval_required) return null;

  const id = checkpointFor(phase);
  const resolution = ledger.resolutions.get(id);
  if (resolution !== undefined) {
    if (resolution.answer === "approve") return null;
    await mirror.phase(options.run_id, {
      ...at(phase, order),
      status: "failed",
      approval_state: "rejected",
      error: `rejected: ${resolution.text}`,
    });
    return end(harness, options, "failed", `${phase.name} was rejected: ${resolution.text}`, mirror);
  }

  if (!ledger.raised.has(id)) {
    const question = `Approve ${phase.name}, run by ${phase.agent}?`;
    const payload: CheckpointPayload = { checkpoint_id: id, checkpoint_class: PHASE_APPROVAL, question, raised_at: new Date().toISOString() };
    await append(harness.state, options.run_id, [{ run_id: options.run_id, run_kind: "compose", kind: "checkpoint", payload }]);
    await mirror.phase(options.run_id, { ...at(phase, order), status: "pending_approval", checkpoint_id: id, question });
  }

  return {
    ...progress(options.spec, ledger.done.size),
    status: "waiting_approval",
    reason: `${phase.name} is waiting on approval`,
    pending: { checkpoint_id: id, tool: null, args: null },
  };
}

function dispatched(options: ComposeOptions, phase: PhaseSpec, failure: string | null): Event {
  const payload: DispatchPayload = {
    dispatch_id: phase.id,
    agent_id: phase.agent,
    status: failure === null ? "complete" : "failed",
    question_id: null,
    failure_reason: failure,
  };
  return { run_id: options.run_id, run_kind: "compose", kind: "dispatch", payload };
}

function turnFor(options: ComposeOptions, phase: PhaseSpec, context: string): TurnConfig {
  const { runtime, approvals } = options.spec;
  return {
    run_id: options.run_id,
    run_kind: "compose",
    // The step, not its agent: grants are per step, and spend attributed to an
    // agent would merge two steps the playbook deliberately kept apart.
    role: phase.id,
    system: phase.prompt,
    task: [context, `## Your step: ${phase.name}`, phase.instructions].filter((part) => part).join("\n\n"),
    schema: PHASE_SCHEMA,
    max_turns: runtime.max_turns,
    approvals: new Set(approvals),
    // No verbs: nothing here emits one, so the scanner has no vocabulary to guard.
    verbs: [],
    result_cap: runtime.result_cap,
    recall_limit: runtime.recall_limit,
    ...(options.signal === undefined ? {} : { signal: options.signal }),
  };
}

// What this run is about, then what the steps before this one handed on. Bounded
// by the arch's window so a long playbook does not resend its whole history.
function brief(spec: RunSpec, answers: readonly PhasePayload[]): string {
  const window = spec.digest["phase_window"] ?? answers.length;
  const objectives = spec.objectives.map((line) => `- ${line}`).join("\n");
  const prior = answers
    .slice(-window)
    .map((answer) => `### ${answer.name} (${answer.agent})\n${answer.answer.handoff || answer.answer.summary}`);

  return [
    `Run: ${spec.name}`,
    spec.prompt && `## What this run is about\n\n${spec.prompt}`,
    objectives && `Objectives:\n${objectives}`,
    spec.narrative,
    prior.length > 0 && `## Completed steps\n\n${prior.join("\n\n")}`,
  ]
    .filter((part) => part)
    .join("\n\n");
}

function progress(spec: RunSpec, completed: number): Omit<ComposeReport, "status" | "reason" | "pending"> {
  return { completed, total: spec.phases.length };
}

async function open(harness: Harness<ComposeKinds>, options: ComposeOptions): Promise<void> {
  const event: Event = {
    run_id: options.run_id,
    run_kind: "compose",
    kind: "run",
    payload: {
      run_kind: "compose",
      spec: options.spec,
      budgets: harness.budget.limits,
      seed: options.run_id,
      tenant_id: null,
      started_by: options.started_by ?? "compose",
    },
  };
  await harness.state.append(options.run_id, [event]);
}

async function end(
  harness: Harness<ComposeKinds>,
  options: ComposeOptions,
  outcome: RunOutcome,
  reason: string,
  mirror: Mirror,
): Promise<ComposeReport> {
  const ledger = await readLedger(harness.state, options.run_id);
  const event: Event = { run_id: options.run_id, run_kind: "compose", kind: "terminal", payload: { outcome, reason } };
  await append(harness.state, options.run_id, [event]);
  await mirror.terminal(options.run_id, { outcome, reason, summary: summarise(ledger.answers), cost_usd: ledger.spent });
  return { ...progress(options.spec, ledger.done.size), status: outcome, reason, pending: null };
}

// The run's result as one document: what each step handed on, in the order the
// playbook ran them. The fold, not a stored field.
function summarise(answers: readonly PhasePayload[]): string {
  return answers.map((answer) => `### ${answer.name}\n${answer.answer.summary}`).join("\n\n");
}

// Appends outside a turn, where there is no outcome to commit alongside.
async function append(state: State<ComposeKinds>, runId: string, events: readonly Event[]): Promise<number> {
  return state.append(runId, events);
}
