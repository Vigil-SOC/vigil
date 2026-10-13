import { describe, expect, it } from "vitest";
import { pendingCheckpoints, resolutionOf, resolveCheckpoint } from "../../workflows/hunt/checkpoints.js";
import { steer } from "../../workflows/hunt/inbox.js";
import { Journal } from "../../workflows/hunt/journal.js";
import { bookOf, newLedger } from "../support/hunt.js";

// Every hunt tool opens the stored ledger and flushes it (withBook), so what must hold is
// that an approval, once applied, is on the durable record.
describe("an approval nobody can see is an approval that did not happen", () => {
  it("releases the start gate and leaves the answer on the durable ledger", async () => {
    const started = await newLedger({ checkpoints: { hypothesis_approval: "ask" } });
    expect(started.ledger.projection.hunt.status).toBe("pending_approval");
    const [checkpoint] = [...started.ledger.projection.checkpoints.values()];

    // What the console does when the operator presses approve on the gate.
    await steer(started.queue, started.runId, "approve", "start it", { checkpoint_id: checkpoint!.checkpoint_id });

    // The before hook's pass: directives, then whatever answers reached the ledger.
    const book = bookOf(started.ledger);
    expect(await book.applyDirectives()).toBe(false);
    book.reconcile();
    await started.ledger.flush();

    const durable = await Journal.open(started.state, started.queue, started.runId);
    expect(durable.projection.hunt.status).toBe("active");
    expect(pendingCheckpoints(durable.projection)).toHaveLength(0);
    expect(resolutionOf(durable.projection, checkpoint!.checkpoint_id)?.answer).toBe("approve");
  });

  // An answer journaled straight to the ledger (the console's other way in) is
  // reconciled on the next pass, so a resume never asks the operator twice.
  it("reconciles an answer that reached the ledger without a directive", async () => {
    const started = await newLedger({ checkpoints: { hypothesis_approval: "ask" } });
    const [checkpoint] = [...started.ledger.projection.checkpoints.values()];
    started.ledger.append({ kind: "resolution", payload: resolveCheckpoint(checkpoint!, "approve", "analyst", "go") });
    await started.ledger.flush();

    const ledger = await Journal.open(started.state, started.queue, started.runId);
    bookOf(ledger).reconcile();
    expect(ledger.projection.hunt.status).toBe("active");

    // Idempotent: a second pass applies nothing twice.
    const length = ledger.log.length;
    bookOf(ledger).reconcile();
    expect(ledger.log.length).toBe(length);
  });

  it("ends the hunt when the start gate is rejected", async () => {
    const started = await newLedger({ checkpoints: { hypothesis_approval: "ask" } });
    const [checkpoint] = [...started.ledger.projection.checkpoints.values()];
    await steer(started.queue, started.runId, "reject", "wrong premise", { checkpoint_id: checkpoint!.checkpoint_id });

    expect(await bookOf(started.ledger).applyDirectives()).toBe(true);
    expect(started.ledger.projection.hunt.outcome).toBe("aborted");
  });
});
