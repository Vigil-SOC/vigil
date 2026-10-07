import { describe, expect, it } from "vitest";
import type { CheckpointPayload, ResolutionPayload } from "../../contracts/events.js";
import type { HuntKinds } from "../../workflows/hunt/ledger.js";
import type { InProcessState } from "../../core/state.js";
import { steer } from "../../workflows/hunt/inbox.js";
import type { DecisionProvider } from "../../workflows/hunt/ports.js";
import type { DecisionResult, Digest } from "../../workflows/hunt/types.js";
import { LeadParked } from "../../workflows/hunt/adapters.js";
import { HuntParked } from "../../workflows/hunt/controller.js";
import { CONCLUDE, controllerFor, newLedger } from "../support/hunt.js";

// The Hunt Lead does not read the controller's journal. It runs inside
// core/stream.ts, which folds the *durable* ledger to decide whether the run is
// still answerable, and refuses to emit while a checkpoint is open. So a lead
// stand-in that reads the buffer would agree with the controller no matter what,
// and prove nothing. This one reads what the real one reads.
//
// Faithful to core/stream.ts settled() (raised minus answered, first open wins)
// and to what decisionProvider throws when that fold comes back parked.
function leadOverDurableLedger(state: InProcessState<HuntKinds>, runId: string): DecisionProvider {
  return {
    decide: async (_digest: Digest): Promise<DecisionResult> => {
      const raised: string[] = [];
      const answered = new Set<string>();
      for (const event of await state.read(runId, { since: 0 })) {
        if (event.kind === "checkpoint") raised.push((event.payload as CheckpointPayload).checkpoint_id);
        if (event.kind === "resolution") answered.add((event.payload as ResolutionPayload).checkpoint_id);
      }
      const open = raised.find((id) => !answered.has(id));
      if (open !== undefined) {
        throw new LeadParked(`the ledger holds an open checkpoint, ${open}`);
      }
      return { decision: CONCLUDE as never, model_id: "test", prompt_version: "test", cost_usd: 0 };
    },
  };
}

describe("an approval the lead cannot see is an approval that did not happen", () => {
  it("makes the resolution durable before asking the lead to decide", async () => {
    const started = await newLedger({ checkpoints: { hypothesis_approval: "ask" } });
    const [checkpoint] = [...started.ledger.projection.checkpoints.values()];

    // What the console does when the operator presses approve on the gate.
    await steer(started.queue, started.runId, "approve", "start it", {
      checkpoint_id: checkpoint!.checkpoint_id,
    });

    const controller = controllerFor(started.ledger, [], {
      provider: leadOverDurableLedger(started.state, started.runId),
    });

    // The approval releases the hunt in the same pass, so the lead is asked for a
    // decision inside this call. Before the flush, the resolution was still in the
    // controller's buffer: the lead folded a ledger holding an open checkpoint,
    // refused three times, and the hunt died having spent nothing and proven
    // nothing — with the operator's approval sitting on the record.
    await expect(controller.advanceIteration()).resolves.toBeDefined();
  });

  // The companion to tests/core/resume.test.ts, which already holds this line at
  // the resume seam: "No terminal at all: the run is still answerable, and writing
  // one would have thrown away the answer the operator was in the middle of
  // giving." The decide loop is the other way in, and it treated a park as a dead
  // call — burning the re-ask budget on a condition no re-ask can change, then
  // ending the hunt as failed instead of leaving it answerable.
  it("parks rather than failing when the lead stops on a checkpoint nobody has answered", async () => {
    const started = await newLedger({ checkpoints: { hypothesis_approval: "auto" } });

    // Raised straight into the store, so hunt.status stays active and only the
    // lead's fold sees it: whatever opens a checkpoint the controller's own status
    // does not reflect, the run must stay answerable rather than die.
    await started.state.append(started.runId, [
      {
        run_id: started.runId,
        run_kind: "hunt",
        kind: "checkpoint",
        payload: { checkpoint_id: "cp-rogue", checkpoint_class: "scope_extension", question: "widen to a second index?" },
      } as never,
    ]);

    const controller = controllerFor(started.ledger, [], {
      provider: leadOverDurableLedger(started.state, started.runId),
    });

    await expect(controller.advanceIteration()).rejects.toThrow(HuntParked);
  });
});
