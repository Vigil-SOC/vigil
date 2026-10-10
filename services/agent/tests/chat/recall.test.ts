import { describe, expect, it } from "vitest";
import { archFor } from "../../arch/registry.js";
import { nullMemory, recalling } from "../../core/memory.js";
import type { State } from "../../core/seams.js";
import { InProcessState } from "../../core/state.js";
import { huntNotes } from "../../workflows/hunt/recall.js";
import { bookOf, newLedger, resolve, type Started } from "../support/hunt.js";

const LIMIT = 8;

// A hunt that reached a verdict, which is the state a follow-up asks about.
async function concluded(): Promise<Started> {
  const started = await newLedger({ hypotheses: ["the host is beaconing to external infrastructure"] });
  resolve(started.ledger, started.hypothesisIds[0]!, "proven");
  bookOf(started.ledger).conclude("the hunt reached its verdict");
  await started.ledger.flush();
  return started;
}

describe("what a hunt carries into a later conversation", () => {
  it("recalls how it ended and where each hypothesis landed", async () => {
    const started = await concluded();
    const notes = await huntNotes(started.state, started.runId)(LIMIT);

    expect(notes[0]).toMatch(/^The hunt ".+" ended completed/);
    expect(notes.join("\n")).toContain("the host is beaconing to external infrastructure");
    expect(notes.join("\n")).toContain("is proven");
  });

  it("recalls no evidence, because a follow-up that wants a record queries for it", async () => {
    const started = await concluded();
    const notes = await huntNotes(started.state, started.runId)(LIMIT);
    expect(notes.every((note) => note.startsWith("The hunt") || note.startsWith("Hypothesis"))).toBe(true);
  });

  it("honours the limit, cutting the unsettled hypotheses before the settled ones", async () => {
    const started = await newLedger({ hypotheses: ["settled one", "still open"] });
    resolve(started.ledger, started.hypothesisIds[1]!, "proven");
    await started.ledger.flush();

    // One summary line plus one hypothesis: the resolved one survives the cut.
    const notes = await huntNotes(started.state, started.runId)(2);
    expect(notes).toHaveLength(2);
    expect(notes[1]).toContain("still open");
  });

  it("recalls nothing from a run that has no ledger, rather than failing the conversation", async () => {
    const empty = new InProcessState() as unknown as Parameters<typeof huntNotes>[0];
    expect(await huntNotes(empty, "5a2c2d3e-0000-4000-8000-00000000dead")(LIMIT)).toEqual([]);
  });
});

describe("the registry says which kinds carry anything forward", () => {
  it("gives a hunt a renderer and gives chat none", () => {
    expect(archFor("hunt").notes).toBeTypeOf("function");
    // A conversation recalled into a conversation is the transcript the client
    // already holds, so there is nothing here to carry.
    expect(archFor("chat").notes).toBeUndefined();
  });

  it("reaches the same notes through the registry as through the workflow", async () => {
    const started = await concluded();
    const through = archFor("hunt").notes!(started.state as unknown as State, started.runId);
    expect(await through(LIMIT)).toEqual(await huntNotes(started.state, started.runId)(LIMIT));
  });
});

describe("recalling as a Memory", () => {
  it("ignores the cue, because the caller already named what to recall from", async () => {
    const started = await concluded();
    const memory = recalling(huntNotes(started.state, started.runId));
    expect(await memory.recall("anything at all", LIMIT)).toEqual(await memory.recall("something else", LIMIT));
  });

  it("remembers nothing: the run it reads is over", async () => {
    const started = await concluded();
    const memory = recalling(huntNotes(started.state, started.runId));
    await memory.remember("a new note");
    expect(await memory.recall("", LIMIT)).toEqual(await huntNotes(started.state, started.runId)(LIMIT));
  });

  it("is the null memory's shape, so a chat with no parent is not a special case", async () => {
    expect(await nullMemory.recall("", LIMIT)).toEqual([]);
  });
});
