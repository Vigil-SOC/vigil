import { gzipSync } from "node:zlib";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { archFor } from "../arch/registry.js";
import { InProcessState } from "../core/state.js";
import type { Memory } from "../core/seams.js";
import { InProcessDirectiveQueue } from "../workflows/hunt/directives.js";
import type { HuntKinds } from "../workflows/hunt/journal.js";
import { runHunt } from "../workflows/hunt/workflow.js";
import { isLead, respondingProvider } from "../tests/support/responding-provider.js";
import { recalledFixture } from "../tests/support/recalled.js";
import { recallHarness, recallHuntSpec } from "../tests/support/recall-hunt.js";

// Records the fixture the replay gate reads: one hunt, run by current code, whose
// prefix carries recalled rows and whose read is journaled (#737).
//
// Not a Golden and it cannot become one. A Golden is the output of the
// implementation being replaced (ADR 0012), and nothing produces the pre-harness
// file ledger any more -- so this is a regression snapshot of the harness's own
// recall path, and it belongs beside the replay test rather than in
// tests/fixtures/runs, whose population is closed.
//
// Run: npx tsx scripts/record-recall-run.ts

const OUT = join(import.meta.dirname, "..", "tests", "fixtures", "replay");
const NAME = "hunt-recall";
const RUN = "hunt-recall-0001";

// The fixture config's call meter is set for a unit test; a whole hunt with a
// write-up needs room to reach its terminal rather than parking on the ceiling.
const MAX_CALLS = 40;

const memory: Memory = {
  recall: async () => [],
  entities: async () => recalledFixture(),
  remember: async () => {},
};

// Recommends CONCLUDE every iteration and answers the write-up when asked. The run
// still parks on its iteration ceiling: CONCLUDE is a recommendation and the
// controller will not end a hunt while a hypothesis is active, and nothing here
// tests the one the operator asked about -- a targeted INVESTIGATE needs the
// hypothesis id, which the digest hands the lead at runtime and this stand-in
// never reads. So the fixture has the shape eight of the ten historical ledgers
// have: a real run, journaled to the point where it stopped.
const provider = respondingProvider({
  emit: (schema) => {
    if (isLead(schema)) return { action: "CONCLUDE", rationale: "the recalled verdict already settles this", evidence_citations: [] };
    const properties = (schema["properties"] ?? {}) as Record<string, unknown>;
    if ("what_happened" in properties)
      return {
        summary: "the overnight traffic is the backup schedule an earlier case already settled",
        what_happened: "The lead opened on what memory held about the address and concluded without spending a dispatch.",
        next_steps: ["watch for the schedule changing"],
      };
    return { results: [] };
  },
  ticks: 0,
});

const main = async (): Promise<void> => {
  const state = new InProcessState<HuntKinds>();
  const spec = recallHuntSpec({ hypotheses: [], maxCalls: MAX_CALLS });

  const report = await runHunt(recallHarness(spec, provider, memory, state), {
    run_id: RUN,
    spec,
    run_kind: "hunt",
    actions: archFor("hunt").actions,
    queue: new InProcessDirectiveQueue(),
  });

  const events = await state.read(RUN, { snapshots: true });
  const recalls = events.filter((event) => event.kind === "recall");
  if (recalls.length !== 1) throw new Error(`the run journaled ${recalls.length} recall events, not one`);
  // What the fixture is for. The outcome is whatever the run actually reached.
  const decisions = events.filter((event) => event.kind === "decision");
  if (decisions.length === 0) throw new Error("the run made no decision, so nothing read the recalled rows");

  mkdirSync(OUT, { recursive: true });
  const jsonl = events.map((event) => JSON.stringify(event)).join("\n");
  writeFileSync(join(OUT, `${NAME}.jsonl.gz`), gzipSync(Buffer.from(`${jsonl}\n`, "utf8")));
  console.log(
    `${NAME}: ${events.length} events, ${decisions.length} decision(s), ${report.iterations} iteration(s), ${report.status}`,
  );
};

await main();
