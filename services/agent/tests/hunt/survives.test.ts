import { describe, expect, it } from "vitest";
import { join } from "node:path";
import { archFor } from "../../arch/registry.js";
import { budgetOf, FRESH, unmeteredQuota } from "../../core/budget.js";
import { localDispatch } from "../../core/dispatch.js";
import { GatewayExhausted } from "../../core/limiter.js";
import type { Harness } from "../../core/loop.js";
import { nullMemory } from "../../core/memory.js";
import type { Provider, ProviderEvent, TurnRequest } from "../../core/provider.js";
import { registryOf } from "../../core/registry.js";
import { buildSpec, type RunSpec } from "../../core/spec.js";
import { InProcessState } from "../../core/state.js";
import { InProcessDirectiveQueue } from "../../workflows/hunt/directives.js";
import type { HuntKinds } from "../../workflows/hunt/journal.js";
import { runHunt } from "../../workflows/hunt/workflow.js";

const FIXTURES = join(import.meta.dirname, "..", "fixtures");

function huntSpec(): RunSpec {
  const entry = archFor("hunt");
  return buildSpec(
    { arch: entry.arch, playbook: join(FIXTURES, "hunt.playbook.yaml"), config: join(FIXTURES, "hunt.config.yaml") },
    entry.actions,
  );
}

// Every call dies, which is the shape a gateway at its ceiling presents: the lead
// cannot decide, so the hunt cannot advance a single iteration.
function dyingProvider(error: () => Error): Provider {
  return {
    model: "m",
    provider_type: "test",
    // eslint-disable-next-line require-yield
    async *stream(_request: TurnRequest): AsyncGenerator<ProviderEvent> {
      throw error();
    },
  };
}

async function run(error: () => Error) {
  const state = new InProcessState<HuntKinds>();
  const spec = huntSpec();
  const harness: Harness<HuntKinds> = {
    provider: dyingProvider(error),
    registry: registryOf([], {}),
    dispatch: localDispatch,
    budget: budgetOf(spec.budgets, unmeteredQuota, Date.now, FRESH),
    memory: nullMemory,
    state,
  };
  const report = await runHunt(harness, {
    run_id: "run-dying",
    spec,
    actions: archFor("hunt").actions,
    queue: new InProcessDirectiveQueue(),
  });
  return { report, terminal: await state.terminal("run-dying"), events: await state.read("run-dying") };
}

// Rethrown, this ended the run through the worker with no finalize and no summary: the
// console showed a failed hunt holding nothing while the ledger held every hypothesis
// it had opened. The ending is a fact about the run; what it found is a separate one.
describe("a hunt whose model calls all die", () => {
  it("ends on a terminal that carries the report, rather than escaping", async () => {
    const { report, terminal, events } = await run(() => new Error("504 request timed out"));

    expect(report.status).toBe("failed");
    expect(terminal?.outcome).toBe("failed");
    expect(terminal?.reason).toContain("504");
    // The half that used to be lost: what the hunt was testing, written down.
    expect(terminal?.summary ?? "").not.toBe("");
    expect(events.some((event) => event.kind === "finalize")).toBe(true);
  });

  // The gateway's own allowance is the one ending a reader may want to tell apart from
  // a defect, and it is not this hunt failing.
  it("reports the gateway's exhausted budget as a budget, not as a failure", async () => {
    const { report, terminal } = await run(() => new GatewayExhausted("no credit"));

    expect(report.status).toBe("budget_exhausted");
    expect(terminal?.outcome).toBe("budget_exhausted");
    expect(terminal?.summary ?? "").not.toBe("");
  });

  // The report is written from the projection, and an ending the controller never
  // chose left that projection untouched: the deliverable on a failed run read
  // "not terminated / still running" with every hypothesis active, over a run the
  // API had already reported ended.
  it("writes a report that says the hunt ended, not that it is still running", async () => {
    const { terminal } = await run(() => new Error("504 request timed out"));
    const summary = terminal?.summary ?? "";

    expect(summary).toContain("**Outcome:** failed");
    expect(summary).not.toContain("still running");
    expect(summary).not.toMatch(/^### h-.* — active$/m);
  });
});
