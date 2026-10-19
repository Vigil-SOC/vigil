import type { SpendPayload } from "../../contracts/budget.js";
import type { CheckpointPayload, DispatchPayload, NewEvent, ResolutionPayload } from "../../contracts/events.js";
import { runSubturn, type Early, type PluginContext, type PluginHooks } from "../../core/agent.js";
import type { Outcome } from "../../core/loop.js";
import type { State } from "../../core/seams.js";
import type { PhaseSpec, RunSpec } from "../../core/spec.js";
import type { Mirror, PhaseUpdate } from "./mirror.js";
import { PHASE_APPROVAL, PHASE_SCHEMA, type ComposeKinds, type PhaseAnswer, type PhasePayload } from "./vocabulary.js";

// The compose plugin's code: the playbook decided the order, so this walks it.
// No step is skipped, reordered or repeated, and no decision is taken here.

type Event = NewEvent<ComposeKinds>;

// Addressed by step rather than derived from content: a resume has to recognise
// the checkpoint it already raised, and a rephrased question must not orphan it.
export function checkpointFor(phase: PhaseSpec): string {
  return `chk-${phase.id}`;
}

function state(ctx: PluginContext): State<ComposeKinds> {
  return ctx.harness.state as unknown as State<ComposeKinds>;
}

function append(ctx: PluginContext, events: readonly Event[]): Promise<number> {
  return state(ctx).append(ctx.options.run_id, events);
}

export function composeHooks(mirror: Mirror): PluginHooks {
  return {
    before: async (ctx): Promise<Early> => {
      const { spec, run_id } = ctx.options;
      await declareUnbound(ctx);

      for (const [order, phase] of spec.phases.entries()) {
        const ledger = await readLedger(state(ctx), run_id);
        if (ledger.done.has(phase.id)) continue;

        const gate = await clear(ctx, phase, ledger, mirror, order);
        if (gate !== null) return gate;

        await mirror.phase(run_id, { ...at(phase, order), ...answered(phase, ledger), status: "running" });
        const outcome = await runSubturn<PhaseAnswer>(ctx, {
          // The step, not its agent: grants are per step, and spend attributed to an
          // agent would merge two steps the playbook deliberately kept apart.
          role: phase.id,
          // Except a skill read, which is the phase's agent's use of the library:
          // read attribution follows the agent, spend follows the step (#1560).
          agent_id: phase.agent,
          system: phase.prompt,
          task: [brief(spec, ledger.answers), `## Your step: ${phase.name}`, phase.instructions].filter((part) => part).join("\n\n"),
          schema: PHASE_SCHEMA,
          tools: phase.tools,
        });

        if (outcome.status === "waiting_approval") {
          await mirror.phase(run_id, { ...at(phase, order), status: "pending_approval", ...idOf(outcome.pending), question: outcome.reason });
          return {
            kind: "park",
            checkpoint_id: outcome.pending?.checkpoint_id ?? checkpointFor(phase),
            reason: outcome.reason,
            tool: outcome.pending?.tool ?? null,
            args: outcome.pending?.args ?? null,
          };
        }

        // A step that failed ends the run rather than being stepped over: the steps
        // after it were written expecting its answer, not expecting its absence.
        if (outcome.status === "failed" || outcome.value === null) {
          await append(ctx, [dispatched(ctx, phase, outcome.reason)]);
          await mirror.phase(run_id, { ...at(phase, order), status: "failed", error: outcome.reason });
          return { kind: "end", outcome: outcome.refusal === null ? "failed" : "budget_exhausted", reason: `${phase.name}: ${outcome.reason}`, summary: null };
        }

        await append(ctx, [
          dispatched(ctx, phase, null),
          { run_id, run_kind: "compose", kind: "phase", payload: { phase_id: phase.id, agent: phase.agent, name: phase.name, answer: outcome.value } },
        ]);
        await mirror.phase(run_id, { ...at(phase, order), status: "completed", output: { ...outcome.value } });
      }
      return { kind: "end", outcome: "completed", reason: `all ${spec.phases.length} phases ran`, summary: null };
    },

    ended: async (ctx, terminal) => {
      const ledger = await readLedger(state(ctx), ctx.options.run_id);
      await mirror.terminal(ctx.options.run_id, { outcome: terminal.outcome, reason: terminal.reason, summary: summarise(ledger.answers), cost_usd: ledger.spent });
    },
  };
}

