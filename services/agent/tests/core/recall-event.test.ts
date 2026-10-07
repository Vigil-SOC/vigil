import { describe, expect, it } from "vitest";
import { drain, streamTurn } from "../../core/stream.js";
import { prefixBytes, prefixMessages, prefixOf } from "../../core/context.js";
import { emptyRecall, recalledNotesOf, recalledNotes } from "../../contracts/memory.js";
import { nullMemory } from "../../core/memory.js";
import { budgetOf, unmeteredQuota } from "../../core/budget.js";
import { registryOf } from "../../core/registry.js";
import { localDispatch } from "../../core/dispatch.js";
import { InProcessState } from "../../core/state.js";
import { defineTool, type RegisteredTool } from "../../contracts/tool.js";
import type { Memory } from "../../core/seams.js";
import type { Harness, TurnConfig } from "../../core/loop.js";
import { scriptedProvider, type ScriptedProvider, type ScriptedTurn } from "../support/scripted-provider.js";
import { countingMemory, recalledFixture, RECALL_KEYS } from "../support/recalled.js";

const RUN = "5a2c2d3e-0000-4000-8000-000000000737";
const SCHEMA = { type: "object", required: ["verb"], properties: { verb: { type: "string", enum: ["TALLY", "HALT"] } } };
// The emission the schema asks for, which is how a turn answers rather than
// stopping: a `content` turn is prose and the scripted provider refuses it here.
const HALT: ScriptedTurn = { emit: { verb: "HALT" } };
const CALLING: ScriptedTurn = { calls: [{ tool: "bump", args: "{}" }] };
const RECALLED = recalledFixture();

const BUMP = defineTool(
  { id: "bump", description: "bump", parameters: {}, execute: async () => ({ ok: true, rows: [{ n: 1 }], rowCount: 1, capped: false, sourceSystem: "test" }) },
  { maxRows: 10, timeoutMs: 500 },
);

interface Wiring {
  memory?: Memory;
  state?: InProcessState;
  tools?: readonly RegisteredTool[];
}

function harnessOf(script: readonly ScriptedTurn[], wiring: Wiring = {}): Harness {
  return {
    provider: scriptedProvider(script),
    registry: registryOf(wiring.tools ?? [BUMP], { counter: ["bump"] }),
    dispatch: localDispatch,
    budget: budgetOf({ max_calls: 10, max_cost_usd: 100, max_wall_ms: 600_000, max_park_ms: 604_800_000 }, unmeteredQuota),
    memory: wiring.memory ?? nullMemory,
    state: wiring.state ?? new InProcessState(),
  };
}

function config(overrides: Partial<TurnConfig> = {}): TurnConfig {
  return {
    run_id: RUN,
    run_kind: "tally",
    role: "counter",
    system: "count things",
    task: "count to one",
    schema: SCHEMA,
    max_turns: 4,
    approvals: new Set(),
    verbs: ["TALLY", "HALT"],
    result_cap: 4_000,
    recall_limit: 5,
    ...overrides,
  };
}

const withKeys = (over: Partial<TurnConfig> = {}): TurnConfig => config({ recall_keys: RECALL_KEYS, ...over });

// The opening user turn is the whole of what recall reaches: prefixMessages puts
// the rendered notes there and nowhere else.
const openings = (provider: ScriptedProvider): string[] =>
  provider.requests.map((request) => {
    const opening = request.messages[1];
    return opening?.role === "user" ? opening.content : "";
  });

// What the prompt cache is keyed on. Tools are left out because no recalled row
// can move them: prefixOf sorts them and the rendering never reads one.
const rebuiltBytes = (cfg: TurnConfig, log: Parameters<typeof recalledNotesOf>[0]): string =>
  prefixBytes(prefixOf(cfg.system, [], recalledNotesOf(log)));

