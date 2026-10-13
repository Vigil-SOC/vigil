import type { PhasePayload } from "./vocabulary.js";
import type { RunOutcome } from "../../contracts/events.js";
import { runAgent, type AgentKinds } from "../../core/agent.js";
import { noAnnounce } from "../../core/checkpoints.js";
import type { Harness, Outcome } from "../../core/loop.js";
import { SpecError, type RunSpec } from "../../core/spec.js";
import { loadPlugin } from "../index.js";
import { checkpointFor, composeHooks } from "./hooks.js";
import { toolsFrom } from "../../tools/remote.js";
import { nullMirror, type Mirror } from "./mirror.js";
import type { ComposeKinds } from "./vocabulary.js";

export { checkpointFor };

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

// Refused before the ledger opens. A step that stops for a human, on a deployment
// with nowhere to ask, parks on its first gate and no answer can ever reach it.
function assertAnswerable(spec: RunSpec, mirror: Mirror): void {
  if (mirror.answerable) return;
  const gated = spec.phases.filter((phase) => phase.approval_required).map((phase) => phase.id);
  if (gated.length > 0) {
    throw new SpecError(`${spec.name} stops for a human at ${gated.join(", ")}, and no mirror is configured to carry a decision back`);
  }
}

// A composed playbook is the compose plugin on the kernel: the kernel opens, parks,
// stops and ends the run; the plugin walks the playbook's phases in order.
export async function runCompose(harness: Harness<ComposeKinds>, options: ComposeOptions): Promise<ComposeReport> {
  const mirror = options.mirror ?? nullMirror;
  assertAnswerable(options.spec, mirror);
  const { manifest } = loadPlugin("compose");
  const report = await runAgent(harness as unknown as Harness<AgentKinds>, {
    run_id: options.run_id,
    run_kind: "compose",
    spec: options.spec,
    plugin: manifest,
    hooks: composeHooks(mirror),
    tools: toolsFrom(options.spec.tools),
    started_by: options.started_by ?? "compose",
    // Python records the decision and the mirror carries it back, so the ledger
    // keeps its single writer. The phase row is how a person is asked.
    answers: (runId) => mirror.decisions(runId),
    announce: noAnnounce,
    ...(options.signal === undefined ? {} : { signal: options.signal }),
  });
  const done = (await harness.state.read(options.run_id)).filter((event) => event.kind === "phase").map((event) => event.payload as PhasePayload);
  return { status: report.status, reason: report.reason, completed: new Set(done.map((one) => one.phase_id)).size, total: options.spec.phases.length, pending: report.pending };
}
