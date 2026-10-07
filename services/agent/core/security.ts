import type { ToolFailure, ToolResult } from "../contracts/tool.js";

// Tool output reaches a model inside a delimited block so it cannot read as
// direction. A result carrying the delimiter would close that block early.
const DELIMITER = /<\/?vigil:/gi;

// Truncation is marked rather than silent: output that just stops is
// indistinguishable from output that was genuinely that short.
export function clamp(text: string, cap: number): string {
  return text.length <= cap ? text : `${text.slice(0, cap)} [truncated ${text.length - cap} chars]`;
}

export function scrub(text: string, cap: number): string {
  // Control characters other than tab and newline render as nothing, which is
  // exactly what makes them useful for hiding text from a human reviewer.
  const stripped = text.replace(/[\x00-\x08\x0B-\x1F\x7F]/g, "");
  return clamp(stripped.replace(DELIMITER, "<vigil-"), cap);
}

export type Scanner = (text: string) => boolean;

// ponytail: keyword heuristic, not a classifier; upgrade if paraphrase matters.
// It only raises attention and never suppresses, so a miss costs only notice.
const INSTRUCTION_LIKE: readonly RegExp[] = [
  /\bignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)\b/i,
  /\bdisregard\s+(all\s+|any\s+)?(previous|prior|above|earlier|instructions)\b/i,
  /\byou\s+(must|should|need to|are required to)\b/i,
  /\b(system|developer)\s+(prompt|message|instruction)/i,
  /\bnew\s+instructions?\b/i,
  /^\s*#{1,6}\s/m,
];

function escape(verb: string): string {
  return verb.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// A workflow's verbs are what imitating text would use and what the harness must
// not know, so they arrive as an argument. No verbs means none, not scanning off.
export function scannerFor(verbs: readonly string[]): Scanner {
  const vocabulary = verbs.length === 0 ? [] : [new RegExp(`\\b(${verbs.map(escape).join("|")})\\b`)];
  const patterns = [...INSTRUCTION_LIKE, ...vocabulary];
  return (text) => patterns.some((pattern) => pattern.test(text));
}

export interface Wrapped {
  // What reaches the model: scrubbed, capped and delimited, in that order.
  text: string;
  instruction_like: boolean;
  // The taxonomy rather than the prose, so a workflow deciding whether this was
  // a gap in the environment or a defect in the call never parses a string.
  failure: ToolFailure | null;
}

// timeout and unavailable are gaps in what the environment could answer; refused
// and invalid_args are defects in the call and are never recorded as gaps.
export function isVisibilityGap(failure: ToolFailure): boolean {
  return failure.kind === "timeout" || failure.kind === "unavailable";
}

// The one place a ToolResult becomes text a model reads. Rows serialise here and
// nowhere else, so no caller can hand a model output that skipped the scan.
export function wrap(toolId: string, result: ToolResult, scan: Scanner, cap: number): Wrapped {
  const body = scrub(result.ok ? renderRows(result) : renderFailure(result.failure), cap);
  const id = toolId.replace(/[^\w.-]/g, "");
  return {
    text: `<vigil:tool_result tool="${id}">\n${body}\n</vigil:tool_result>`,
    instruction_like: scan(body),
    failure: result.ok ? null : result.failure,
  };
}

// capped is stated, because a model that cannot tell it saw a prefix will reason
// about the prefix as though it were the whole answer.
function renderRows(result: Extract<ToolResult, { ok: true }>): string {
  const capped = result.capped ? ", capped at the row limit" : "";
  return `${result.rowCount} row(s) from ${result.sourceSystem}${capped}\n${JSON.stringify(result.rows, null, 2)}`;
}

function renderFailure(failure: ToolFailure): string {
  const detail = failure.kind === "timeout" ? `after ${failure.timeoutMs}ms` : failure.detail;
  return `failed: ${failure.kind} -- ${detail}`;
}