// Tools a phase asked for that this deployment lacks, journaled once with the opening.
async function declareUnbound(ctx: PluginContext): Promise<void> {
  const events = await state(ctx).read(ctx.options.run_id);
  if (events.some((event) => event.kind === "unbound")) return;
  const unbound = ctx.options.spec.phases.flatMap((phase) =>
    (phase.unavailable ?? []).map(
      ({ tool, reason }): Event => ({
        run_id: ctx.options.run_id,
        run_kind: "compose",
        kind: "unbound",
        payload: { capability: tool, reason: `${phase.name}: ${reason}`, phase_id: phase.id },
      }),
    ),
  );
  if (unbound.length > 0) await append(ctx, unbound);
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
  spent: number;
}

// One read per step, folded into everything the step needs to know.
async function readLedger(held: State<ComposeKinds>, runId: string): Promise<Ledger> {
  const events = await held.read(runId);
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
    // A call nobody could price contributes nothing rather than a fabricated zero.
    if (event.kind === "spend") spent += (event.payload as SpendPayload).cost_usd ?? 0;
  }
  return { done: new Set(answers.map((answer) => answer.phase_id)), answers, resolutions, raised, spent };
}

// Parks the run when a step needs a human and nobody has answered. A rejection ends
// the run: refusing a step then running it anyway would make the gate a formality.
async function clear(ctx: PluginContext, phase: PhaseSpec, ledger: Ledger, mirror: Mirror, order: number): Promise<Early | null> {
  if (!phase.approval_required) return null;
  const { run_id } = ctx.options;
  const id = checkpointFor(phase);
  const resolution = ledger.resolutions.get(id);
  if (resolution !== undefined) {
    if (resolution.answer === "approve") return null;
    await mirror.phase(run_id, { ...at(phase, order), status: "failed", approval_state: "rejected", error: `rejected: ${resolution.text}` });
    return { kind: "end", outcome: "failed", reason: `${phase.name} was rejected: ${resolution.text}`, summary: null };
  }
  if (!ledger.raised.has(id)) {
    const question = `Approve ${phase.name}, run by ${phase.agent}?`;
    const payload: CheckpointPayload = { checkpoint_id: id, checkpoint_class: PHASE_APPROVAL, question, raised_at: new Date().toISOString() };
    await append(ctx, [{ run_id, run_kind: "compose", kind: "checkpoint", payload }]);
    await mirror.phase(run_id, { ...at(phase, order), status: "pending_approval", checkpoint_id: id, question });
  }
  return { kind: "park", checkpoint_id: id, reason: `${phase.name} is waiting on approval` };
}

function dispatched(ctx: PluginContext, phase: PhaseSpec, failure: string | null): Event {
  const payload: DispatchPayload = {
    dispatch_id: phase.id,
    agent_id: phase.agent,
    status: failure === null ? "complete" : "failed",
    question_id: null,
    failure_reason: failure,
  };
  return { run_id: ctx.options.run_id, run_kind: "compose", kind: "dispatch", payload };
}

// What this run is about, then what the steps before this one handed on. Bounded
// by the arch's window so a long playbook does not resend its whole history.
function brief(spec: RunSpec, answers: readonly PhasePayload[]): string {
  const window = spec.digest["phase_window"] ?? answers.length;
  const objectives = spec.objectives.map((line) => `- ${line}`).join("\n");
  const prior = answers.slice(-window).map((answer) => `### ${answer.name} (${answer.agent})\n${answer.answer.handoff || answer.answer.summary}`);
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

// The run's result as one document: what each step handed on, in playbook order.
function summarise(answers: readonly PhasePayload[]): string {
  return answers.map((answer) => `### ${answer.name}\n${answer.answer.summary}`).join("\n\n");
}
