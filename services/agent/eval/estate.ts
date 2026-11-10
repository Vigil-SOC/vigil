import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { parse as parseYaml } from "yaml";

// A labelled eval case and its estate: each call is answered from the case file (first
// matching response, then the tool's default, then empty). Telemetry is the exception: its
// rows form one events table the call's SQL runs over, and the case shell's command runs
// over a directory built from the case's read_case_shell rows. Nothing reaches a real system.

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
// The case shell (#4199): the case's rows become case.json in a fresh directory laid out
// as core/cases/case_view.py lays one out, and the command runs there with no
// environment, a time limit and the sidecar's output caps. The eval has no sidecar, so
// the command runs on this host: the directory is its working copy, not a boundary.
const SHELL = "read_case_shell";
const SHELL_TIMEOUT_MS = 10_000;
const STDOUT_CAP = 8_000;
const STDERR_CAP = 2_000;
const SHELL_README = `This directory is one Vigil case, read-only except scratch/.

  case.json             the case record and its notes
  scratch/              writable for this one command; gone afterwards
`;

async function caseShell(one: EvalCase, args: Record<string, unknown>): Promise<Response> {
  const caseId = String(args["case_id"] ?? "");
  const command = String(args["command"] ?? "");
  if (!caseId || !command.trim()) return { failure: "backend_error", detail: "read_case_shell needs a case_id and a command" };
  // Picked by the case id alone, so the command's words cannot choose the case's notes.
  const response = responseFor(one, SHELL, { case_id: caseId });
  if (response.failure !== undefined) return response;
  const dir = mkdtempSync(join(tmpdir(), "vigil-eval-case-"));
  try {
    mkdirSync(join(dir, "scratch"));
    writeFileSync(join(dir, "README.txt"), SHELL_README);
    writeFileSync(join(dir, "case.json"), JSON.stringify({ case_id: caseId, records: response.rows ?? [] }, null, 2) + "\n");
    const ran = await bash(command, dir);
    // As the backend reports a sidecar timeout: unavailable, not an answer.
    if (ran === null) return { failure: "timeout" };
    return { rows: [{ case_id: caseId, ...ran }] };
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

function bash(command: string, cwd: string): Promise<Record<string, unknown> | null> {
  return new Promise((resolve) => {
    const child = spawn("/bin/bash", ["-c", command], { cwd, env: { PATH: "/usr/local/bin:/usr/bin:/bin", HOME: cwd, LC_ALL: "C" }, stdio: ["ignore", "pipe", "pipe"], detached: true });
    const out: Buffer[] = [];
    const err: Buffer[] = [];
    let outBytes = 0;
    let errBytes = 0;
    let truncated = false;
    child.stdout.on("data", (chunk: Buffer) => {
      if (outBytes < STDOUT_CAP) out.push(chunk);
      outBytes += chunk.length;
      truncated ||= outBytes > STDOUT_CAP;
    });
    child.stderr.on("data", (chunk: Buffer) => {
      if (errBytes < STDERR_CAP) err.push(chunk);
      errBytes += chunk.length;
    });
    // Settles once: on exit after a short drain, so a background child holding the
    // pipes cannot hold the call, and whatever the command left is killed.
    let settled = false;
    const finish = (ran: Record<string, unknown> | null) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try { process.kill(-child.pid!, "SIGKILL"); } catch { /* already gone */ }
      resolve(ran);
    };
    const timer = setTimeout(() => finish(null), SHELL_TIMEOUT_MS);
    const answer = (code: number | null) => ({
      exit_code: code,
      stdout: Buffer.concat(out).subarray(0, STDOUT_CAP).toString("utf8"),
      stderr: Buffer.concat(err).subarray(0, STDERR_CAP).toString("utf8"),
      truncated,
    });
    child.on("error", (caught) => finish({ exit_code: null, stdout: "", stderr: `bash could not start: ${caught.message}`, truncated: false }));
    child.on("exit", (code) => setTimeout(() => finish(answer(code)), 200));
    child.on("close", (code) => finish(answer(code)));
  });
}

export function estateDispatch(one: EvalCase, inner: DispatchLike, calls: { tool: string; args: string }[]): DispatchLike {
  return {
    invoke: async (tool, args, signal) => {
      if (tool.local) return inner.invoke(tool, args, signal);
      calls.push({ tool: tool.id, args: JSON.stringify(args) });
      return resultOf(tool.id === SHELL ? await caseShell(one, args) : responseFor(one, tool.id, args), tool.id);
    },
  };
}
