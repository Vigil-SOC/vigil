import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { beforeEach, describe, expect, it } from "vitest";
import { Ledger, type LedgerEvent } from "../ai/ledger.js";
import { HuntController, startHunt } from "../ai/loop.js";
import { replay } from "../ai/replay.js";
import { ScriptedDecisionProvider, ScriptedWorkerDispatcher } from "../ai/scripted.js";
import { buildSpec } from "../ai/spec.js";
import type { Decision } from "../ai/types.js";

const INVESTIGATE: Decision = { action: "INVESTIGATE", rationale: "look", query_intent: "baseline" };

let dir: string;
beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "hunt-"));
});

async function hunt(decisions: Decision[]): Promise<Ledger> {
  const ledger = startHunt(buildSpec({ prompt: "a host is beaconing outbound" }), dir);
  const controller = new HuntController(
    ledger,
    new ScriptedDecisionProvider(decisions),
    new ScriptedWorkerDispatcher(),
  );
  for (const _ of decisions) await controller.advanceIteration();
  return ledger;
}

describe("replay", () => {
  it("rebuilds the digest behind every decision from the ledger prefix", async () => {
    const ledger = await hunt([INVESTIGATE, INVESTIGATE, INVESTIGATE]);
    const report = replay(ledger.log);

    expect(report.decisions).toHaveLength(3);
    expect(report.reproduced).toBe(3);
    // Every decision carries its own boundary, so nothing was inferred.
    expect(report.inexact).toBe(0);
    expect(report.decisions.map((decision) => decision.iteration)).toEqual([1, 2, 3]);
  });

  // The boundary is the point the digest was built, not the decision's own seq:
  // this iteration's dispatches are already on the ledger by the time it lands.
  it("records a digest_seq that precedes the decision event", async () => {
    const ledger = await hunt([INVESTIGATE, INVESTIGATE]);
    const decisions = ledger.log.filter((event) => event.kind === "decision");

    for (const event of decisions) {
      if (event.kind !== "decision") continue;
      expect(event.decision.digest_seq).toBeLessThan(event.seq);
    }
  });

  it("reports a mismatch when the journaled digest does not match the ledger", async () => {
    const ledger = await hunt([INVESTIGATE, INVESTIGATE]);
    const tampered = ledger.log.map((event): LedgerEvent => {
      if (event.kind !== "decision" || event.decision.iteration !== 2) return event;
      return { ...event, digest_presented: { ...event.digest_presented, hunt_name: "not what was presented" } };
    });

    const report = replay(tampered);
    expect(report.reproduced).toBe(1);
    expect(report.decisions[1]!.mismatch).toContain("differs");
  });

  // A rejection note and an EXPAND are appended by the controller after the
  // digest is built, so replay carries them rather than trying to re-derive them.
  it("accepts notes the controller appended to the digest it built", async () => {
    const ledger = await hunt([INVESTIGATE]);
    const carried = ledger.log.map((event): LedgerEvent => {
      if (event.kind !== "decision") return event;
      const digest = {
        ...event.digest_presented,
        notes: [...event.digest_presented.notes, "Your previous emission was rejected: bad."],
      };
      return { ...event, digest_presented: digest };
    });

    expect(replay(carried).reproduced).toBe(1);
  });
});
