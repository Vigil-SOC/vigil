import { describe, expect, it } from "vitest";
import { archFor } from "../../plugins/kinds.js";
import { InProcessState } from "../../core/state.js";
import { InProcessDirectiveQueue } from "../../plugins/hunt/directives.js";
import type { HuntKinds } from "../../plugins/hunt/ledger.js";
import { runHunt } from "../../plugins/hunt/workflow.js";
import { RUN, specOf, hypothesisIn, modelOf, harnessOf, finding, type Step } from "../support/kernel-hunt.js";

// A hunt that proves an adversary explanation carries the severity its answer gave onto
// the terminal, so the case's priority is raised as an investigation's is (#2562).

async function terminalOf(steps: readonly Step[], answers: Parameters<typeof modelOf>[1]) {
  const state = new InProcessState<HuntKinds>();
  const spec = specOf();
  await runHunt(harnessOf(spec, modelOf(steps, answers), state), { run_id: RUN, run_kind: "hunt", spec, actions: archFor("hunt").actions, queue: new InProcessDirectiveQueue() });
  return (await state.read(RUN)).find((one) => one.kind === "terminal")?.payload;
}

const query: Step = () => ({ tool: "splunk_search", args: { sql: "select * from flows" } });
const record: Step = (request) => ({ tool: "record_evidence", args: { results: [finding("network", hypothesisIn(request))] } });

describe("a hunt's severity on its terminal", () => {
  it("carries the rated severity when the hunt proved an adversary explanation", async () => {
    const terminal = await terminalOf(
      [
        query,
        // Two domains: corroboration a verdict can stand on.
        (request) => ({ tool: "record_evidence", args: { results: [finding("network", hypothesisIn(request)), finding("endpoint", hypothesisIn(request))] } }),
        (request) => ({ tool: "validate_hypothesis", args: { hypothesis_id: hypothesisIn(request) } }),
        () => ({ tool: "conclude_hunt", args: { reason: "the beacon is proven" } }),
      ],
      { conclusion: { disposition: "malicious", citations: ["r1"], severity: "high" } },
    );
    expect(terminal).toMatchObject({ outcome: "completed", severity: "high" });
  });

  it("sends no severity when nothing was proven, whatever the answer rated", async () => {
    const terminal = await terminalOf([query, record, () => ({ say: "done" })], {
      conclusion: { disposition: "suspicious", citations: ["r1"], severity: "critical" },
    });
    expect(terminal).toMatchObject({ outcome: "completed" });
    expect(terminal).not.toHaveProperty("severity");
  });
});
