import type { CheckpointPayload, DispatchPayload, NewEvent, RunOutcome, TerminalPayload } from "../../contracts/events.js";
import { journalAnswers, noAnswers, type Answers } from "../../core/answers.js";
import { announceOpen, noAnnounce, type Announce } from "../../core/checkpoints.js";
import type { Harness, Outcome } from "../../core/loop.js";
import { registryOf } from "../../core/registry.js";
import type { State } from "../../core/seams.js";
import { SpecError, type RunSpec } from "../../core/spec.js";
import { streamTurn, type StreamEvent } from "../../core/stream.js";
import { toolsFrom } from "../../tools/remote.js";
import { grantsOf } from "../lead/workflow.js";
import { link } from "../hunt/adapters.js";
import type { DirectiveQueue } from "../hunt/ports.js";
import type { Directive } from "../hunt/types.js";
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

export type { NoticePayload, RootCauseKinds, StepPayload };

export interface RootCauseOptions {
  run_id: string;
  spec: RunSpec;
  started_by?: string;
  answers?: Answers;
  announce?: Announce;
  signal?: AbortSignal;
  // Where an operator's stop is queued. Read before the trace starts and polled
  // while it runs, so a cancel lands here rather than at the backend's backstop.
  queue?: DirectiveQueue;
}

export interface RootCauseReport {
  status: RunOutcome | "waiting_approval";
  reason: string;
  pending: Outcome<unknown>["pending"];
}

const PERMIT_ID = "cp-root-cause-permit";
const PERMIT_QUESTION = "Permit a root-cause trace of this finding?";
const NO_TELEMETRY = "No telemetry_search tool is bound, so this run cannot investigate.";
const UNPROVABLE =
  "A link or an origin cannot be proved on this deployment: the bound telemetry is not splunk_execute on the in-repo server.";

type Event = NewEvent<RootCauseKinds>;

// How often the queue is read while the trace runs. Bounds how long a stop waits
// on a model call or a search already in flight.
const ABORT_POLL_MS = 500;

// One investigator, one streamTurn. The ledger holds the steps; a resume reads
// those and continues, and does not replay the transcript.
export async function runRootCause(harness: Harness<RootCauseKinds>, options: RootCauseOptions): Promise<RootCauseReport> {
  const { run_id, spec } = options;
  if (spec.roles.lead === undefined) throw new SpecError(`arch ${spec.arch} declares no lead, so it cannot trace a root cause`);
  if ((await harness.state.latestSeq(run_id)) === null) await open(harness, options);
  await journalAnswers(harness.state, run_id, "root_cause", options.answers ?? noAnswers);

  const done = await terminalOf(harness.state, run_id);
  if (done !== null) return { status: done.outcome, reason: done.reason, pending: null };

  const queued = await abortOf(options);
  if (queued !== null) return stopped(harness, options, queued);

  const parked = await permit(harness, options);
  if (parked !== null) return parked;

  const telemetry = spec.tools.filter((tool) => tool["provides"] === "telemetry_search");
  if (telemetry.length === 0) return end(harness, options, "completed", NO_TELEMETRY, NO_TELEMETRY);

  if (!telemetry.some((tool) => tool.id === PROVER_TOOL)) await noticeOnce(harness, options);

  const scoped: Harness<RootCauseKinds> = {
    ...harness,
    registry: registryOf(
      toolsFrom(spec.tools, {
        record: recordFrom(harness.state, run_id),
        finish: finishFrom(harness.state, run_id, harness.budget.limits),
      }),
      grantsOf(spec),
    ),
  };

  // The lease and the operator's stop both end the turn.
  const watch = watchForAbort(options);
  const linked = link(options.signal, watch.signal);
  const turn = turnFor({ ...options, ...(linked.signal === undefined ? {} : { signal: linked.signal }) }, await brief(harness.state, options));
  const stream = streamTurn<string, RootCauseKinds>(turn, scoped);
  let outcome: Outcome<string> | undefined;
  try {
    for (;;) {
      const next = await stream.next();
      if (next.done) {
        outcome = next.value;
        break;
      }
      await journalRemote(harness, options, next.value);
    }
  } catch (error) {
    const directive = watch.directive();
    if (directive !== null) return stopped(harness, options, directive);
    throw error;
  } finally {
    watch.stop();
    linked.release();
  }
  if (outcome === undefined) return end(harness, options, "failed", "the trace produced no outcome");

  if (outcome.status === "waiting_approval") {
    if (outcome.pending !== null) {
      await announceOpen(harness.state, run_id, "root_cause", outcome.pending.checkpoint_id, options.announce ?? noAnnounce);
    }
    return { status: "waiting_approval", reason: outcome.reason, pending: outcome.pending };
  }

  const events = await harness.state.read(run_id);
  const steps = latestSteps(events);
  const notices = noticeText(events);
  if (outcome.refusal?.reason === "cost_exhausted" || outcome.refusal?.reason === "wall_exhausted") {
    const summary = deliver(openSummary(steps), steps, notices);
    return end(harness, options, "completed", "the cost or wall ceiling stopped the trace", summary);
  }
  if (outcome.status === "failed" || outcome.value === null) {
    return end(harness, options, "failed", outcome.reason);
  }
  // The turn ends when the model answers in prose, whether or not it called
  // finish. A report over steps still open says so, as the ceiling's does.
  if (steps.length > 0 && openSteps(steps).length === 0) {
    return end(harness, options, "completed", "the trace finished", deliver(outcome.value, steps, notices));
  }
  const summary = deliver(`${outcome.value}\n\n${openSummary(steps)}`, steps, notices);
  return end(harness, options, "completed", "the investigator stopped with steps still open", summary);
}

