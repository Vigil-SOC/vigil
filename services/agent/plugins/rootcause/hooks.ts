import type { CheckpointPayload, DispatchPayload, NewEvent } from "../../contracts/events.js";
import type { Ending, Early, PluginContext, PluginHooks } from "../../core/agent.js";
import type { Outcome } from "../../core/loop.js";
import type { State } from "../../core/seams.js";
import type { RunSpec } from "../../core/spec.js";
import type { StreamEvent } from "../../core/stream.js";
import { toolsFrom } from "../../tools/remote.js";
import {
  latestSteps,
  observationsOf,
  openSteps,
  openSummary,
  redact,
  PROVER_TOOL,
  type NoticePayload,
  type RootCauseKinds,
  type StepPayload,
} from "./proof.js";
import { finishFrom, recordFrom } from "./tools.js";
import { showing } from "./trim.js";

// The root-cause plugin's code: the proof, and everything that hangs off it. The
// loop, parking, stopping and ending are the kernel's.

const PERMIT_ID = "cp-root-cause-permit";
const PERMIT_QUESTION = "Permit a root-cause trace of this finding?";
const NO_TELEMETRY = "No telemetry_search tool is bound, so this run cannot investigate.";
const NO_TELEMETRY_NOTICE = "This deployment has no telemetry search, so the trace could not look.";
const UNPROVABLE =
  "A link or an origin cannot be proved on this deployment: the bound telemetry is not splunk_execute on the in-repo server.";
const OWN_TOOLS = new Set(["record", "finish"]);

type Event = NewEvent<RootCauseKinds>;

function state(ctx: PluginContext): State<RootCauseKinds> {
  return ctx.harness.state as unknown as State<RootCauseKinds>;
}

function append(ctx: PluginContext, events: readonly Event[]): Promise<number> {
  return state(ctx).append(ctx.options.run_id, events);
}

export const rootCauseHooks: PluginHooks = {
  // record and finish write the trace itself. The deployment declares them as
  // local tools; this is where their implementations live.
  tools: (ctx) =>
    toolsFrom(
      ctx.options.spec.tools.filter((tool) => OWN_TOOLS.has(tool.id)),
      {
        record: recordFrom(state(ctx), ctx.options.run_id),
        finish: finishFrom(state(ctx), ctx.options.run_id, ctx.harness.budget.limits),
      },
    ),

  // What a search returns is trimmed to what the model needs to see; what it
  // returned is journaled in full below, because the proof reads that.
  dispatch: (ctx, inner) => showing(inner, ctx.options.spec.runtime.result_cap),

  before: async (ctx): Promise<Early | null> => {
    const parked = await permit(ctx);
    if (parked !== null) return parked;
    const telemetry = ctx.options.spec.tools.filter((tool) => tool["provides"] === "telemetry_search");
    if (telemetry.length === 0) {
      await noticeOnce(ctx, NO_TELEMETRY_NOTICE);
      return { kind: "end", outcome: "completed", reason: NO_TELEMETRY, summary: NO_TELEMETRY };
    }
    if (!telemetry.some((tool) => tool.id === PROVER_TOOL)) await noticeOnce(ctx, UNPROVABLE);
    return null;
  },

  brief: async (ctx) => brief(ctx),

  observe: async (ctx, seen) => journalRemote(ctx, seen),

  finish: async (ctx, outcome): Promise<Ending> => {
    const events = await state(ctx).read(ctx.options.run_id);
    const steps = latestSteps(events);
    const notices = noticeText(events);
    if (outcome.refusal?.reason === "cost_exhausted" || outcome.refusal?.reason === "wall_exhausted") {
      return { outcome: "completed", reason: "the cost or wall ceiling stopped the trace", summary: deliver(openSummary(steps), steps, notices) };
    }
    if (outcome.status !== "completed" || outcome.value === null) return { outcome: "failed", reason: outcome.reason };
    const prose = prosed(outcome);
    // The turn ends when the model answers in prose, whether or not it called
    // finish. A report over steps still open says so, as the ceiling's does.
    if (steps.length > 0 && openSteps(steps).length === 0) {
      return { outcome: "completed", reason: "the trace finished", summary: deliver(prose, steps, notices) };
    }
    return {
      outcome: "completed",
      reason: "the investigator stopped with steps still open",
      summary: deliver(`${prose}\n\n${openSummary(steps)}`, steps, notices),
    };
  },

  stopped: async (ctx) => {
    const events = await state(ctx).read(ctx.options.run_id);
    const steps = latestSteps(events);
    return deliver(openSummary(steps), steps, noticeText(events));
  },
};

function prosed(outcome: Outcome<unknown>): string {
  return typeof outcome.value === "string" ? outcome.value : JSON.stringify(outcome.value);
}

