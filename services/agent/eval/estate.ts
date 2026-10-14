import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { parse as parseYaml } from "yaml";

// A labelled eval case and its estate: each call is answered from the case file (first
// matching response, then the tool's default, then empty). Telemetry is the exception: its
// rows form one events table the call's SQL runs over. Nothing reaches a real system.

// Typed structurally, so the same file drives the kernel and the engine it replaced.

export type Truth = "attack" | "benign";

// What the case is built to catch. blind: the decisive source cannot answer, or its search
// is empty. injection: a result steers the analyst. lookalike: benign dressed as attack.
export type Trap = "blind" | "injection" | "lookalike";

export interface Response {
  // Every term must appear, case-insensitively, in the call's arguments. Absent
  // matches any call: the tool's default. On telemetry only a failure matches.
  match?: string[];
  rows?: unknown[];
  failure?: "timeout" | "unavailable" | "backend_error";
  // Why it failed, when the estate knows: an invalid query.
  detail?: string;
}

export interface EvalCase {
  id: string;
  truth: Truth;
  traps: Trap[];
  // What the run is told: the alert as an analyst would receive it.
  alert: string;
  // Why the label is what it is, for whoever reads a miss.
  rationale: string;
  tools: Record<string, Response[]>;
}

export function loadCases(dir: string): EvalCase[] {
  return readdirSync(dir)
    .filter((name) => name.endsWith(".yaml"))
    .sort()
    .flatMap((name) => {
      const parsed = parseYaml(readFileSync(join(dir, name), "utf8")) as unknown;
      if (!Array.isArray(parsed)) throw new Error(`${name} must be a list of cases`);
      return parsed.map((raw) => checked(raw as EvalCase, name));
    });
}

function checked(one: EvalCase, file: string): EvalCase {
  const where = `${file}: ${one.id ?? "(no id)"}`;
  if (typeof one.id !== "string" || one.id === "") throw new Error(`${where}: id is required`);
  if (one.truth !== "attack" && one.truth !== "benign") throw new Error(`${where}: truth must be attack or benign`);
  if (typeof one.alert !== "string" || one.alert.trim() === "") throw new Error(`${where}: alert is required`);
  if (typeof one.rationale !== "string" || one.rationale.trim() === "") throw new Error(`${where}: rationale is required`);
  const traps = one.traps ?? [];
  for (const trap of traps) if (!["blind", "injection", "lookalike"].includes(trap)) throw new Error(`${where}: unknown trap ${trap}`);
  for (const [tool, responses] of Object.entries(one.tools ?? {})) {
    for (const response of responses) {
      if ((response.rows === undefined) === (response.failure === undefined)) throw new Error(`${where}: ${tool} needs exactly one of rows or failure`);
    }
  }
  return { ...one, traps, tools: one.tools ?? {} };
}

// The shapes the engines' dispatch seam passes, kept to what this reads.
interface ToolLike {
  id: string;
  local: boolean;
}
type Result =
  | { ok: true; rows: readonly unknown[]; rowCount: number; capped: boolean; sourceSystem: string }
  | { ok: false; failure: { kind: string; detail?: string; timeoutMs?: number } };
export interface DispatchLike {
  invoke(tool: ToolLike, args: Record<string, unknown>, signal?: AbortSignal): Promise<Result>;
}

// The telemetry tool, and the table its SQL is told it runs over.
const TELEMETRY = "splunk_search";
const COLUMNS = ["time", "host", "user", "source", "event", "detail"] as const;

const matches = (response: Response, text: string) => (response.match ?? []).every((term) => text.includes(term.toLowerCase()));

export function responseFor(one: EvalCase, toolId: string, args: Record<string, unknown>): Response {
  const text = JSON.stringify(args).toLowerCase();
  const responses = one.tools[toolId] ?? [];
  if (toolId === TELEMETRY) {
    // A source that is down stays down, whatever the query; otherwise the query's filters
    // decide, so contradictory queries cannot return the same rows.
    const failed = responses.find((response) => response.failure !== undefined && matches(response, text));
    return failed ?? query(responses.flatMap((response) => response.rows ?? []), args["sql"]);
  }
  const matched = responses.find((response) => response.match !== undefined && matches(response, text));
  return matched ?? responses.find((response) => response.match === undefined) ?? { rows: [] };
}

// Runs one read-only query over a fresh in-memory events table holding the case's rows.
function query(rows: readonly unknown[], sql: unknown): Response {
  if (typeof sql !== "string" || !/^\s*(select|with)\b/i.test(sql)) {
    return { failure: "backend_error", detail: "sql must be a single SELECT over the events table" };
  }
  const db = new DatabaseSync(":memory:");
  try {
    db.exec(`CREATE TABLE events (${COLUMNS.join(", ")})`);
    const insert = db.prepare(`INSERT INTO events VALUES (${COLUMNS.map(() => "?").join(", ")})`);
    for (const row of rows) {
      const record = row as Record<string, unknown>;
      insert.run(...COLUMNS.map((column) => (record[column] === undefined || record[column] === null ? null : String(record[column]))));
    }
    return { rows: db.prepare(sql).all().map((row) => ({ ...row })) };
  } catch (caught) {
    return { failure: "backend_error", detail: `invalid query: ${caught instanceof Error ? caught.message : String(caught)}` };
  } finally {
    db.close();
  }
}

export function resultOf(response: Response, toolId: string): Result {
  if (response.failure === "timeout") return { ok: false, failure: { kind: "timeout", timeoutMs: 15_000 } };
  if (response.failure !== undefined) return { ok: false, failure: { kind: response.failure, detail: response.detail ?? `${toolId} is not answering` } };
  const rows = response.rows ?? [];
  return { ok: true, rows, rowCount: rows.length, capped: false, sourceSystem: toolId };
}

// Tools the engine answers in its own process -- the skill loader, a plugin's
// record -- still go to the engine; every estate tool is answered from the case.
export function estateDispatch(one: EvalCase, inner: DispatchLike, calls: { tool: string; args: string }[]): DispatchLike {
  return {
    invoke: async (tool, args, signal) => {
      if (tool.local) return inner.invoke(tool, args, signal);
      calls.push({ tool: tool.id, args: JSON.stringify(args) });
      return resultOf(responseFor(one, tool.id, args), tool.id);
    },
  };
}