async function permit(harness: Harness<RootCauseKinds>, options: RootCauseOptions): Promise<RootCauseReport | null> {
  if (!asks(options.spec)) return null;
  const events = await harness.state.read(options.run_id);
  const resolution = events.find(
    (event) => event.kind === "resolution" && (event.payload as { checkpoint_id?: string }).checkpoint_id === PERMIT_ID,
  );
  const answer = resolution === undefined ? undefined : (resolution.payload as { answer?: string; text?: string });
  if (answer?.answer === "approve") return null;
  if (answer?.answer === "reject") {
    return end(harness, options, "failed", answer.text || "the root-cause trace was rejected");
  }

  const raised = events.some(
    (event) => event.kind === "checkpoint" && (event.payload as { checkpoint_id?: string }).checkpoint_id === PERMIT_ID,
  );
  const finding = options.spec.prompt.trim();
  if (!raised) {
    const payload: CheckpointPayload = {
      checkpoint_id: PERMIT_ID,
      checkpoint_class: "hypothesis_approval",
      question: finding === "" ? PERMIT_QUESTION : `${PERMIT_QUESTION}\n\n${finding}`,
      raised_at: new Date().toISOString(),
      context: { finding },
    };
    await append(harness.state, options.run_id, [{ run_id: options.run_id, run_kind: "root_cause", kind: "checkpoint", payload }]);
  }
  await announceOpen(harness.state, options.run_id, "root_cause", PERMIT_ID, options.announce ?? noAnnounce);
  return { status: "waiting_approval", reason: PERMIT_QUESTION, pending: { checkpoint_id: PERMIT_ID, tool: null, args: null } };
}

function asks(spec: RunSpec): boolean {
  const declared = spec.sections["checkpoints"];
  if (declared === null || typeof declared !== "object" || Array.isArray(declared)) return false;
  return (declared as Record<string, unknown>)["hypothesis_approval"] === "ask";
}

async function abortOf(options: RootCauseOptions): Promise<Directive | null> {
  if (options.queue === undefined) return null;
  const pending = await options.queue.pending(options.run_id, []);
  return pending.find((directive) => directive.kind === "abort") ?? null;
}

// Aborts its signal the moment a stop appears in the queue. At most one read in
// flight, so a slow query does not stack ticks behind it.
function watchForAbort(options: RootCauseOptions): { signal: AbortSignal; directive: () => Directive | null; stop: () => void } {
  const halt = new AbortController();
  let found: Directive | null = null;
  let checking = false;
  const poll = setInterval(() => {
    if (checking || options.queue === undefined) return;
    checking = true;
    void abortOf(options)
      .then((directive) => {
        if (directive === null || halt.signal.aborted) return;
        found = directive;
        halt.abort(new Error("an operator stopped the trace"));
      })
      .catch(() => {})
      .finally(() => {
        checking = false;
      });
  }, ABORT_POLL_MS);
  poll.unref?.();
  return { signal: halt.signal, directive: () => found, stop: () => clearInterval(poll) };
}

