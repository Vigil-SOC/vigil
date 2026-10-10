import { gzipSync } from "node:zlib";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { archFor } from "../plugins/kinds.js";
import type { ChildPayload } from "../contracts/events.js";
import { InProcessState } from "../core/state.js";
import { InProcessDirectiveQueue } from "../plugins/hunt/directives.js";
import type { HuntKinds } from "../plugins/hunt/ledger.js";
import { runHunt } from "../plugins/hunt/workflow.js";
import type { ToolResult } from "../contracts/tool.js";
import type { Memory, ToolDispatch } from "../core/seams.js";
import { harnessOf, hypothesisIn, modelOf, specOf, type Step } from "../tests/support/kernel-hunt.js";
import { recalledFixture, RECALL_KEYS } from "../tests/support/recalled.js";

// Records the kernel replay gate's fixture: one kernel hunt with a keyed recall, a gap,
// the record kept and two child runs, every ledger it wrote in one file.

// Frozen, so an input change a replay cannot follow fails the gate rather than re-recording.
// Run: npx tsx scripts/record-kernel-replay.ts

const OUT = join(import.meta.dirname, "..", "tests", "fixtures", "replay");
const NAME = "kernel-hunt";
const RUN = "7d3c2d3e-0000-4000-8000-0000000c0001";

const memory: Memory = {
  recall: async () => [],
  entities: async () => recalledFixture(),
  remember: async () => {},
};

// Documentation addresses only (RFC 5737): the fixture is committed, and swept.
const HOST = "192.0.2.10";
const PEER = "198.51.100.7";

const estate: ToolDispatch = {
  invoke: async (tool, args): Promise<ToolResult> => {
    if (tool.local) return tool.invoke(args);
    if (tool.id === "splunk_search") return { ok: false, failure: { kind: "unavailable", detail: "splunk_search is down" } };
    return { ok: true, rows: [{ src: HOST, dst: PEER, interval_s: 300, connections: 412 }], rowCount: 1, capped: false, sourceSystem: tool.id };
  },
};

const finding = (source: string, hypothesis: string) => ({
  source_system: source,
  summary: `every 300s +/- 4s over 6 hours, 412 connections from ${HOST} to ${PEER} (${source})`,
  salience: "anomalous",
  why_notable: "the jitter is too low for a person or a poller",
  payload: JSON.stringify({ interval_s: 300, connections: 412 }),
  supports: [hypothesis],
  rests_on: [{ field: "interval_s", authored: "sensor" }],
});

const steps: Step[] = [
  () => ({ tool: "load_skill", args: { name: "hunt-method" } }),
  () => ({ tool: "splunk_search", args: { sql: "select * from flows where src = '192.0.2.10'" } }),
  () => ({ tool: "search_findings", args: { query: "198.51.100.7" } }),
  (request) => ({ tool: "record_evidence", args: { results: [finding("network", hypothesisIn(request)), finding("endpoint", hypothesisIn(request))] } }),
  () => ({ tool: "propose_hypothesis", args: { statement: "192.0.2.10 runs an unsanctioned sync client" } }),
  () => ({ tool: "open_question", args: { question: "is the sync client on the approved list?" } }),
  (request) => ({ tool: "validate_hypothesis", args: { hypothesis_id: hypothesisIn(request) } }),
  () => ({ tool: "conclude_hunt", args: { reason: "the beacon is proven" } }),
];

async function main(): Promise<void> {
  const base = specOf("hunt", [`${HOST} is beaconing to ${PEER}`]);
  const spec = { ...base, sections: { ...base.sections, recall_keys: [...RECALL_KEYS] } };
  const state = new InProcessState<HuntKinds>();
  const report = await runHunt(harnessOf(spec, modelOf(steps), state, estate, memory), {
    run_id: RUN,
    run_kind: "hunt",
    spec,
    actions: archFor("hunt").actions,
    queue: new InProcessDirectiveQueue(),
  });

  const parent = await state.read(RUN);
  const children = parent.filter((event) => event.kind === "child").map((event) => (event.payload as ChildPayload).child_run_id);
  const all = [...parent, ...(await Promise.all(children.map((id) => state.read(id)))).flat()];
  mkdirSync(OUT, { recursive: true });
  writeFileSync(join(OUT, `${NAME}.jsonl.gz`), gzipSync(all.map((event) => JSON.stringify(event)).join("\n") + "\n"));
  console.log(`${report.status}: ${parent.length} events on the hunt, ${children.length} child runs, ${all.length} in all`);
}

await main();
