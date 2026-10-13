import { gunzipSync } from "node:zlib";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import type { CallPayload, ChildPayload, ConclusionPayload, RunPayload } from "../../contracts/events.js";
import { recordOf } from "../../core/record.js";
import { replayFrames, trajectoryOf } from "../../core/replay.js";
import { fold, type HuntEvent } from "../../workflows/hunt/ledger.js";

// The kernel replay gate: a recorded kernel hunt and its two child runs, each call's input
// rebuilt from those ledgers alone and diffed against the hash it was sent with.

// It replaces the Digest diff: a kernel run is replayed call by call, not digest by digest.

const FIXTURE = join(import.meta.dirname, "..", "fixtures", "replay", "kernel-hunt.jsonl.gz");
const RUN = "7d3c2d3e-0000-4000-8000-0000000c0001";

const ALL = gunzipSync(readFileSync(FIXTURE))
  .toString("utf8")
  .split("\n")
  .filter((line) => line.trim() !== "")
  .map((line) => JSON.parse(line) as HuntEvent);

const ledger = (runId: string): HuntEvent[] => ALL.filter((event) => event.run_id === runId);
const HUNT = ledger(RUN);
const CHILDREN = HUNT.filter((event) => event.kind === "child").map((event) => event.payload as unknown as ChildPayload);

describe("the recorded kernel hunt replays from its ledgers alone", () => {
  it("rebuilds every model call the hunt made to the input it was sent", () => {
    const replayed = replayFrames(HUNT);
    expect(replayed.frames.length).toBeGreaterThan(5);
    expect(replayed.frames.filter((frame) => frame.mismatch !== null)).toEqual([]);
    expect(replayed.missing).toEqual([]);
  });

  it("finds the recall each call was shown is the one the run journaled", () => {
    expect(HUNT.some((event) => event.kind === "recall")).toBe(true);
    expect(replayFrames(HUNT).recall_matches).toBe(true);
  });

  it("rebuilds the critic and the narrator from their own ledgers, linked to the hunt", () => {
    expect(CHILDREN.map((child) => child.role).sort()).toEqual(["critic", "narrator"]);
    for (const child of CHILDREN) {
      const events = ledger(child.child_run_id);
      expect((events[0]?.payload as unknown as RunPayload).parent).toMatchObject({ run_id: RUN, run_kind: "hunt", role: child.role });
      const replayed = replayFrames(events);
      expect(replayed.frames.length).toBeGreaterThan(0);
      expect(replayed.frames.filter((frame) => frame.mismatch !== null)).toEqual([]);
    }
  });

  it("carries the record, the classified calls and the declared gaps a verdict is read from", () => {
    const calls = HUNT.filter((event) => event.kind === "call").map((event) => event.payload as unknown as CallPayload);
    expect(calls.find((call) => call.tool === "splunk_search")?.class).toBe("visibility_gap");
    const record = recordOf(HUNT);
    expect([...record.hypotheses.values()].some((one) => one.provenance === "agent")).toBe(true);
    expect([...record.questions.values()].some((one) => one.question === "is the sync client on the approved list?")).toBe(true);
    const conclusion = HUNT.find((event) => event.kind === "conclusion")?.payload as unknown as ConclusionPayload;
    expect(conclusion.declared_gaps?.visibility.map((gap) => gap.tool)).toEqual(["splunk_search"]);
    expect(conclusion.declared_gaps?.declared).toEqual(["is the sync client sanctioned?"]);
  });

  it("still folds to a hunt projection, so every reader of a hunt reads it", () => {
    const view = fold(HUNT);
    expect(view.hunt.status).toBe("terminal");
    expect([...view.hypotheses.values()].some((one) => one.status === "proven")).toBe(true);
  });

  it("hands a learner a faithful trajectory", () => {
    const trajectory = trajectoryOf(RUN, HUNT);
    expect(trajectory.faithful).toBe(true);
    expect(trajectory.children).toHaveLength(2);
    expect(trajectory.steps.flatMap((step) => step.actions).some((action) => action.class === "visibility_gap")).toBe(true);
  });
});