describe("a run journals the memory it opened on", () => {
  // The run's own start, not the moment the read ran: a resumed run and a replay
  // have to sit inside the same freshness boundary as the first turn did.
  it("asks as of the run's start", async () => {
    const state = new InProcessState();
    const memory = countingMemory();
    const cfg = withKeys();
    // What a workflow writes at seq 0. A turn driven with no ledger behind it is
    // its own start, and there is no earlier stamp to read.
    await state.append(cfg.run_id, [{ run_id: cfg.run_id, run_kind: cfg.run_kind, kind: "run", payload: {} as never }]);
    const opened = (await state.read(cfg.run_id))[0]?.ts;

    await drain(streamTurn(cfg, harnessOf([{ calls: [] }, HALT], { memory, state })));
    expect(memory.asOf()).toBe(opened);
  });

  it("writes one recall event carrying the result verbatim", async () => {
    const state = new InProcessState();
    const memory = countingMemory();
    const harness = harnessOf([CALLING, { calls: [] }, HALT], { memory, state });
    const cfg = withKeys();
    await drain(streamTurn(cfg, harness));

    const recalls = (await state.read(cfg.run_id)).filter((event) => event.kind === "recall");
    expect(recalls).toHaveLength(1);
    expect(recalls[0]?.payload).toEqual(RECALLED);
    expect(memory.reads()).toEqual([[...RECALL_KEYS]]);
  });

  it("shows the model the notes those rows render to", async () => {
    const harness = harnessOf([{ calls: [] }, HALT], { memory: countingMemory() });
    await drain(streamTurn(withKeys(), harness));
    const shown = openings(harness.provider as ScriptedProvider)[0] ?? "";
    for (const note of recalledNotes(RECALLED)) expect(shown).toContain(note);
  });

  it("journals no recall when the run named no keys", async () => {
    const state = new InProcessState();
    const harness = harnessOf([{ calls: [] }, HALT], { memory: countingMemory(), state });
    const cfg = config();
    await drain(streamTurn(cfg, harness));
    expect((await state.read(cfg.run_id)).some((event) => event.kind === "recall")).toBe(false);
  });

  it("journals a read that found nothing, so an empty prefix is a fact and not a gap", async () => {
    const state = new InProcessState();
    const none = emptyRecall(RECALL_KEYS, "2026-03-07T00:00:00.000Z");
    const harness = harnessOf([{ calls: [] }, HALT], { memory: countingMemory(none), state });
    const cfg = withKeys();
    await drain(streamTurn(cfg, harness));

    expect((await state.read(cfg.run_id)).filter((event) => event.kind === "recall")).toHaveLength(1);
    expect(openings(harness.provider as ScriptedProvider)[0]).not.toContain("Recalled from earlier work");
  });
});

// A read that cannot be served is an answer, not an absence: the run opens on
// nothing, says so on its own record, and carries that for the rest of its life.
describe("a read that could not be served", () => {
  const refusing = (detail: string): Memory & { reads: () => number } => {
    let reads = 0;
    return {
      reads: () => reads,
      recall: async () => [],
      entities: async () => {
        reads += 1;
        throw new Error(detail);
      },
      remember: async () => {},
    };
  };

  it("journals why, rather than an empty result", async () => {
    const state = new InProcessState();
    const harness = harnessOf([{ calls: [] }, HALT], { memory: refusing("the endpoint answered 503"), state });
    const cfg = withKeys();
    await drain(streamTurn(cfg, harness));

    const [event] = (await state.read(cfg.run_id)).filter((one) => one.kind === "recall");
    expect(event?.payload).toMatchObject({ keys: [...RECALL_KEYS], unavailable: "the endpoint answered 503" });
    expect(recalledNotesOf(await state.read(cfg.run_id))).toEqual([]);
  });

  it("does not fail the run: memory reorders and never decides", async () => {
    const harness = harnessOf([{ calls: [] }, HALT], { memory: refusing("no token") });
    const outcome = await drain(streamTurn(withKeys(), harness));
    expect(outcome.status).toBe("completed");
  });

  it("does not ask again on a later turn, so the prefix cannot move", async () => {
    const state = new InProcessState();
    const memory = refusing("the endpoint answered 503");
    const cfg = withKeys();
    for (const _turn of [0, 1]) {
      await drain(streamTurn(cfg, harnessOf([{ calls: [] }, HALT], { memory, state })));
    }

    expect(memory.reads()).toBe(1);
    expect((await state.read(cfg.run_id)).filter((one) => one.kind === "recall")).toHaveLength(1);
  });

  it("shows the model no recalled rows at all", async () => {
    const harness = harnessOf([{ calls: [] }, HALT], { memory: refusing("no token") });
    await drain(streamTurn(withKeys(), harness));
    expect(openings(harness.provider as ScriptedProvider)[0]).not.toContain("Recalled from earlier work");
  });
});

