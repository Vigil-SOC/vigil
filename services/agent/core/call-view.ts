import type { ToolFailure } from "../contracts/tool.js";

// One row of the Checked tab. cost_usd is the dispatch's spend, repeated on each
// of its calls: a call has no dollar cost of its own. Absent when nothing priced
// the dispatch, which a reader shows as not recorded rather than as $0. duration_ms is absent when
// the ledger predates timing, or the attempt never reached invoke. iteration is the
// dispatch's, so a reader can tie a call to the move that asked for it; absent on a
// dispatch that carries none. failed is the kind a failed call reports, read off the
// journaled text (nothing else records it); absent when the call succeeded.
export interface CallView {
  question: string;
  tool: string;
  result_length: number;
  cost_usd?: number;
  duration_ms?: number;
  iteration?: number;
  failed?: ToolFailure["kind"];
  // The call itself, for a reader checking what was asked: its ref (opened whole at
  // /runs/<id>/calls/<ref>), the query text it sent, how many rows came back, and how
  // the kernel classed the answer. Absent on a ledger that does not carry them.
  ref?: string;
  query?: string;
  rows?: number;
  class?: string;
  // The turn's own words before its calls, shared by every call of that turn: a reader
  // groups by it rather than repeating it on each row.
  thought?: string;
}

// The argument that is the query, by the names query tools give it; otherwise the
// arguments as written, so nothing a reader needs to check is hidden.
const QUERY_KEYS = ["spl_query", "query", "search", "kql", "sql", "q", "indicator", "value", "ip", "hostname"];

export function queryOf(args: unknown): string {
  let parsed: unknown = args;
  if (typeof args === "string") {
    try {
      parsed = JSON.parse(args);
    } catch {
      return args;
    }
  }
  if (parsed === null || typeof parsed !== "object") return String(parsed ?? "");
  const record = parsed as Record<string, unknown>;
  const key = QUERY_KEYS.find((one) => typeof record[one] === "string" && record[one] !== "");
  return key === undefined ? JSON.stringify(record) : (record[key] as string);
}

// "N row(s) from <source>" opens every rendered result (core/security.ts).
const ROWS = /^(?:<vigil:tool_result[^>]*>\s*)?(\d+) rows?\b/;

// How many rows came back. A tool that wraps its answer ({success, query, results: [...]},
// as the Splunk tools do) reports one row, the wrapper; the rows are its results.
export function rowsIn(result: string): number | undefined {
  const said = ROWS.exec(result);
  if (said === null) return undefined;
  const count = Number(said[1]);
  if (count !== 1) return count;
  const body = /^<vigil:tool_result[^>]*>\n?([\s\S]*?)\n?<\/vigil:tool_result>$/.exec(result)?.[1] ?? result;
  const split = body.indexOf("\n");
  try {
    const parsed: unknown = JSON.parse(body.slice(split + 1));
    const only = Array.isArray(parsed) && parsed.length === 1 ? (parsed[0] as Record<string, unknown> | null) : null;
    return only !== null && typeof only === "object" && Array.isArray(only["results"]) ? only["results"].length : count;
  } catch {
    // A wrapper cut short by the result cap does not parse; its "1" is the wrapper, not the rows.
    return undefined;
  }
}

interface DispatchCalls {
  iteration?: number;
  query_intent?: string;
  cost_usd?: number;
  calls?: readonly unknown[];
}

// A failed call's result is `failed: <kind> -- <detail>` (renderFailure in core/security.ts),
// after the wrapper's opener. Anchored at the start so rows that merely contain the words do not match.
const FAILURE_KINDS: Record<ToolFailure["kind"], true> = { invalid_args: true, refused: true, timeout: true, unavailable: true, backend_error: true };
const FAILED_CALL = new RegExp(`^(?:<vigil:tool_result[^>]*>\\s*)?failed: (${Object.keys(FAILURE_KINDS).join("|")}) -- `);

export function callViews(dispatches: Iterable<DispatchCalls>): CallView[] {
  const rows: CallView[] = [];
  for (const dispatch of dispatches) {
    const question = dispatch.query_intent ?? "";
    const cost_usd = dispatch.cost_usd;
    const iteration = typeof dispatch.iteration === "number" ? dispatch.iteration : undefined;
    for (const call of dispatch.calls ?? []) {
      if (typeof call !== "object" || call === null) continue;
      const record = call as { tool?: unknown; result?: unknown; duration_ms?: unknown; ref?: unknown; arguments?: unknown; class?: unknown; thought?: unknown };
      const result = typeof record.result === "string" ? record.result : "";
      const duration_ms = typeof record.duration_ms === "number" ? record.duration_ms : undefined;
      const failed = FAILED_CALL.exec(result)?.[1] as ToolFailure["kind"] | undefined;
      rows.push({
        question,
        tool: typeof record.tool === "string" ? record.tool : "",
        result_length: result.length,
        ...(cost_usd === undefined ? {} : { cost_usd }),
        ...(duration_ms === undefined ? {} : { duration_ms }),
        ...(iteration === undefined ? {} : { iteration }),
        ...(failed === undefined ? {} : { failed }),
        ...(typeof record.ref === "string" ? { ref: record.ref } : {}),
        ...(queryOf(record.arguments ?? "") === "" || queryOf(record.arguments ?? "") === "{}" ? {} : { query: queryOf(record.arguments) }),
        ...(rowsIn(result) === undefined ? {} : { rows: rowsIn(result) as number }),
        ...(typeof record.class === "string" ? { class: record.class } : {}),
        ...(typeof record.thought === "string" && record.thought !== "" ? { thought: record.thought } : {}),
      });
    }
  }
  return rows;
}
