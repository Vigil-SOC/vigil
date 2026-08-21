import { gunzipSync } from "node:zlib";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import type { HuntEvent } from "../../workflows/hunt/ledger.js";

export const RUNS = join(import.meta.dirname, "..", "fixtures", "runs");

// The file ledger's shape, which predates the harness envelope. Each record put
// its body under a key named for its kind rather than under a payload.
interface Historical {
  kind: string;
  seq: number;
  ts: string;
  schema_version: number;
  [key: string]: unknown;
}

const RENAMED: Record<string, string> = { hunt: "run" };

// A patch carried its fields beside the kind; everything else carried one object
// under a key named for the kind.
const FIELDS: Record<string, Record<string, string>> = {
  checkpoint: { class: "checkpoint_class", payload: "context" },
  resolution: { verdict: "answer", reason: "text" },
};

// approved/rejected became approve/reject with the harness payload, so the value
// is translated beside the key it lives under.
const ANSWERS: Record<string, string> = { approved: "approve", rejected: "reject" };

export function renamed(kind: string, body: unknown): unknown {
  const map = FIELDS[kind];
  if (map === undefined || typeof body !== "object" || body === null) return body;
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(body as Record<string, unknown>)) {
    const to = map[key] ?? key;
    out[to] = to === "answer" ? (ANSWERS[String(value)] ?? value) : value;
  }
  return out;
}

function payloadOf(record: Historical): unknown {
  if (record.kind === "patch") return { target: record["target"], id: record["id"], fields: record["fields"] };
  if (record.kind === "hunt") return { hunt: record["hunt"] };
  if (record.kind === "finalize") return record["report"];
  return renamed(record.kind, record[record.kind]);
}

export function asHarnessEvents(text: string, runId: string): HuntEvent[] {
  return text
    .split("\n")
    .filter((line) => line.trim() !== "")
    .map((line) => {
      const record = JSON.parse(line) as Historical;
      return {
        run_id: runId,
        run_kind: "hunt",
        seq: record.seq,
        ts: record.ts,
        kind: RENAMED[record.kind] ?? record.kind,
        payload: payloadOf(record),
        schema_version: record.schema_version,
      } as HuntEvent;
    });
}

export function gunzipped(name: string): string {
  return gunzipSync(readFileSync(join(RUNS, name))).toString("utf8");
}

// The ten real ledgers. Sidecars and the torn file are deliberately not here:
// one is not a ledger and the other is the subject of its own test.
export function historicalRuns(): string[] {
  if (!existsSync(RUNS)) return [];
  return readdirSync(RUNS)
    .filter((name) => name.endsWith(".jsonl.gz"))
    .map((name) => name.replace(".jsonl.gz", ""))
    .sort();
}

const RESOLUTION_KEYS = new Set(["verdict", "reason"]);

// A resolution is renamed wherever it nests; a checkpoint only in the projection,
// since the report keeps its own published field names and those did not move.
function renamedResolutions(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(renamedResolutions);
  if (typeof value !== "object" || value === null) return value;
  const held = value as Record<string, unknown>;
  const source =
    "checkpoint_id" in held && Object.keys(held).some((key) => RESOLUTION_KEYS.has(key))
      ? (renamed("resolution", held) as Record<string, unknown>)
      : held;
  return Object.fromEntries(Object.entries(source).map(([key, one]) => [key, renamedResolutions(one)]));
}

export function renamedGolden(golden: Record<string, unknown>): Record<string, unknown> {
  const walked = renamedResolutions(golden) as Record<string, unknown>;
  const checkpoints = walked["checkpoints"];
  if (typeof checkpoints !== "object" || checkpoints === null || Array.isArray(checkpoints)) return walked;
  return {
    ...walked,
    checkpoints: Object.fromEntries(
      Object.entries(checkpoints as Record<string, unknown>).map(([id, one]) => [id, renamed("checkpoint", one)]),
    ),
  };
}
