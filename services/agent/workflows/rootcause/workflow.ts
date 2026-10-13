import type { RunOutcome } from "../../contracts/events.js";
import type { Answers } from "../../core/answers.js";
import type { Announce } from "../../core/checkpoints.js";
import { runAgent, type AgentKinds, type StopSource } from "../../core/agent.js";
import type { Harness, Outcome } from "../../core/loop.js";
import { SpecError, type RunSpec } from "../../core/spec.js";
import { loadPlugin } from "../../plugins/index.js";
import { toolsFrom } from "../../tools/remote.js";
import type { DirectiveQueue } from "../hunt/ports.js";
import type { NoticePayload, RootCauseKinds, StepPayload } from "./proof.js";

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

// A root-cause trace is the rootcause plugin on the kernel, the proof in its hooks. Kept
// under this name so every caller and test that drives a trace drives the kernel.
export async function runRootCause(harness: Harness<RootCauseKinds>, options: RootCauseOptions): Promise<RootCauseReport> {
  const { run_id, spec } = options;
  if (spec.roles.lead === undefined) throw new SpecError(`arch ${spec.arch} declares no lead, so it cannot trace a root cause`);
  const { manifest, hooks } = loadPlugin("rootcause");
  const report = await runAgent(harness as unknown as Harness<AgentKinds>, {
    run_id,
    run_kind: "root_cause",
    spec,
    plugin: manifest,
    ...(hooks === undefined ? {} : { hooks }),
    tools: toolsFrom(spec.tools),
    ...(options.started_by === undefined ? {} : { started_by: options.started_by }),
    ...(options.answers === undefined ? {} : { answers: options.answers }),
    ...(options.announce === undefined ? {} : { announce: options.announce }),
    ...(options.queue === undefined ? {} : { stop: stopFrom(options.queue, run_id) }),
    ...(options.signal === undefined ? {} : { signal: options.signal }),
  });
  return { status: report.status, reason: report.reason, pending: report.pending };
}

export function stopFrom(queue: DirectiveQueue, runId: string): StopSource {
  return async () => {
    const found = (await queue.pending(runId, [])).find((directive) => directive.kind === "abort");
    if (found === undefined) return null;
    const { directive_id, actor, kind, text, created_at } = found;
    return { directive_id, actor, kind, text, created_at };
  };
}