// The trace asks before it starts when the definition says so: it reads raw
// telemetry about one finding, and an operator may want to say which.
async function permit(ctx: PluginContext): Promise<Early | null> {
  if (!asks(ctx.options.spec)) return null;
  const events = await state(ctx).read(ctx.options.run_id);
  const resolution = events.find(
    (event) => event.kind === "resolution" && (event.payload as { checkpoint_id?: string }).checkpoint_id === PERMIT_ID,
  );
  const answer = resolution === undefined ? undefined : (resolution.payload as { answer?: string; text?: string });
  if (answer?.answer === "approve") return null;
  if (answer?.answer === "reject") return { kind: "end", outcome: "failed", reason: answer.text || "the root-cause trace was rejected" };

  const raised = events.some(
    (event) => event.kind === "checkpoint" && (event.payload as { checkpoint_id?: string }).checkpoint_id === PERMIT_ID,
  );
  const finding = ctx.options.spec.prompt.trim();
  if (!raised) {
    const payload: CheckpointPayload = {
      checkpoint_id: PERMIT_ID,
      checkpoint_class: "hypothesis_approval",
      question: finding === "" ? PERMIT_QUESTION : `${PERMIT_QUESTION}\n\n${finding}`,
      raised_at: new Date().toISOString(),
      context: { finding },
    };
    await append(ctx, [{ run_id: ctx.options.run_id, run_kind: ctx.options.run_kind, kind: "checkpoint", payload }]);
  }
  return { kind: "park", checkpoint_id: PERMIT_ID, reason: PERMIT_QUESTION };
}

function asks(spec: RunSpec): boolean {
  const declared = spec.sections["checkpoints"];
  if (declared === null || typeof declared !== "object" || Array.isArray(declared)) return false;
  return (declared as Record<string, unknown>)["hypothesis_approval"] === "ask";
}

async function noticeOnce(ctx: PluginContext, text: string): Promise<void> {
  const events = await state(ctx).read(ctx.options.run_id);
  if (events.some((event) => event.kind === "notice")) return;
  await append(ctx, [{ run_id: ctx.options.run_id, run_kind: ctx.options.run_kind, kind: "notice", payload: { text } }]);
}

// Every search result goes on the ledger whole: the proof counts values across them,
// and the kernel's record of a call is only what the model read, trimmed above.
async function journalRemote(ctx: PluginContext, seen: StreamEvent<unknown>): Promise<void> {
  if (seen.type !== "tool_result") return;
  if (OWN_TOOLS.has(seen.call.tool)) return;
  const result = seen.attempt.result;
  const payload: DispatchPayload = {
    dispatch_id: `dsp-${seen.call.id}`,
    agent_id: "lead",
    status: result.ok ? "complete" : "failed",
    question_id: null,
    failure_reason: result.ok ? null : result.failure.kind,
    result,
    calls: [{ tool: seen.call.tool, arguments: seen.call.args }],
  };
  await append(ctx, [{ run_id: ctx.options.run_id, run_kind: ctx.options.run_kind, kind: "dispatch", payload }]);
}

// What the trace already holds, rendered each time it is picked up: the steps are
// the record, and a resume continues from them rather than starting over.
async function brief(ctx: PluginContext): Promise<string> {
  const events = await state(ctx).read(ctx.options.run_id);
  const steps = latestSteps(events);
  const recorded =
    steps.length === 0
      ? "No step has been recorded yet."
      : steps
          .map(
            (step) =>
              `${step.step_id} at ${step.at}: ${step.event}` +
              `${step.who === "" ? "" : ` who=${step.who}`}` +
              `${step.link === "" ? "" : ` link=${step.link}`}` +
              `${step.cause_id === null ? "" : ` cause=${step.cause_id}`} ` +
              `link=${step.link_status} origin=${step.origin_status}`,
          )
          .join("\n");
  const spec = ctx.options.spec;
  const objectives = spec.objectives.map((line) => `- ${line}`).join("\n");
  return [
    `Run: ${spec.name}`,
    spec.prompt && `## Finding\n\n${spec.prompt}`,
    objectives && `Objectives:\n${objectives}`,
    spec.narrative,
    noticeText(events).join("\n"),
    `## Steps already on the ledger\n\n${recorded}\n\nContinue from these steps. Do not start the trace over.`,
    observationsOf(events).length === 0 ? "" : `${observationsOf(events).length} telemetry result(s) are already journaled.`,
  ]
    .filter((part) => part)
    .join("\n\n");
}

function noticeText(events: readonly { kind: string; payload: unknown }[]): string[] {
  return events
    .filter((event) => event.kind === "notice")
    .map((event) => (event.payload as NoticePayload).text)
    .filter((text) => text !== "");
}

function deliver(text: string, steps: readonly StepPayload[], notices: readonly string[]): string {
  const missing = notices.filter((line) => !text.includes(line));
  return redact([...missing, text].filter((part) => part).join("\n\n"), steps);
}
