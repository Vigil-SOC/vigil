// Checks that sanitising the ledgers did not change what the folds read.
//
// A byte diff is the wrong instrument here. entityViews sorts every entity by bare
// value across types and then slices, so pseudonyms -- which cannot sort where the
// originals did -- legitimately change `entities` and `pivot_candidates`. A byte
// comparison would fail on that every run and prove nothing about the rest.
//
// So this asserts the invariants instead. A 1:1 substitution must leave the entity
// graph's shape identical: the same multiset of node counts, the same multiset of
// pair counts, the same first-sighting per record. Those three are what
// hasRarePairing and introducedRecurring are computed from, and therefore what
// salienceFloor is -- and salience drives compression, which drives the resurface
// sample. Everything else in the fold must be equal outright once the two
// value-ordered digest fields are set aside.
//
// Usage: npx tsx scripts/check-sanitised-folds.ts --real <dir> --sanitised <dir>

import { gunzipSync } from "node:zlib";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fold } from "../plugins/hunt/ledger.js";
import { buildEntityGraph } from "../plugins/hunt/entities.js";
import { evidenceStrength } from "../plugins/hunt/strength.js";
import { buildReport } from "../plugins/hunt/report.js";
import { asHarnessEvents } from "../tests/support/historical.js";
import type { Entity, EvidenceRecord } from "../plugins/hunt/types.js";

const read = (dir: string, name: string): string => gunzipSync(readFileSync(join(dir, name))).toString("utf8");

const runsIn = (dir: string): string[] =>
  readdirSync(dir)
    .filter((name) => name.endsWith(".jsonl.gz"))
    .map((name) => name.replace(".jsonl.gz", ""))
    .sort();

const ordered = (projection: ReturnType<typeof fold>): EvidenceRecord[] =>
  [...projection.evidence.values()].sort((a, b) =>
    a.captured_at === b.captured_at ? a.evidence_id.localeCompare(b.evidence_id) : a.captured_at.localeCompare(b.captured_at),
  );

// Shape, not identity: counts and pairings sorted, so a renaming is invisible and a
// structural change is not.
const shapeOf = (projection: ReturnType<typeof fold>): unknown => {
  const records = ordered(projection);
  const graph = buildEntityGraph(records, projection.hunt.scope["entity"] as Entity | undefined);
  return {
    nodes: graph.nodes().length,
    counts: graph
      .nodes()
      .map((node) => node.count)
      .sort((a, b) => a - b),
    pairs: graph
      .nodes()
      .flatMap((node) => [...node.pairs.values()])
      .sort((a, b) => a - b),
    introduced: records.map((record) => `${record.evidence_id}:${graph.introduced(record.evidence_id).length}`),
    rare: records.map((record) => `${record.evidence_id}:${graph.hasRarePairing(record, 1)}`),
    recurring: records.map((record) => `${record.evidence_id}:${graph.introducedRecurring(record)}`),
  };
};

// Everything the fold derives. The digest went with the controller.
const derivedOf = (projection: ReturnType<typeof fold>): unknown => {
  return {
    strength: Object.fromEntries([...projection.hypotheses.keys()].map((id) => [id, evidenceStrength(projection, id)])),
    report: buildReport(projection),
  };
};

// Ids, timestamps and seeds are carried verbatim, so anything keyed on them must
// match exactly. Free text does not, so it is compared by length rather than value.
const scrubText = (node: unknown): unknown => {
  if (typeof node === "string") return `len:${node.length === 0 ? 0 : 1}`;
  if (Array.isArray(node)) return node.map(scrubText);
  if (node !== null && typeof node === "object")
    return Object.fromEntries(Object.entries(node as Record<string, unknown>).map(([key, value]) => [key, scrubText(value)]));
  return node;
};

const parseArgs = (): { real: string; sanitised: string } => {
  const args = process.argv.slice(2);
  const at = (flag: string): string => {
    const index = args.indexOf(flag);
    if (index === -1 || args[index + 1] === undefined) throw new Error(`missing ${flag}`);
    return args[index + 1] as string;
  };
  return { real: at("--real"), sanitised: at("--sanitised") };
};

const main = (): void => {
  const { real, sanitised } = parseArgs();
  const runs = runsIn(real);
  if (runs.length !== runsIn(sanitised).length) throw new Error("run sets differ");

  let failures = 0;
  for (const run of runs) {
    const before = fold(asHarnessEvents(read(real, `${run}.jsonl.gz`), run));
    const after = fold(asHarnessEvents(read(sanitised, `${run}.jsonl.gz`), run));

    const checks: [string, unknown, unknown][] = [
      ["events", before.evidence.size, after.evidence.size],
      ["graph shape", shapeOf(before), shapeOf(after)],
      ["derived (text by length)", scrubText(derivedOf(before)), scrubText(derivedOf(after))],
    ];

    const bad = checks.filter(([, a, b]) => JSON.stringify(a) !== JSON.stringify(b)).map(([name]) => name);
    failures += bad.length;
    console.log(`${bad.length === 0 ? "ok  " : "FAIL"} ${run}${bad.length === 0 ? "" : `  ${bad.join(", ")}`}`);
  }

  console.log(failures === 0 ? `\nall ${runs.length} runs preserve fold structure` : `\n${failures} check(s) failed`);
  if (failures > 0) process.exitCode = 1;
};

main();
