import { describe, expect, it } from "vitest";
import { runTally, type TallyOptions } from "../../workflows/tally/workflow.js";
import { bumpTool } from "../../workflows/tally/tool.js";
import type { TallyKinds, TallyPayload } from "../../workflows/tally/vocabulary.js";
import { approvalId, type Harness } from "../../core/loop.js";
import { budgetOf, unmeteredQuota } from "../../core/budget.js";
import { registryOf } from "../../core/registry.js";
import { InProcessState } from "../../core/state.js";
import { nullMemory } from "../../core/memory.js";
import { localDispatch } from "../../core/dispatch.js";
import type { AgentEvent, NewEvent } from "../../contracts/events.js";
import type { Budget } from "../../contracts/budget.js";
import type { ToolResult } from "../../contracts/tool.js";
import type { Memory, State, ToolDispatch } from "../../core/seams.js";
import { scriptedProvider, type ScriptedTurn } from "../support/scripted-provider.js";

const RUN = "7d3c2d3e-0000-4000-8000-000000000592";

interface Wiring {
  state?: State<TallyKinds>;
  memory?: Memory;
  dispatch?: ToolDispatch;
  budget?: Budget;
  max_calls?: number;
}

function harnessOf(script: readonly ScriptedTurn[], wiring: Wiring = {}): Harness<TallyKinds> {
  const limits = { max_calls: wiring.max_calls ?? 10, max_cost_usd: 100, max_wall_ms: 600_000, max_park_ms: 604_800_000 };
  return {
    provider: scriptedProvider(script),
    registry: registryOf([bumpTool()], { counter: ["bump"] }),
    dispatch: wiring.dispatch ?? localDispatch,
    budget: wiring.budget ?? budgetOf(limits, unmeteredQuota),
    memory: wiring.memory ?? nullMemory,
    state: wiring.state ?? new InProcessState<TallyKinds>(),
  };
}

function options(overrides: Partial<TallyOptions> = {}): TallyOptions {
  return { run_id: RUN, target: 3, max_turns: 4, ...overrides };
}

function bump(by: number): ScriptedTurn {
  return { calls: [{ tool: "bump", args: JSON.stringify({ by }) }] };
}

function emit(verb: "TALLY" | "HALT", note = "carrying on"): ScriptedTurn {
  return { emit: { verb, note } };
}

const STOP: ScriptedTurn = { calls: [] };

// Two bumps, six model calls, target reached on the second workflow pass.
const TO_THREE: ScriptedTurn[] = [bump(2), STOP, emit("TALLY"), bump(1), STOP, emit("TALLY")];

describe("the throwaway workflow", () => {
  it("runs end to end on the harness and terminates", async () => {
    const state = new InProcessState<TallyKinds>();
    const harness = harnessOf(TO_THREE, { state });
    const report = await runTally(harness, options());

    expect(report.status).toBe("completed");
    expect(report.count).toBe(3);
    // An iteration is a model call, counted by the harness that makes it.
    expect(report.iterations).toBe(6);

    const kinds = (await state.read(RUN)).map((event) => event.kind);
    expect(kinds).toEqual(["run", "spend", "spend", "spend", "tally", "spend", "spend", "spend", "tally", "terminal"]);
    expect(await state.terminal(RUN)).toEqual({ outcome: "completed", reason: "the count reached 3" });
  });

  it("ends on HALT before the target", async () => {
    const harness = harnessOf([bump(1), STOP, emit("HALT", "that is enough")]);
    const report = await runTally(harness, options());

    expect(report.status).toBe("completed");
    expect(report.reason).toBe("that is enough");
    expect(report.count).toBe(1);
  });

  // The workflow's termination is a predicate the model does not control, so a
  // model that never says HALT still ends -- against the harness's budget.
  it("is ended by the budget when the model never stops", async () => {
    const forever = [emit("TALLY"), emit("TALLY"), emit("TALLY")].flatMap((turn) => [STOP, turn]);
    const harness = harnessOf(forever, { max_calls: 2 });
    const report = await runTally(harness, options({ target: 99 }));

    expect(report.status).toBe("budget_exhausted");
    expect(report.iterations).toBe(2);
    expect(report.reason).toContain("calls_exhausted");
  });

  it("parks on a gated tool and goes on once a reviewer answers", async () => {
    const state = new InProcessState<TallyKinds>();
    const approvals = new Set(["bump"]);
    const parked = await runTally(harnessOf([bump(3)], { state }), options({ approvals }));

    expect(parked.status).toBe("waiting_approval");
    expect(parked.pending?.checkpoint_id).toBe(approvalId(RUN, "bump", '{"by":3}'));
    expect(await state.terminal(RUN)).toBeNull();

    await approve(state, parked.pending!.checkpoint_id);
    const resumed = await runTally(harnessOf([bump(3), STOP, emit("TALLY")], { state }), options({ approvals }));

    expect(resumed.status).toBe("completed");
    expect(resumed.count).toBe(3);
  });

  it("reports the count from the rows, not from what the model said about them", async () => {
    const state = new InProcessState<TallyKinds>();
    // The model claims nothing about the number; the ledger takes it from the row.
    await runTally(harnessOf([bump(2), STOP, emit("HALT", "done")], { state }), options());

    const tally = (await state.read(RUN)).find((event) => event.kind === "tally");
    expect((tally?.payload as TallyPayload).count).toBe(2);
  });
});

