import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { gunzipSync } from "node:zlib";
import { huntKernelReplay, huntView } from "../plugins/hunt/view.js";
import { investigateKernelReplay, investigateView } from "../plugins/investigate/view.js";

// Writes the projection and replay serve.ts answers for two real kernel ledgers, which the
// web client's KernelRuns and CasePage tests render: a change either side cannot read fails there.

// Run: npx tsx scripts/kernel-view-fixtures.ts

const FIXTURES = join(import.meta.dirname, "..", "tests", "fixtures");
const OUT = join(import.meta.dirname, "..", "..", "..", "clients", "web", "src", "screens", "workflows", "__fixtures__");
const RUN = "run-1";

const lines = (text: string) => text.split("\n").filter((line) => line.trim() !== "").map((line) => JSON.parse(line));
const hunt = lines(gunzipSync(readFileSync(join(FIXTURES, "replay", "kernel-hunt.jsonl.gz"))).toString("utf8")).filter(
  (event) => event.run_id === "7d3c2d3e-0000-4000-8000-0000000c0001",
);
const investigate = lines(readFileSync(join(FIXTURES, "ledgers", "kernel-investigate.jsonl"), "utf8"));
// The same case with its dismissal held by the reviewer guard, and a question left open.
const held = lines(readFileSync(join(FIXTURES, "ledgers", "kernel-investigate-held.jsonl"), "utf8"));

const write = (name: string, value: unknown) => writeFileSync(join(OUT, `${name}.json`), `${JSON.stringify(value, null, 1)}\n`);
write("kernel-hunt-projection", huntView(RUN, hunt));
write("kernel-hunt-replay", huntKernelReplay(RUN, hunt));
write("kernel-investigate-projection", investigateView(RUN, investigate));
write("kernel-investigate-replay", investigateKernelReplay(RUN, investigate));
write("kernel-investigate-held-projection", investigateView(RUN, held));
console.log(`wrote 5 fixtures to ${OUT}`);
