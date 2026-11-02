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

const REDACTED = "[redacted credential]";

// Whole names, normalised, so session_token and Session-Token and sessionToken are one
// entry, and token_count, tokens or password_changed_at are none.
const CREDENTIAL_FIELDS: ReadonlySet<string> = new Set([
  "password",
  "passwd",
  "pwd",
  "secret",
  "clientsecret",
  "apikey",
  "xapikey",
  "accesstoken",
  "refreshtoken",
  "idtoken",
  "sessiontoken",
  "awssessiontoken",
  "securitytoken",
  "xamzsecuritytoken",
  "secretaccesskey",
  "awssecretaccesskey",
  "authorization",
  "proxyauthorization",
  "cookie",
  "setcookie",
  "privatekey",
]);

// pwd is also the working directory in sudo and audit records, and a path is evidence.
const WORKING_DIRECTORY = /^["']?(?:[/~]|[A-Za-z]:\\)/;

function holdsCredential(field: string, value: string): boolean {
  const name = field.toLowerCase().replace(/[-_]/g, "");
  if (!CREDENTIAL_FIELDS.has(name) || value === "" || value === REDACTED) return false;
  return !(name === "pwd" && WORKING_DIRECTORY.test(value));
}

// The same names inside free text: embedded JSON or a Python dict, at the one level of
// escaping a log wrapping a log adds, or name=value in a command line or a query string.
const NAMES = [...CREDENTIAL_FIELDS].map((name) => [...name].join("[-_]?")).join("|");
const FIELDS_IN_TEXT: readonly RegExp[] = [
  new RegExp(String.raw`(?<head>(?<quote>\\?["'])(?<field>${NAMES})\k<quote>\s*:\s*\k<quote>)(?<value>[\s\S]*?)(?<tail>(?<!\\)\k<quote>)`, "gi"),
  new RegExp(String.raw`(?<head>(?<![\w-])-*(?<field>${NAMES})=)(?!\[redacted )(?<value>"[^"]*"|'[^']*'|[^\s&;,"']+)`, "gi"),
];

// By shape, under any field. Group 1, where a shape has one, is context left for the
// reader. The JWT's lookbehind keeps a long run of near misses from rescanning itself.
const CREDENTIAL_SHAPES: readonly RegExp[] = [
  /-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]{0,40}PRIVATE KEY-----|$)/g,
  /\b(Bearer\s+)(?=[\w.~+/-]*\d|[\w.~+/-]{20})[\w.~+/-]+=*/gi,
  /\b((?:proxy-)?authorization:\s*(?:Basic|Bearer)\s+)[\w.~+/-]+=*/gi,
  /(?<![\w-])((?:set-)?cookie:(?!\s*\[redacted )\s*)[^\r\n"']+/gi,
  /(?<![\w-])((?:x-)?api-key:(?!\s*\[redacted )\s*)[^\s"']+/gi,
  /\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,})/g,
  /\bxox[abeprs]-[A-Za-z0-9-]{10,}/g,
  /(?<![\w-])eyJ[\w-]+\.[\w-]+\.[\w-]*/g,
];

const isPlain = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && [Object.prototype, null].includes(Object.getPrototypeOf(value));

// Credentials out, and nothing else: an analyst still needs the addresses, accounts, hosts,
// hashes, ARNs and event text, so this masks a value in place and is no anonymiser.
export function redactCredentials<T>(value: T): { value: T; redacted: number } {
  let redacted = 0;
  const masked = (head = "", tail = ""): string => {
    redacted += 1;
    return `${head}${REDACTED}${tail}`;
  };
  const inText = (text: string): string => {
    const named = FIELDS_IN_TEXT.reduce(
      (out, pattern) =>
        out.replace(pattern, (whole, ...rest) => {
          const found = rest.at(-1) as { head: string; field: string; value: string; tail?: string };
          return holdsCredential(found.field, found.value) ? masked(found.head, found.tail) : whole;
        }),
      text,
    );
    return CREDENTIAL_SHAPES.reduce(
      (out, shape) => out.replace(shape, (_whole, kept: unknown) => masked(typeof kept === "string" ? kept : "")),
      named,
    );
  };
  // A list under a credential's name is the credential's: Set-Cookie arrives as one. An
  // object is not, since a field called secret may describe one by name and arn.
  const walk = (held: unknown, field?: string): unknown => {
    if (typeof held === "string") return field !== undefined && holdsCredential(field, held) ? masked() : inText(held);
    if (Array.isArray(held)) return held.map((one) => walk(one, field));
    if (!isPlain(held)) return held;
    return Object.fromEntries(Object.entries(held).map(([key, one]) => [inText(key), walk(one, key)]));
  };
  return { value: walk(value) as T, redacted };
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

// timeout, unavailable and a backend error are gaps: the estate did not answer. refused
// and invalid_args are defects in the call and are never recorded as gaps.
export function isVisibilityGap(failure: ToolFailure): boolean {
  return failure.kind === "timeout" || failure.kind === "unavailable" || failure.kind === "backend_error";
}

// The one place a ToolResult becomes text a model reads. Rows serialise here and
// nowhere else, so no caller can hand a model output that skipped the scan.
export function wrap(toolId: string, result: ToolResult, scan: Scanner, cap: number, ref?: string): Wrapped {
  const body = result.ok ? renderRows(result, cap) : scrub(renderFailure(result.failure), cap);
  const id = toolId.replace(/[^\w.-]/g, "");
  // ref, the id a reader cites this result by, rides the opener: the scrub above keeps
  // a body from opening a block of its own, so the result's text cannot forge it.
  const cited = ref === undefined ? "" : ` ref="${ref.replace(/[^\w.-]/g, "")}"`;
  return {
    text: `<vigil:tool_result tool="${id}"${cited}>\n${body}\n</vigil:tool_result>`,
    instruction_like: scan(body),
    failure: result.ok ? null : result.failure,
  };
}

// capped is stated, because a model that cannot tell it saw a prefix will reason
// about the prefix as though it were the whole answer. A cut is stated the same way,
// in the header rather than at the cut, where it would read as part of a value
// (#3276); the header's room is reserved so header and body stay within cap.
function renderRows(result: Extract<ToolResult, { ok: true }>, cap: number): string {
  const capped = result.capped ? ", capped at the row limit" : "";
  const head = scrub(`${result.rowCount} row(s) from ${result.sourceSystem}${capped}`, Infinity);
  const rows = scrub(JSON.stringify(result.rows) ?? "null", Infinity);
  if (head.length + 1 + rows.length <= cap) return `${head}\n${rows}`;
  const cut = (kept: number) => `${head}, ${cutNotice(kept, rows.length)}`;
  const kept = Math.max(0, cap - cut(cap).length - 1);
  return `${cut(kept)}\n${rows.slice(0, kept)}`;
}

// Read back by call-view.ts (CUT), so a run view knows the rows that follow are a prefix.
function cutNotice(kept: number, total: number): string {
  const count = (n: number) => n.toLocaleString("en-US");
  return `cut at ${count(kept)} of ${count(total)} characters: ask for less (a smaller limit, a filter, or one field)`;
}

function renderFailure(failure: ToolFailure): string {
  const detail = failure.kind === "timeout" ? `after ${failure.timeoutMs}ms` : failure.detail;
  return `failed: ${failure.kind} -- ${detail}`;
}
