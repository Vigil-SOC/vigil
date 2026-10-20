import { describe, expect, it } from "vitest";
import { archFor } from "../../plugins/kinds.js";
import type { ReviewPayload } from "../../contracts/events.js";
import { InProcessState } from "../../core/state.js";
import { InProcessDirectiveQueue } from "../../plugins/hunt/directives.js";
import type { HuntKinds } from "../../plugins/hunt/ledger.js";
import { huntProjection } from "../../plugins/hunt/projection.js";
import { runHunt } from "../../plugins/hunt/workflow.js";
import { huntView } from "../../plugins/hunt/view.js";
import { RUN, specOf, hypothesisIn, modelOf, harnessOf, finding, type Step } from "../support/kernel-hunt.js";

// The hunt's conclusion says a disposition, and the reviewer guard holds a dismissal to a
// cited result the record links to a hypothesis, as an investigation's is held.

async function hunt(steps: readonly Step[], answers: Parameters<typeof modelOf>[1], kind: "hunt" | "adjudicate" = "hunt") {
  const state = new InProcessState<HuntKinds>();
  const spec = specOf(kind);
  await runHunt(harnessOf(spec, modelOf(steps, answers), state), { run_id: RUN, run_kind: kind, spec, actions: archFor(kind).actions, queue: new InProcessDirectiveQueue() });
  return state.read(RUN);
}

const query: Step = () => ({ tool: "splunk_search", args: { sql: "select * from flows" } });
const record: Step = (request) => ({ tool: "record_evidence", args: { results: [finding("network", hypothesisIn(request))] } });
const stop: Step = () => ({ say: "done" });

describe("a hunt's disposition on its projection head", () => {
  it("stands on a benign conclusion that cites a served result the record links, once a reviewer upholds it", async () => {
    const events = await hunt([query, record, stop], { conclusion: { disposition: "benign", citations: ["r1"] } });
    const view = huntView(RUN, events as never);
    const review = events.find((one) => one.kind === "review")?.payload as ReviewPayload;
    expect(review).toMatchObject({ stance: "uphold", cited: ["r1"], grounded: true, guarded: { asked: "benign", final: "benign" } });
    expect(view).toMatchObject({ disposition: "benign", held: null });
  });

  it("holds a benign conclusion for review when the reviewer does not uphold it, and says what was asked", async () => {
    const events = await hunt([query, record, stop], {
      conclusion: { disposition: "benign", citations: ["r1"] },
      review: { stance: "insufficient", reasons: "one source is not two", missing: ["the owner"] },
    });
    expect(huntView(RUN, events as never)).toMatchObject({
      disposition: "needs_review",
      held: { field: "disposition", asked: "benign", final: "needs_review", reason: "the review was insufficient: one source is not two" },
    });
  });

  it("holds an upheld dismissal that cites nothing, or cites a result no record links", async () => {
    const uncited = await hunt([query, record, stop], { conclusion: { disposition: "false_positive", citations: [] } });
    expect(huntView(RUN, uncited as never)).toMatchObject({ disposition: "needs_review", held: { asked: "false_positive" } });
    // r3 was asked after the last record, so no record rests on it yet.
    const unlinked = await hunt([query, record, query, stop], { conclusion: { disposition: "benign", citations: ["r3"] } });
    expect((unlinked.find((one) => one.kind === "review")?.payload as ReviewPayload).grounded).toBe(false);
    expect(huntView(RUN, unlinked as never)).toMatchObject({ disposition: "needs_review" });
    const bare = await hunt([query, stop], { conclusion: { disposition: "benign", citations: ["r1"] } });
    expect((bare.find((one) => one.kind === "review")?.payload as ReviewPayload).grounded).toBe(false);
    expect(huntView(RUN, bare as never)).toMatchObject({ disposition: "needs_review", held: { reason: "no cited result is linked to a claim on the record" } });
  });

  it("does not let a dismissal stand that no review saw", async () => {
    const events = await hunt([query, record, stop], { conclusion: { disposition: "benign", citations: ["r1"] } });
    const unreviewed = events.filter((one) => one.kind !== "review");
    expect(huntView(RUN, unreviewed as never)).toMatchObject({ disposition: "needs_review", held: null });
  });

  it("passes a malicious conclusion through unchanged", async () => {
    const events = await hunt([query, record, stop], { conclusion: { disposition: "malicious", citations: ["r1"] } });
    expect(huntView(RUN, events as never)).toMatchObject({ disposition: "malicious", held: null });
  });

  it("reads the word off a hunt that concluded by its own tool, then answered", async () => {
    const events = await hunt(
      [
        query,
        record,
        (request) => ({ tool: "validate_hypothesis", args: { hypothesis_id: hypothesisIn(request) } }),
        () => ({ tool: "conclude_hunt", args: { reason: "the beacon is proven" } }),
      ],
      { conclusion: { disposition: "malicious", citations: ["r1"] } },
    );
    expect(huntView(RUN, events as never)).toMatchObject({ disposition: "malicious", outcome: "completed" });
  });

  it("is null for a hunt that concluded nothing, and for a conclusion with no disposition", async () => {
    const events = await hunt([query, record, stop], { conclusion: { disposition: "malicious", citations: [] } });
    const unanswered = events.filter((one) => one.kind !== "conclusion" && one.kind !== "review");
    expect(huntView(RUN, unanswered as never).disposition).toBeNull();
    expect(huntProjection(RUN, events as never).disposition).toBeNull();
    const adjudicated = await hunt([query, record, stop], {}, "adjudicate");
    expect(huntView(RUN, adjudicated as never)).toMatchObject({ disposition: null, held: null });
  });
});
