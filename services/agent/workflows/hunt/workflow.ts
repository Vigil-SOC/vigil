import type { Announce } from "../../core/checkpoints.js";
import { childOrdinal, runAgent, runChild, type AgentKinds } from "../../core/agent.js";
import type { Harness, TurnConfig } from "../../core/loop.js";
import type { RunKind, RunOutcome, TerminalHandoff } from "../../contracts/events.js";
import type { RunSpec } from "../../core/spec.js";
import { narrativeWriter } from "./adapters.js";
import { narrativeInput, type Narrative } from "./narrative.js";
import { huntSpec, recallKeysOf } from "./config.js";
import { fold, type HuntEvent, type HuntKinds } from "./ledger.js";
import type { HarnessFactory } from "../../harness.js";
import type { State } from "../../core/seams.js";
import type { DirectiveQueue } from "./ports.js";
import { buildReport } from "./report.js";
import { toolsFrom } from "../../tools/remote.js";
import { loadPlugin } from "../../plugins/index.js";
import { huntHooks } from "../../plugins/hunt/hooks.js";
import { stopFrom } from "../rootcause/workflow.js";

export interface HuntOptions {
  run_id: string;
  // The kind the run was started as. "hunt" for a forward hunt, "adjudicate" for a
  // second opinion: the loop is the same, so this only decides how events are stamped.
  run_kind: RunKind;
  spec: RunSpec;
  actions: readonly string[];
  queue: DirectiveQueue;
  started_by?: string;
  announce?: Announce;
  // Told the moment a handoff is journaled, not held back to the terminal. A hunt
  // escalates and keeps hunting, so its terminal can be far off or never come; the
  // case and the root-cause run it tees up should not wait on that. Fail-open --
  // the ledger is the record, and the terminal carries the same handoffs, so a
  // backend that takes these must be idempotent per case.
  //
  // Answers whether the escalation landed. false is retried on the next iteration,
  // which is the only cover a run that never writes a terminal has.
  onHandoff?: (runId: string, handoff: TerminalHandoff) => Promise<boolean>;
  // Told which alerts the hunt's evidence cites, as each iteration lands, so the case
  // the hunt was started on lists them. Fail-open like onHandoff, and answers whether
  // it landed: false is asked again on the next iteration. The backend links a finding
  // once however often it is told.
  onFindings?: (runId: string, findingIds: readonly string[]) => Promise<boolean>;
  // Told what the hunt has spent after each iteration that leaves it going, so a
  // live run's cost follows it and a parked one that was woken reads as running
  // again. Fail-open like onHandoff: the ledger is the record.
  onCost?: (runId: string, cost_usd: number) => Promise<void>;
  // Told which iteration the hunt has finished and the ceiling it runs under, after
  // every iteration, including the one that parks or ends it. Fail-open like onCost.
  onProgress?: (runId: string, iteration: number, max_cost_usd: number) => Promise<void>;
  signal?: AbortSignal;
}

export interface HuntReport {
  status: RunOutcome | "waiting_approval";
  reason: string;
  iterations: number;
}

// A hunt is the hunt plugin on the kernel: one agent keeps the record through its tools,
// the verdicts, directives and ending in its hooks. Kept under this name for every caller.
export async function runHunt(harness: Harness<HuntKinds>, options: HuntOptions): Promise<HuntReport> {
  const id = options.run_kind === "adjudicate" ? "adjudicate" : "hunt";
  const { manifest } = loadPlugin(id);
  const hooks = huntHooks({
    queue: options.queue,
    handoffs: id === "hunt",
    ...(options.onHandoff === undefined ? {} : { onHandoff: options.onHandoff }),
    ...(options.onFindings === undefined ? {} : { onFindings: options.onFindings }),
    ...(options.onCost === undefined ? {} : { onCost: options.onCost }),
    ...(options.onProgress === undefined ? {} : { onProgress: options.onProgress }),
  });
  const report = await runAgent(harness as unknown as Harness<AgentKinds>, {
    run_id: options.run_id,
    run_kind: options.run_kind,
    spec: options.spec,
    plugin: manifest,
    hooks,
    tools: toolsFrom(options.spec.tools),
    started_by: options.started_by ?? "worker",
    // The hunt's own: declared subjects first, else what its statements name.
    recall_keys: recallKeysOf(huntSpec(options.spec)),
    ...(options.announce === undefined ? {} : { announce: options.announce }),
    stop: stopFrom(options.queue, options.run_id),
    ...(options.signal === undefined ? {} : { signal: options.signal }),
  });
  const events = await harness.state.read(options.run_id);
  const opened = events.find((event) => event.kind === "run");
  const iterations = opened === undefined ? 0 : fold(events as readonly HuntEvent[]).hunt.iteration;
  return { status: report.status, reason: report.reason, iterations };
}

// An account of a run, written now, from the spec the run itself was built with --
// not from whatever the arch files say today. Standalone so a caller needs neither
// the lease nor the loop: a finished hunt is the one whose write-up a person most
// wants rewritten, and by then there is nothing left running to ask.
export async function narrateRun(
  state: State<HuntKinds>,
  runId: string,
  events: readonly HuntEvent[],
  build: HarnessFactory,
): Promise<Narrative> {
  const projection = fold(events);
  const spec = projection.hunt.spec;
  // The kind is on the run's own events, so a rewrite stamps the narrative with the
  // same kind the run carried rather than assuming "hunt".
  const runKind: RunKind = events[0]?.run_kind ?? "hunt";
  const harness = build<HuntKinds>(runKind, spec, state);
  // Empty recall_keys: the write-up recalls nothing. It runs after the terminal,
  // over a projection, and a keyed read there would be a read no decision was
  // made on.

  // The narrator is a judge of the run, so it runs as a child of it, on its own ledger.
  const parent = { harness: harness as unknown as Harness<AgentKinds>, options: { run_id: runId, run_kind: runKind } };
  const turn = async <T>(cfg: TurnConfig) => runChild<T>(parent, `${cfg.role}:${await childOrdinal(parent, cfg.role)}`, cfg);
  const narrative = await narrativeWriter({ harness, spec, run_id: runId, run_kind: runKind, actions: [], recall_keys: [], turn }).narrate(
    narrativeInput(projection, buildReport(projection)),
  );
  await state.append(runId, [
    { run_id: runId, run_kind: runKind, kind: "narrative", payload: narrative } as never,
  ]);
  return narrative;
}

