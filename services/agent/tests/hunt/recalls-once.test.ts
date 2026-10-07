import { describe, expect, it } from "vitest";
import { archFor } from "../../arch/registry.js";
import { InProcessState } from "../../core/state.js";
import { recalledNotes, type RecallResult } from "../../contracts/memory.js";
import { InProcessDirectiveQueue } from "../../workflows/hunt/directives.js";
import type { HuntKinds } from "../../workflows/hunt/journal.js";
import { fold } from "../../workflows/hunt/ledger.js";
import { runHunt } from "../../workflows/hunt/workflow.js";
import { isLead, respondingProvider } from "../support/responding-provider.js";
import { countingMemory, recalledFixture } from "../support/recalled.js";
import { recallHarness, recallHuntSpec, SUBJECT, type Asked } from "../support/recall-hunt.js";

const RUN = "run-recalls-once";
const RECALLED = recalledFixture();

// Concludes on the first decision it is asked for, so the run reaches a terminal
// and the ledger holds every turn the lead took to get there.
const provider = respondingProvider({
  emit: (schema) =>
    isLead(schema) ? { action: "CONCLUDE", rationale: "nothing left to test", evidence_citations: [] } : { results: [] },
  ticks: 0,
});

async function hunt(asked: Asked = {}) {
  const state = new InProcessState<HuntKinds>();
  const spec = recallHuntSpec(asked);
  const memory = countingMemory(RECALLED);
  const report = await runHunt(recallHarness(spec, provider, memory, state), {
    run_id: RUN,
    spec,
    run_kind: "hunt",
    actions: archFor("hunt").actions,
    queue: new InProcessDirectiveQueue(),
  });
  return { report, memory, events: await state.read(RUN) };
}

describe("a hunt reads episodic memory once, on the keys the operator named", () => {
  it("reads on the subjects of the operator's own hypotheses", async () => {
    const { memory } = await hunt();
    expect(memory.reads()).toEqual([[SUBJECT]]);
  });

  it("journals that read as one event on the hunt's ledger", async () => {
    const { events } = await hunt();
    const recalls = events.filter((event) => event.kind === "recall");
    expect(recalls).toHaveLength(1);
    expect(recalls[0]?.payload).toEqual(RECALLED);
  });

  // Every iteration is a fresh turn for the lead, and every one of them reads the
  // journaled event rather than memory.
  it("reads memory once however many turns the hunt takes", async () => {
    const { memory } = await hunt();
    expect(memory.reads()).toHaveLength(1);
  });

  // A scheduled hunt declares no subjects -- the scheduler queues a hypothesis and
  // nothing else -- so the autonomous path would recall nothing at all.
  it("falls back to the entities its hypotheses name", async () => {
    const { memory } = await hunt({ operator: [], hypotheses: ["198.51.100.7 is receiving beacons from the finance segment"] });
    expect(memory.reads()).toEqual([["ip:198.51.100.7"]]);
  });

  it("prefers the subjects an operator declared over anything its prose names", async () => {
    const { memory } = await hunt({ hypotheses: ["203.0.113.9 is also involved"] });
    expect(memory.reads()).toEqual([[SUBJECT]]);
  });

  it("reads nothing when neither names an entity", async () => {
    const { memory, events } = await hunt({ operator: [], hypotheses: ["something is beaconing overnight"] });
    expect(memory.reads()).toEqual([]);
    expect(events.some((event) => event.kind === "recall")).toBe(false);
  });

  it("leaves the projection alone", async () => {
    const { events } = await hunt();
    const withRecall = fold(events);
    const without = fold(events.filter((event) => event.kind !== "recall"));
    expect(withRecall).toEqual(without);
  });

  it("renders what it recalled where the lead reads it", async () => {
    const { events } = await hunt();
    const recall = events.find((event) => event.kind === "recall");
    expect(recalledNotes(recall?.payload as RecallResult)[0]).toContain("scheduled backup target");
  });
});