describe("swapping a seam", () => {
  // Criterion 6: three seams replaced, no line of harness or workflow code
  // changed, and the ledger the run produces is the same one.
  it("changes nothing about the run when the implementations change", async () => {
    const plain = new InProcessState<TallyKinds>();
    await runTally(harnessOf(TO_THREE, { state: plain }), options());

    const inner = new InProcessState<TallyKinds>();
    const counted = counting(inner);
    await runTally(
      harnessOf(TO_THREE, { state: counted.state, memory: recording(), dispatch: remote }),
      options(),
    );

    expect(await bare(counted.state)).toEqual(await bare(plain));
    // And the loop reached the ledger only through the port it was given.
    expect(counted.reads).toBeGreaterThan(0);
    expect(counted.appends).toBeGreaterThan(0);
  });

  // The other half of the same claim: a different budget changes the outcome
  // without a line of the workflow changing, which is what makes it the seam.
  it("is where the budget's answer comes from", async () => {
    const report = await runTally(harnessOf(TO_THREE, { budget: broke() }), options());
    expect(report.status).toBe("budget_exhausted");
    expect(report.count).toBe(0);
  });
});

async function bare(state: State<TallyKinds>): Promise<unknown[]> {
  const events = await state.read(RUN);
  return events.map(({ ts, ...rest }: AgentEvent<TallyKinds>) => rest);
}

async function approve(state: State<TallyKinds>, checkpoint_id: string): Promise<void> {
  const event: NewEvent<TallyKinds> = {
    run_id: RUN,
    run_kind: "tally",
    kind: "resolution",
    payload: { checkpoint_id, actor: "reviewer", answer: "approve", text: "go on", resolved_at: new Date().toISOString() },
  };
  await state.append(RUN, [event]);
}

// A different State implementation rather than a second instance of the same
// one: it delegates, so what it proves is that the loop used only the port.
function counting(inner: State<TallyKinds>) {
  const tally = { reads: 0, appends: 0, state: {} as State<TallyKinds> };
  tally.state = {
    latestSeq: (runId) => inner.latestSeq(runId),
    read: (runId) => {
      tally.reads += 1;
      return inner.read(runId);
    },
    append: (runId, events) => {
      tally.appends += 1;
      return inner.append(runId, events);
    },
    terminal: (runId) => inner.terminal(runId),
  };
  return tally;
}

// Recalls something, unlike the null implementation. Nothing about the run
// changes, which is the point: memory reaches the model and not the ledger.
function recording(): Memory {
  const notes: string[] = ["the count was two yesterday"];
  return {
    recall: async () => notes,
    remember: async (note) => void notes.push(note),
  };
}

// Stands in for an executor on the other side of a wire: everything crosses as
// JSON, which is the reason dispatch is a port rather than a function call.
const remote: ToolDispatch = {
  invoke: async (tool, args) => {
    const sent = JSON.parse(JSON.stringify(args)) as Record<string, unknown>;
    return JSON.parse(JSON.stringify(await tool.invoke(sent))) as ToolResult;
  },
};

function broke(): Budget {
  return {
    limits: { max_calls: 0, max_cost_usd: 0, max_wall_ms: 600_000, max_park_ms: 604_800_000 },
    spent: { calls: 0, cost_usd: 0, tokens: { input: 0, output: 0, cache_read: 0, cache_write: 0 } },
    beginCall: async () => ({ reason: "calls_exhausted", used: 0, limit: 0 }),
    record: () => {},
    raise: () => {},
    priceOf: async () => ({ cost_usd: null, source: null }),
  };
}
