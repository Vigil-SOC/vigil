import { randomUUID } from "node:crypto";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import type { RunJob } from "../../contracts/job.js";
import type { RunPayload } from "../../contracts/events.js";
import { InProcessState } from "../../core/state.js";
import type { HarnessFactory } from "../../harness.js";
import { InProcessDirectiveQueue } from "../../plugins/hunt/directives.js";
import { startHunt } from "../../plugins/hunt/opening.js";
import { advance, principalFor } from "../../worker.js";
import { huntSpecFor } from "../support/hunt.js";
import { InProcessLeases } from "../support/in-process-leases.js";
import { scriptedHarness } from "../support/scripted-harness.js";
import type { ScriptedTurn } from "../support/scripted-provider.js";

const FIXTURES = join(import.meta.dirname, "..", "fixtures");

type StartJob = Extract<RunJob, { reason: "start" }>;

function startJob(id: string, principal?: string): StartJob {
  return {
    schema_version: 1,
    run_id: id,
    run_kind: "investigate",
    tenant_id: null,
    enqueued_at: new Date().toISOString(),
    enqueued_by: "nestor",
    ...(principal === undefined ? {} : { principal }),
    reason: "start",
    request: {
      arch: "",
      playbook: join(FIXTURES, "case.playbook.yaml"),
      config: join(FIXTURES, "case.config.yaml"),
      prompt: "go",
    },
  };
}

const CONCLUDE: ScriptedTurn[] = [
  { calls: [] },
  { calls: [] },
  { emit: { summary: "nothing to pursue", disposition: "inconclusive", confidence: "low", citations: [], gaps: [], next_steps: [], proposed_actions: [] } },
];

describe("a person who started a run stays bound to it", () => {
  it("reads the principal off the job, and off the ledger once the job no longer carries it", async () => {
    const state = new InProcessState();
    const runId = randomUUID();
    await state.append(runId, [
      {
        run_id: runId,
        run_kind: "investigate",
        kind: "run",
        payload: { principal: "from-the-ledger", started_by: "nestor" } as RunPayload,
      },
    ]);
    const resume: RunJob = {
      schema_version: 1,
      run_id: runId,
      run_kind: "investigate",
      tenant_id: null,
      enqueued_at: new Date().toISOString(),
      enqueued_by: "watchdog",
      reason: "resume",
    };

    expect(await principalFor(state, startJob(runId, "from-the-job"))).toBe("from-the-job");
    expect(await principalFor(state, resume)).toBe("from-the-ledger");
    expect(await principalFor(state, startJob(randomUUID()))).toBeUndefined();
  });

  it("journals the principal and hands it to the harness", async () => {
    const runId = randomUUID();
    const seen: (string | undefined)[] = [];
    const build: HarnessFactory = (spec, state, memory, seed, principal) => {
      seen.push(principal);
      return scriptedHarness(CONCLUDE)(spec, state, memory, seed);
    };
    const state = new InProcessState();

    await advance(state, new InProcessLeases(), startJob(runId, "signed-by-the-api"), build, new InProcessDirectiveQueue());

    const [opened] = await state.read(runId);
    expect((opened?.payload as RunPayload).principal).toBe("signed-by-the-api");
    expect(seen).toContain("signed-by-the-api");
  });

  it("records a hunt's principal on the run it opens", async () => {
    const state = new InProcessState();
    const runId = randomUUID();
    await startHunt(state as never, new InProcessDirectiveQueue(), runId, huntSpecFor(), "nestor", "hunt", [], "signed-by-the-api");
    const [opened] = await state.read(runId);
    expect((opened?.payload as { principal?: string }).principal).toBe("signed-by-the-api");
  });
});