// The stop goes on the record, then the run ends naming what was still open.
async function stopped(harness: Harness<RootCauseKinds>, options: RootCauseOptions, directive: Directive): Promise<RootCauseReport> {
  await append(harness.state, options.run_id, [
    { run_id: options.run_id, run_kind: "root_cause", kind: "directive", payload: directive },
  ]);
  const events = await harness.state.read(options.run_id);
  const steps = latestSteps(events);
  const reason = directive.text.trim() === "" ? "an operator stopped the trace" : `an operator stopped the trace: ${directive.text.trim()}`;
  return end(harness, options, "aborted", reason, deliver(openSummary(steps), steps, noticeText(events)));
}

async function noticeOnce(harness: Harness<RootCauseKinds>, options: RootCauseOptions): Promise<void> {
  const events = await harness.state.read(options.run_id);
  if (events.some((event) => event.kind === "notice")) return;
  await append(harness.state, options.run_id, [
    { run_id: options.run_id, run_kind: "root_cause", kind: "notice", payload: { text: UNPROVABLE } },
  ]);
}

async function journalRemote(
  harness: Harness<RootCauseKinds>,
  options: RootCauseOptions,
  event: StreamEvent<string>,
): Promise<void> {
  if (event.type !== "tool_result") return;
  if (event.call.tool === "record" || event.call.tool === "finish") return;
  const result = event.attempt.result;
  const payload: DispatchPayload = {
    dispatch_id: `dsp-${event.call.id}`,
    agent_id: "lead",
    status: result.ok ? "complete" : "failed",
    question_id: null,
    failure_reason: result.ok ? null : result.failure.kind,
    result,
    calls: [{ tool: event.call.tool, arguments: event.call.args }],
  };
  await append(harness.state, options.run_id, [{ run_id: options.run_id, run_kind: "root_cause", kind: "dispatch", payload }]);
}

function turnFor(options: RootCauseOptions, task: string) {
  const lead = options.spec.roles.lead;
  if (lead === undefined) throw new SpecError("root cause has no lead");
  const { runtime } = options.spec;
  return {
    run_id: options.run_id,
    run_kind: "root_cause" as const,
    role: "lead",
    system: lead.prompt,
    task,
    schema: lead.output_schema,
    max_turns: runtime.max_turns,
    approvals: new Set(options.spec.approvals),
    verbs: [] as readonly string[],
    result_cap: runtime.result_cap,
    recall_limit: runtime.recall_limit,
    ...(options.signal === undefined ? {} : { signal: options.signal }),
  };
}

async function brief(state: State<RootCauseKinds>, options: RootCauseOptions): Promise<string> {
  const events = await state.read(options.run_id);
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
  const spec = options.spec;
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
  const notice = notices.find((line) => !text.includes(line));
  return redact([notice, text].filter((part) => part).join("\n\n"), steps);
}

async function open(harness: Harness<RootCauseKinds>, options: RootCauseOptions): Promise<void> {
  await append(harness.state, options.run_id, [
    {
      run_id: options.run_id,
      run_kind: "root_cause",
      kind: "run",
      payload: {
        run_kind: "root_cause",
        spec: options.spec,
        budgets: harness.budget.limits,
        seed: options.run_id,
        tenant_id: null,
        started_by: options.started_by ?? "worker",
      },
    },
  ]);
}

async function terminalOf(state: State<RootCauseKinds>, runId: string): Promise<TerminalPayload | null> {
  const events = await state.read(runId);
  const found = events.find((event) => event.kind === "terminal");
  return found === undefined ? null : (found.payload as TerminalPayload);
}

async function end(
  harness: Harness<RootCauseKinds>,
  options: RootCauseOptions,
  outcome: RunOutcome,
  reason: string,
  summary?: string,
): Promise<RootCauseReport> {
  // The backend's backstop may have ended the run while a call was in flight. Its
  // terminal stands; a second one would give the run two endings.
  const ended = await terminalOf(harness.state, options.run_id);
  if (ended !== null) return { status: ended.outcome, reason: ended.reason, pending: null };
  const payload = summary === undefined ? { outcome, reason } : { outcome, reason, summary };
  await append(harness.state, options.run_id, [
    { run_id: options.run_id, run_kind: "root_cause", kind: "terminal", payload },
  ]);
  return { status: outcome, reason, pending: null };
}

function append(state: State<RootCauseKinds>, runId: string, events: readonly Event[]): Promise<number> {
  return state.append(runId, events);
}
