import type { CallPayload } from "../../contracts/events.js";

// A record's values checked against the results it cites, by code rather than by the
// model that wrote both. Each value is found under a matching field with the same
// value, found with a different one, or not found in any cited result: what a reader
// sees beside the value, and what decides whether a field counts as the sensor's.

export type CheckStatus = "match" | "differs" | "absent";

export interface ValueCheck {
  field: string;
  claimed: unknown;
  status: CheckStatus;
  // Where it was found: the cited call, the result's own field name and its value.
  ref?: string;
  found_field?: string;
  found?: unknown;
  // The claimed number is the found one rounded to the places the record wrote.
  rounded?: true;
}

type Row = Record<string, unknown>;

// "N row(s) from <source>" then the rows as JSON (core/security.ts renderRows).
export function rowsOf(result: string): Row[] {
  const body = /^<vigil:tool_result[^>]*>\n?([\s\S]*?)\n?<\/vigil:tool_result>$/.exec(result)?.[1] ?? result;
  const split = body.indexOf("\n");
  if (split < 0) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(body.slice(split + 1));
  } catch {
    return [];
  }
  if (!Array.isArray(parsed)) return [];
  const rows = parsed.filter((row): row is Row => row !== null && typeof row === "object" && !Array.isArray(row));
  // A tool that wraps its answer ({success, query, results: [...]}) is read as its results.
  return rows.flatMap((row) => (Array.isArray(row["results"]) ? (row["results"] as unknown[]).filter((one): one is Row => one !== null && typeof one === "object" && !Array.isArray(one)) : [row]));
}

const tokens = (name: string): string[] => name.toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);

// The same field under two writers' names: equal, or one's words a run inside the
// other's (count in conn_count, bytes_out in agg_bytes_out). A plural is its singular.
// Short forms a query writes for the words a record spells out.
const SYNONYM: Record<string, string> = { mx: "max", mn: "min", mean: "avg", average: "avg", std: "sd", stdev: "sd", stddev: "sd", cnt: "count", conn: "connection" };

export function sameField(a: string, b: string): boolean {
  const singular = (raw: string) => {
    const word = SYNONYM[raw] ?? raw;
    return word.length > 3 && word.endsWith("s") ? word.slice(0, -1) : word;
  };
  const x = tokens(a).map(singular);
  const y = tokens(b).map(singular);
  if (x.length === 0 || y.length === 0) return false;
  const within = (long: string[], short: string[]) => {
    for (let i = 0; i + short.length <= long.length; i += 1) if (short.every((word, j) => long[i + j] === word)) return true;
    return false;
  };
  return x.length >= y.length ? within(x, y) : within(y, x);
}

function scalar(value: unknown): string | number | null {
  if (typeof value === "number") return value;
  if (typeof value === "string") {
    const text = value.trim();
    if (text !== "" && /^-?\d+(\.\d+)?$/.test(text)) return Number(text);
    return text.toLowerCase();
  }
  if (typeof value === "boolean") return String(value);
  return null;
}

const same = (a: unknown, b: unknown): boolean => {
  const x = scalar(a);
  return x !== null && x === scalar(b);
};

// The places a record wrote a number to: 33.5 has one, 371 none.
function places(value: unknown): number | null {
  const text = typeof value === "number" ? String(value) : typeof value === "string" ? value.trim() : "";
  const found = /^-?\d+(?:\.(\d+))?$/.exec(text);
  return found === null ? null : (found[1]?.length ?? 0);
}

// Claimed 33.5 against a result's 33.48: the record rounded it, not got it wrong.
function roundsTo(claimed: unknown, held: unknown): boolean {
  const want = scalar(claimed);
  const have = scalar(held);
  const kept = places(claimed);
  if (typeof want !== "number" || typeof have !== "number" || kept === null || want === have) return false;
  if ((places(held) ?? 0) <= kept) return false;
  const factor = 10 ** kept;
  return Math.round(have * factor) / factor === want;
}

// A value is only telling found under an unrelated field when it could not turn up by
// chance: a number of two digits or more, or text of four characters or more.
const distinctive = (value: unknown): boolean => {
  const x = scalar(value);
  return typeof x === "number" ? Math.abs(x) >= 10 : typeof x === "string" && x.length >= 4;
};

const RANK: Record<CheckStatus, number> = { match: 2, differs: 1, absent: 0 };

function checkOne(field: string, claimed: unknown, rows: readonly Row[], anchor: Row | undefined, ref: string): ValueCheck {
  const absent: ValueCheck = { field, claimed, status: "absent" };
  // A list holds when every element of it turns up under a field of the same name.
  if (Array.isArray(claimed)) {
    const all =
      claimed.length > 0 &&
      claimed.every((one) => rows.some((row) => Object.entries(row).some(([name, held]) => sameField(field, name) && same(held, one))));
    return all ? { field, claimed, status: "match", ref } : absent;
  }
  if (scalar(claimed) === null) return absent;
  for (const row of rows) {
    for (const [name, held] of Object.entries(row)) {
      if (sameField(field, name) && same(held, claimed)) return { field, claimed, status: "match", ref, found_field: name, found: held };
    }
  }
  for (const row of rows) {
    for (const [name, held] of Object.entries(row)) {
      if (sameField(field, name) && roundsTo(claimed, held)) return { field, claimed, status: "match", ref, found_field: name, found: held, rounded: true };
    }
  }
  if (distinctive(claimed)) {
    for (const row of rows) {
      for (const [name, held] of Object.entries(row)) {
        if (same(held, claimed)) return { field, claimed, status: "match", ref, found_field: name, found: held };
      }
    }
  }
  // Not found as written: the row the record is about, under the same field, says otherwise.
  const named = anchor === undefined ? undefined : Object.entries(anchor).find(([name]) => sameField(field, name));
  return named === undefined ? absent : { field, claimed, status: "differs", ref, found_field: named[0], found: named[1] };
}

// The row a record is about: the one that agrees with most of what it says.
function anchorOf(values: readonly unknown[], rows: readonly Row[]): Row | undefined {
  let anchor: Row | undefined;
  let best = 0;
  for (const row of rows) {
    const held = Object.values(row);
    const agreed = values.filter((value) => distinctive(value) && held.some((one) => same(one, value))).length;
    if (agreed > best) {
      best = agreed;
      anchor = row;
    }
  }
  return anchor;
}

// Each value of the record against every result it cites; the best finding wins.
export function checkValues(payload: Record<string, unknown>, calls: readonly CallPayload[]): ValueCheck[] {
  const entries = Object.entries(payload).filter(([field]) => field !== "finding_id").slice(0, 30);
  const cited = calls.map((call) => {
    const rows = rowsOf(call.result);
    return { ref: call.ref, rows, anchor: anchorOf(entries.map(([, value]) => value), rows) };
  });
  return entries.map(([field, claimed]) =>
    cited
      .map((one) => checkOne(field, claimed, one.rows, one.anchor, one.ref))
      .reduce<ValueCheck>((kept, next) => (RANK[next.status] > RANK[kept.status] ? next : kept), { field, claimed, status: "absent" }),
  );
}

// Whether any cited result carries a field by this name: what lets a field the record
// rests on count as the sensor's.
export function carriesField(field: string, calls: readonly CallPayload[]): boolean {
  return calls.some((call) => rowsOf(call.result).some((row) => Object.keys(row).some((name) => sameField(field, name))));
}
