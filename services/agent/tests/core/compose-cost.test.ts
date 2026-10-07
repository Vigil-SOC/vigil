import { describe, expect, it } from "vitest";
import { ZERO_TOKENS, type SpendPayload } from "../../contracts/budget.js";
import { InProcessState } from "../../core/state.js";
import { DEFAULT_BUDGETS, DEFAULT_DISPATCH, DEFAULT_RUNTIME, type RunSpec } from "../../core/spec.js";
import { composeProjection } from "../../workflows/compose/projection.js";
import { runCompose } from "../../workflows/compose/workflow.js";
import type { Mirror, TerminalResult } from "../../workflows/compose/mirror.js";
import type { ComposeKinds } from "../../workflows/compose/vocabulary.js";
import { scriptedHarness } from "../support/scripted-harness.js";

// A compose run ended without telling the mirror what it cost, so a finished run
// showed $0.00 in History while its ledger held the spend. Every other kind
// reports the figure from settle(); this one ends through its own end(), where
// the field was absent rather than wrong.

function composeSpecFor(): RunSpec {
  return {
    sections: {},
    model: "scripted",
    budgets: DEFAULT_BUDGETS,
    runtime: DEFAULT_RUNTIME,
    tools: [],
    approvals: [],
    thresholds: {},
    arch: "compose",
    name: "test compose",
    description: "",
    use_case: "",
    trigger_examples: [],
    objectives: [],
    scope: {},
    narrative: "",
    prompt: "",
    phases: [{ id: "only", agent: "reporter", name: "Report", instructions: "report", approval_required: false, tools: [], prompt: "" }],
    roles: { workers: {} },
    dispatch: DEFAULT_DISPATCH,
    digest: {},
  };
}

// Long enough for the step to answer under retry. Every turn answers the same
// way, so the run's shape is the subject and the script is not.
function answering() {
  return scriptedHarness(Array.from({ length: 6 }, () => ({ emit: { summary: "done", handoff: "nothing follows" } })));
}

// Journals one model call's spend the way the stream does, so the fold under test
// reads the rows a real run leaves behind rather than a double's. Appending it
// before the run also opens the ledger, which is what a resumed run finds.
async function spend(state: InProcessState<ComposeKinds>, runId: string, cost_usd: number | null): Promise<void> {
  const payload: SpendPayload = {
    model_id: "scripted/model",
    provider_type: "scripted",
    role: "only",
    tokens: ZERO_TOKENS,
    cost_usd,
    pricing_source: "scripted",
    rates: null,
    fetched_at: null,
  };
  await state.append(runId, [{ run_id: runId, run_kind: "compose", kind: "spend", payload }]);
}

function recordingMirror(): Mirror & { terminals: TerminalResult[] } {
  const terminals: TerminalResult[] = [];
  return {
    answerable: true,
    terminals,
    phase: async () => {},
    terminal: async (_runId, result) => void terminals.push(result),
    handoff: async () => true,
    decisions: async () => [],
  };
}

async function run(runId: string, costs: readonly (number | null)[]): Promise<TerminalResult> {
  const spec = composeSpecFor();
  const state = new InProcessState<ComposeKinds>();
  const mirror = recordingMirror();
  for (const cost of costs) await spend(state, runId, cost);

  const report = await runCompose(answering()("compose", spec, state), { run_id: runId, spec, mirror });
  expect(report.status).toBe("completed");
  expect(mirror.terminals).toHaveLength(1);
  return mirror.terminals[0] as TerminalResult;
}

describe("a compose run's terminal", () => {
  it("carries what the ledger's spend events add up to", async () => {
    const terminal = await run("run-compose-cost", [0.12, 0.0655]);
    expect(terminal.cost_usd).toBeCloseTo(0.1855);
  });

  it("counts a call nobody could price as nothing rather than dropping the total", async () => {
    const terminal = await run("run-compose-unpriced", [null, 0.04]);
    expect(terminal.cost_usd).toBeCloseTo(0.04);
  });

  it("reports zero for a run that spent nothing, rather than leaving the console without a figure", async () => {
    const terminal = await run("run-compose-free", [0]);
    expect(terminal.cost_usd).toBe(0);
  });
});

// A tool a phase named that the deployment lacks is dropped from its grant, and the
// run says so once at open rather than carrying on as though nothing was asked.
describe("a phase tool this deployment lacks", () => {
  const withMissing = (): RunSpec => {
    const spec = composeSpecFor();
    const [phase] = spec.phases;
    return { ...spec, phases: [{ ...(phase as RunSpec["phases"][number]), unavailable: [{ tool: "okta_get_user", reason: "no tool in this deployment answers okta_get_user" }] }] };
  };
  const gaps = async (state: InProcessState<ComposeKinds>) =>
    (await state.read("run-gap")).filter((event) => event.kind === "unbound").map((event) => event.payload);

  it("journals a blind spot at open, once across a resume, and still runs the phase", async () => {
    const state = new InProcessState<ComposeKinds>();
    const spec = withMissing();
    const first = await runCompose(answering()("compose", spec, state), { run_id: "run-gap", spec });
    expect(first.status).toBe("completed");
    expect(await gaps(state)).toEqual([{ capability: "okta_get_user", reason: "Report: no tool in this deployment answers okta_get_user", phase_id: "only" }]);

    await runCompose(answering()("compose", spec, state), { run_id: "run-gap", spec });
    expect(await gaps(state)).toHaveLength(1);
    expect(composeProjection("run-gap", await state.read("run-gap"))).toMatchObject({ run_kind: "compose", unbound: [{ capability: "okta_get_user" }] });
  });
});
