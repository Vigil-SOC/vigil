import type { RunOutcome } from "../../contracts/events.js";
import { runAgent, type AgentKinds } from "../../core/agent.js";
import type { Harness, Outcome } from "../../core/loop.js";
import { DEFAULT_BUDGETS, DEFAULT_DISPATCH, type RunSpec } from "../../core/spec.js";
import { loadPlugin } from "../../plugins/index.js";
import type { TallyKinds, TallyPayload } from "./vocabulary.js";

export interface TallyOptions {
  run_id: string;
  target: number;
  max_turns: number;
  approvals?: ReadonlySet<string>;
  started_by?: string;
}

export interface TallyReport {
  status: RunOutcome | "waiting_approval";
  reason: string;
  count: number;
  iterations: number;
  pending: Outcome<unknown>["pending"];
}

// The tally plugin on the kernel. Nothing here reaches a model or a tool except
// through the harness, which is what the conformance run exists to show.
export async function runTally(harness: Harness<TallyKinds>, options: TallyOptions): Promise<TallyReport> {
  const { manifest, hooks } = loadPlugin("tally");
  const report = await runAgent(harness as unknown as Harness<AgentKinds>, {
    run_id: options.run_id,
    run_kind: "tally",
    spec: specOf(options),
    plugin: manifest,
    ...(hooks === undefined ? {} : { hooks }),
    tools: [],
    started_by: options.started_by ?? "tally",
  });
  const events = await harness.state.read(options.run_id);
  const last = events.filter((event) => event.kind === "tally").at(-1);
  return {
    status: report.status,
    reason: report.reason,
    count: last === undefined ? 0 : (last.payload as TallyPayload).count,
    // An iteration is a model call, counted by the harness that makes it.
    iterations: harness.budget.spent.calls,
    pending: report.pending,
  };
}

// No playbook and no config: a conformance run is the target and the turn cap.
function specOf(options: TallyOptions): RunSpec {
  return {
    sections: { target: options.target },
    model: "",
    budgets: DEFAULT_BUDGETS,
    runtime: { max_turns: options.max_turns, result_cap: 2_000, recall_limit: 3 },
    tools: [],
    approvals: [...(options.approvals ?? [])],
    thresholds: {},
    arch: "tally",
    roles: { workers: {} },
    dispatch: DEFAULT_DISPATCH,
    digest: {},
    name: "tally",
    description: "",
    use_case: "",
    trigger_examples: [],
    objectives: [],
    scope: {},
    phases: [],
    narrative: `The target is ${options.target}.`,
    prompt: "",
  };
}