// A run is many turns and a workflow's run is many of these -- a lead takes a
// fresh turn each iteration -- so a read per turn would move the prefix inside the
// run and query a neighbourhood that has moved since the first turn.
describe("a run recalls once, however many turns it takes", () => {
  it("keeps the cache key identical across the turns of one call", async () => {
    const harness = harnessOf([CALLING, CALLING, HALT], { memory: countingMemory() });
    await drain(streamTurn(withKeys(), harness));
    const sent = openings(harness.provider as ScriptedProvider);
    expect(sent.length).toBeGreaterThan(1);
    expect(new Set(sent).size).toBe(1);
  });

  it("reads the journaled event on the turns after the first", async () => {
    const state = new InProcessState();
    const memory = countingMemory();
    const cfg = withKeys();
    for (const _turn of [0, 1, 2]) {
      await drain(streamTurn(cfg, harnessOf([{ calls: [] }, HALT], { memory, state })));
    }

    expect(memory.reads()).toHaveLength(1);
    expect((await state.read(cfg.run_id)).filter((event) => event.kind === "recall")).toHaveLength(1);
  });

  // A read that found nothing renders no notes, so presence has to be read off the
  // event: rendering alone cannot tell "nobody has looked at this" from "nobody
  // has read memory yet", and the second one re-queries every turn.
  it("does not read again when the first read found nothing", async () => {
    const state = new InProcessState();
    const memory = countingMemory(emptyRecall(RECALL_KEYS, "2026-03-07T00:00:00.000Z"));
    const cfg = withKeys();
    for (const _turn of [0, 1]) {
      await drain(streamTurn(cfg, harnessOf([{ calls: [] }, HALT], { memory, state })));
    }

    expect(memory.reads()).toHaveLength(1);
    expect((await state.read(cfg.run_id)).filter((event) => event.kind === "recall")).toHaveLength(1);
  });

  // What a resume is: the run picks back up against a memory that has moved, and
  // is shown the rows it was given rather than the rows there are now.
  it("re-renders what it was given even when memory has since moved", async () => {
    const state = new InProcessState();
    const cfg = withKeys();
    const first = harnessOf([{ calls: [] }, HALT], { memory: countingMemory(), state });
    await drain(streamTurn(cfg, first));

    const moved = countingMemory(emptyRecall(RECALL_KEYS, "2026-04-01T00:00:00.000Z"));
    const resumed = harnessOf([{ calls: [] }, HALT], { memory: moved, state });
    await drain(streamTurn(cfg, resumed));

    expect(moved.reads()).toEqual([]);
    expect(openings(resumed.provider as ScriptedProvider)[0]).toBe(openings(first.provider as ScriptedProvider)[0]);
  });
});

describe("the replay rebuilds the prefix from the ledger", () => {
  it("rebuilds the bytes the model was shown, and reads memory no further", async () => {
    const state = new InProcessState();
    const memory = countingMemory();
    const harness = harnessOf([{ calls: [] }, HALT], { memory, state });
    const cfg = withKeys();
    await drain(streamTurn(cfg, harness));

    const log = await state.read(cfg.run_id);
    const rebuilt = prefixOf(cfg.system, [], recalledNotesOf(log));
    expect(prefixMessages(rebuilt, cfg.task)[1]?.content).toBe(openings(harness.provider as ScriptedProvider)[0]);
    expect(memory.reads()).toHaveLength(1);
  });

  it("rebuilds the same cache key when the ranking parameters have since moved", async () => {
    const state = new InProcessState();
    const harness = harnessOf([{ calls: [] }, HALT], { memory: countingMemory(), state });
    const cfg = withKeys();
    await drain(streamTurn(cfg, harness));

    const log = await state.read(cfg.run_id);
    const drifted = log.map((event) =>
      event.kind === "recall"
        ? { ...event, payload: recalledFixture({ ranking: { order: "salience", per_key_cap: 1, overall_cap: 1 } }) }
        : event,
    );
    expect(rebuiltBytes(cfg, drifted)).toBe(rebuiltBytes(cfg, log));
  });

  // The drift the contract's own comment names: a payload missing a key renders as
  // an entity nobody has looked at, which is true of every entity, so nothing
  // looks wrong. Read as absent instead, which a caller can see.
  it("presents nothing at all for an event it cannot read", async () => {
    const state = new InProcessState();
    const harness = harnessOf([{ calls: [] }, HALT], { memory: countingMemory(), state });
    const cfg = withKeys();
    await drain(streamTurn(cfg, harness));

    const log = await state.read(cfg.run_id);
    const { verdicts: _dropped, ...missing } = RECALLED;
    const drifted = log.map((event) => (event.kind === "recall" ? { ...event, payload: missing } : event));
    expect(recalledNotesOf(drifted)).toEqual([]);
  });
});
